//! The blumkin-agent daemon itself (foundation layer, issue #328).
//!
//! A small, lazily-spawned background process that will become the sole
//! holder of decrypted, time-boxed secrets once later layers land (macOS
//! `LocalAuthentication` presence check + keychain-backed cache). This
//! foundation layer only implements the daemon's lifecycle and control
//! surface - version handshake, health check, and a `lock` command that
//! already does the one thing it can meaningfully do yet: ask the agent to
//! exit (there is nothing cached to drop in memory until the secret-serving
//! layer exists, so "lock" and "shut down" are the same operation for now).
//!
//! Behavior mirrors `blumkin.agent.server` (the Python prototype this
//! replaces) exactly: same idle-exit timeout, same stale-socket detection
//! by connect-probe, same self-shutdown-on-protocol-mismatch trick that
//! avoids the chicken-and-egg problem of asking a version-rejecting agent
//! to shut down.

use std::io::ErrorKind;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::time::{Duration, Instant};
use std::{fs, process, thread};

use serde_json::{json, Value};

use crate::protocol::{self, ProtocolError};
use crate::{paths, version};

/// How long the agent runs with no requests before exiting on its own, so
/// an abandoned agent does not linger as a forgotten background process
/// forever. Comfortably above the eventual default 24h re-verify TTL (a
/// later layer) - this foundation layer has no per-profile TTL of its own
/// yet, only this blanket idle exit.
const IDLE_EXIT_SECONDS: u64 = 60 * 60 * 26;

/// How often the accept loop wakes up to re-check the idle/shutdown
/// conditions when nothing is connecting. Small enough that `lock`/idle
/// exit feel immediate, large enough to not busy-spin the CPU.
const POLL_INTERVAL: Duration = Duration::from_millis(100);

/// How long a single accepted connection is given to send its request
/// before the agent gives up on it - a slow/stuck client must never wedge
/// the single-threaded accept loop indefinitely.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

/// How long `bind` waits for a just-probed, exiting-but-not-yet-dead agent
/// to unlink its own socket before this process removes it instead. A
/// self-shutting-down agent breaks out of its accept loop and unlinks
/// immediately after replying to the probe, so this only needs to be
/// generous, not anywhere near `REQUEST_TIMEOUT` (see PR #329 review).
const STALE_SOCKET_GRACE: Duration = Duration::from_millis(500);

pub fn run() {
    let sock_path = paths::socket_path();
    let listener = match bind(&sock_path) {
        Ok(Some(listener)) => listener,
        Ok(None) => return, // another agent is already running - nothing to do
        Err(err) => {
            eprintln!(
                "blumkin-agent: could not bind {}: {err}",
                sock_path.display()
            );
            process::exit(1);
        }
    };
    listener
        .set_nonblocking(true)
        .expect("set_nonblocking failed");

    let mut last_activity = Instant::now();
    let mut shutdown_requested = false;
    while !shutdown_requested {
        if last_activity.elapsed().as_secs() > IDLE_EXIT_SECONDS {
            break;
        }
        match listener.accept() {
            Ok((stream, _addr)) => {
                last_activity = Instant::now();
                // On Darwin (the only platform this binary targets - see
                // `paths::is_supported_platform` in the Python client), a
                // connection accepted from a non-blocking listener inherits
                // O_NONBLOCK, unlike Linux which clears it. Left set, the
                // non-blocking read in `protocol::recv_message` would
                // return immediately instead of honoring `REQUEST_TIMEOUT`,
                // misreporting a live-but-not-yet-written request as a
                // protocol error (see PR #329 review).
                if let Err(err) = stream.set_nonblocking(false) {
                    eprintln!(
                        "blumkin-agent: could not clear O_NONBLOCK on accepted stream: {err}"
                    );
                    continue;
                }
                handle_connection(stream, &mut shutdown_requested);
            }
            Err(e) if e.kind() == ErrorKind::WouldBlock => thread::sleep(POLL_INTERVAL),
            Err(_) => thread::sleep(POLL_INTERVAL),
        }
    }
    cleanup(&sock_path);
}

/// Bind exclusively, refusing to start a second agent for this user.
///
/// A stale socket file left behind by a crashed agent is distinguished from
/// a live one by trying to connect to it first: a live agent accepts the
/// connection (this process then exits immediately rather than compete
/// with it, signaled here by returning `Ok(None)`), a dead one refuses/
/// times out and the stale path is removed before binding fresh.
///
/// `Ok(None)` ("defer to a live agent") and `Err` (a genuine bind failure,
/// e.g. an over-long `sun_path` or an unwritable runtime dir) must stay
/// distinguishable: `UnixListener::bind(..).ok()` used to collapse both
/// into `None`, so a real failure silently looked like a healthy no-op and
/// `run()` exited 0 without a trace - the client then burned its whole
/// `_wait_for_socket_ready` deadline waiting on a path nothing ever bound
/// (see PR #329 review).
fn bind(sock_path: &Path) -> std::io::Result<Option<UnixListener>> {
    if sock_path.exists() {
        if agent_is_alive(sock_path) {
            return Ok(None);
        }
        // The probe above may have just told an *old, still-running*
        // agent (protocol mismatch) to shut itself down - it is not dead
        // yet, only committed to exiting, and its own `cleanup()` will
        // unlink this same path shortly. Removing the path ourselves right
        // now and binding over it would let that agent's later `cleanup()`
        // delete *our* freshly bound socket instead. Give it a bounded
        // window to finish unlinking on its own first (see PR #329
        // review); a genuinely stale path from a crash just sits there
        // for the whole wait and gets removed by the fallback below, same
        // as before.
        wait_for_path_gone(sock_path);
        // `wait_for_path_gone` succeeding (the common case: the other
        // agent unlinked it before the grace window elapsed) means the
        // path is already gone - `remove_file` returning `NotFound` here
        // is that success, not a failure, and must not be propagated as
        // one (see PR #329 review: this previously made a perfectly
        // healthy respawn print "could not bind ...: No such file or
        // directory" and exit 1). A genuinely stale path that is still
        // present (permissions, or the wait timed out) still needs its
        // removal failure to surface.
        if let Err(err) = fs::remove_file(sock_path) {
            if err.kind() != ErrorKind::NotFound {
                return Err(err);
            }
        }
    }
    let listener = UnixListener::bind(sock_path)?;
    // Belt-and-braces: `bind` honors umask, which may be looser than the
    // `0600` this private, per-user socket needs (the containing directory
    // is already `0700` per `paths::runtime_dir`, but a defense-in-depth
    // mode on the socket file itself costs nothing).
    let _ = fs::set_permissions(sock_path, fs::Permissions::from_mode(0o600));
    Ok(Some(listener))
}

/// Wait (briefly, boundedly) for a path to disappear on its own.
fn wait_for_path_gone(sock_path: &Path) {
    let deadline = Instant::now() + STALE_SOCKET_GRACE;
    while sock_path.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
}

fn agent_is_alive(sock_path: &Path) -> bool {
    let Ok(mut probe) = UnixStream::connect(sock_path) else {
        return false;
    };
    let ping = json!({"cmd": "ping", "protocol_version": protocol::PROTOCOL_VERSION});
    if protocol::send_message(&mut probe, &ping).is_err() {
        return false;
    }
    // Must be at least as long as `REQUEST_TIMEOUT`: the daemon being
    // probed is single-threaded and may currently be blocked inside
    // `handle_connection` on an unrelated, slow-but-legitimate client for
    // up to that long. A shorter probe budget would misjudge a live,
    // merely-busy agent as dead, causing `bind` to delete its socket out
    // from under it and listen over it from a second process (see PR #329
    // review).
    let probe_timeout = REQUEST_TIMEOUT + Duration::from_secs(1);
    // A `protocol_mismatch` reply (`"ok": false`) means the probed agent
    // has just committed to shutting itself down (see `dispatch` below) -
    // not that it is healthy and staying up. Treating any parseable reply
    // as "alive" made `bind` defer to an agent that was, at that very
    // moment, unwinding its own accept loop: nothing ended up listening
    // once both processes exited (see PR #329 review).
    match protocol::recv_message(&mut probe, Some(probe_timeout)) {
        Ok(response) => response.get("ok").and_then(Value::as_bool).unwrap_or(false),
        Err(_) => false,
    }
}

fn cleanup(sock_path: &Path) {
    let _ = fs::remove_file(sock_path);
}

fn dispatch(request: &Value, shutdown_requested: &mut bool) -> Value {
    let client_protocol_version = request.get("protocol_version").and_then(Value::as_u64);
    if client_protocol_version != Some(protocol::PROTOCOL_VERSION) {
        // A mismatch almost always means *this* agent is the stale one -
        // still resident from before an upgrade replaced the binary.
        // Rather than have the client separately ask this agent to shut
        // down (which would hit this same version check and be rejected
        // again, a chicken-and-egg problem), the agent shuts itself down
        // as soon as it sees any mismatch, right after replying, so the
        // client can simply wait for the socket to clear and spawn a
        // fresh one (see the "Installation & upgrade" section of issue
        // #328).
        *shutdown_requested = true;
        return json!({
            "ok": false,
            "error": "protocol_mismatch",
            "agent_protocol_version": protocol::PROTOCOL_VERSION,
            "agent_version": version::package_version(),
            "agent_pid": process::id(),
        });
    }
    match request.get("cmd").and_then(Value::as_str) {
        Some("lock") => handle_lock(shutdown_requested),
        Some("ping") => handle_ping(),
        Some("shutdown") => handle_shutdown(shutdown_requested),
        Some("status") => handle_status(),
        other => json!({
            "ok": false,
            "error": "unknown_command",
            "message": format!("no such command: {other:?}"),
        }),
    }
}

fn handle_connection(mut stream: UnixStream, shutdown_requested: &mut bool) {
    let response = match protocol::recv_message(&mut stream, Some(REQUEST_TIMEOUT)) {
        Ok(request) => dispatch(&request, shutdown_requested),
        Err(ProtocolError(message)) => {
            json!({"ok": false, "error": "protocol_error", "message": message})
        }
    };
    let _ = protocol::send_message(&mut stream, &response);
}

/// Drop all cached state and exit.
///
/// There is nothing cached to drop yet in this foundation layer, so this is
/// equivalent to `shutdown` today; once the secret cache exists, this
/// becomes "wipe every profile's cached secret" without necessarily exiting
/// the process. Kept as its own command name now (rather than introduced
/// later) so the CLI surface (`blumkin agent lock`) and its tests do not
/// need to change shape when that lands.
fn handle_lock(shutdown_requested: &mut bool) -> Value {
    *shutdown_requested = true;
    json!({"ok": true})
}

fn handle_ping() -> Value {
    json!({
        "ok": true,
        "agent_version": version::package_version(),
        "agent_commit": version::source_commit(),
        "agent_pid": process::id(),
        "protocol_version": protocol::PROTOCOL_VERSION,
    })
}

fn handle_shutdown(shutdown_requested: &mut bool) -> Value {
    *shutdown_requested = true;
    json!({"ok": true})
}

fn handle_status() -> Value {
    json!({
        "ok": true,
        "agent_version": version::package_version(),
        "agent_commit": version::source_commit(),
        "agent_pid": process::id(),
        "protocol_version": protocol::PROTOCOL_VERSION,
        // No per-profile cache exists in this foundation layer yet -
        // always reported empty until the secret-serving layer lands.
        "cached_profiles": Vec::<String>::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paths::tests::ENV_LOCK;

    #[test]
    fn dispatch_rejects_a_protocol_version_mismatch_and_requests_shutdown() {
        let mut shutdown_requested = false;
        let request = json!({"cmd": "ping", "protocol_version": protocol::PROTOCOL_VERSION + 1});

        let response = dispatch(&request, &mut shutdown_requested);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "protocol_mismatch");
        assert!(shutdown_requested);
    }

    #[test]
    fn dispatch_accepts_a_matching_protocol_version() {
        let mut shutdown_requested = false;
        let request = json!({"cmd": "ping", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &mut shutdown_requested);

        assert_eq!(response["ok"], true);
        assert!(!shutdown_requested);
    }

    #[test]
    fn dispatch_reports_an_unknown_command() {
        let mut shutdown_requested = false;
        let request =
            json!({"cmd": "not_a_real_command", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &mut shutdown_requested);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "unknown_command");
    }

    #[test]
    fn dispatch_lock_requests_shutdown() {
        let mut shutdown_requested = false;
        let request = json!({"cmd": "lock", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &mut shutdown_requested);

        assert_eq!(response["ok"], true);
        assert!(shutdown_requested);
    }

    #[test]
    fn bind_replaces_a_stale_socket_file_with_no_listener() {
        // `env::temp_dir()` reads the same process-wide TMPDIR/TEMP/TMP
        // env vars `paths::tests` mutates; share its lock so this test never
        // observes a momentarily-swapped temp dir (see PR #329 review).
        let _env_lock = ENV_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!("blumkin-agent-test-{}", process::id()));
        fs::create_dir_all(&dir).unwrap();
        let sock_path = dir.join("stale.sock");
        // A plain file (never bound/listened on) stands in for a socket
        // path left behind by a crashed agent: `agent_is_alive` cannot
        // connect to it, so `bind` must remove it and bind fresh.
        fs::write(&sock_path, b"not a real socket").unwrap();

        let listener = bind(&sock_path);

        assert!(matches!(listener, Ok(Some(_))));
        fs::remove_file(&sock_path).ok();
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_refuses_to_start_a_second_agent_when_one_is_already_listening() {
        // `env::temp_dir()` reads the same process-wide TMPDIR/TEMP/TMP
        // env vars `paths::tests` mutates; share its lock so this test never
        // observes a momentarily-swapped temp dir (see PR #329 review).
        let _env_lock = ENV_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!("blumkin-agent-test2-{}", process::id()));
        fs::create_dir_all(&dir).unwrap();
        let sock_path = dir.join("live.sock");
        let live_listener = UnixListener::bind(&sock_path).unwrap();
        live_listener.set_nonblocking(true).unwrap();

        // Accept in the background so `agent_is_alive`'s connect+ping
        // succeeds against a real (if minimal) peer.
        let accept_sock_path = sock_path.clone();
        let handle = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if let Ok((mut stream, _)) = live_listener.accept() {
                    stream.set_nonblocking(false).unwrap();
                    if let Ok(request) = protocol::recv_message(&mut stream, Some(REQUEST_TIMEOUT))
                    {
                        let mut shutdown_requested = false;
                        let response = dispatch(&request, &mut shutdown_requested);
                        let _ = protocol::send_message(&mut stream, &response);
                    }
                    return;
                }
                thread::sleep(Duration::from_millis(10));
            }
        });

        let result = bind(&sock_path);

        assert!(matches!(result, Ok(None)));
        handle.join().unwrap();
        fs::remove_file(&accept_sock_path).ok();
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_reports_a_genuine_failure_distinctly_from_deferring_to_a_live_agent() {
        // `env::temp_dir()` reads the same process-wide TMPDIR/TEMP/TMP
        // env vars `paths::tests` mutates; share its lock so this test never
        // observes a momentarily-swapped temp dir (see PR #329 review).
        let _env_lock = ENV_LOCK.lock().unwrap();
        // An `AF_UNIX` path over the platform's `sun_path` limit (104 bytes
        // on macOS) makes the underlying `bind(2)` fail - this must surface
        // as `Err`, not the same `Ok(None)` used for "a live agent already
        // owns this path" (see PR #329 review).
        let dir = std::env::temp_dir().join(format!(
            "blumkin-agent-test-toolong-{}-{}",
            process::id(),
            "x".repeat(80)
        ));
        fs::create_dir_all(&dir).unwrap();
        let sock_path = dir.join("agent.sock");

        let result = bind(&sock_path);

        assert!(result.is_err());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_succeeds_when_the_probed_agent_unlinks_its_socket_during_the_grace_wait() {
        // `env::temp_dir()` reads the same process-wide TMPDIR/TEMP/TMP
        // env vars `paths::tests` mutates; share its lock so this test never
        // observes a momentarily-swapped temp dir (see PR #329 review).
        let _env_lock = ENV_LOCK.lock().unwrap();
        // The `wait_for_path_gone` grace window's *success* case: a
        // not-alive-but-not-yet-dead agent unlinks the path itself while
        // this process is waiting. `remove_file` must then tolerate the
        // resulting `NotFound` instead of surfacing it as a bind failure
        // (see PR #329 review - this previously made a healthy respawn
        // fail with "could not bind ...: No such file or directory").
        let dir = std::env::temp_dir().join(format!("blumkin-agent-test-vanish-{}", process::id()));
        fs::create_dir_all(&dir).unwrap();
        let sock_path = dir.join("vanishing.sock");
        // A plain file (not a socket) so `agent_is_alive`'s connect fails
        // and `bind` proceeds into the grace wait.
        fs::write(&sock_path, b"not a real socket").unwrap();
        let vanish_path = sock_path.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            let _ = fs::remove_file(&vanish_path);
        });

        let result = bind(&sock_path);

        assert!(matches!(result, Ok(Some(_))));
        fs::remove_file(&sock_path).ok();
        fs::remove_dir_all(&dir).ok();
    }
}
