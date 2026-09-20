//! The blumkin-agent daemon itself (issue #328/#339).
//!
//! A small, lazily-spawned background process that is the sole holder of
//! decrypted, time-boxed secrets: `unlock` gates a fresh secret behind a
//! macOS `LocalAuthentication` presence check (Touch ID/device password)
//! and caches it, `mlock`ed and TTL-bounded, in a [`SecretCache`];
//! `get_secret` serves it back out until that TTL elapses or `lock`/
//! `lock_all` wipes it early.
//!
//! Behavior otherwise mirrors `blumkin.agent.server` (the Python prototype
//! this replaces): same idle-exit timeout, same stale-socket detection by
//! connect-probe, same self-shutdown-on-protocol-mismatch trick that
//! avoids the chicken-and-egg problem of asking a version-rejecting agent
//! to shut down.
//!
//! **Threading**: each accepted connection is handled on its own spawned
//! thread rather than inline in the accept loop. This is required, not
//! merely nice-to-have: `unlock`'s presence check can block its calling
//! thread for up to [`crate::presence::PRESENCE_TIMEOUT`] (120s) waiting on
//! the user to respond to a Touch ID/password prompt, and a single-threaded
//! accept loop blocked that long would misreport every other, unrelated
//! connection as a wedged/dead agent (see `PRESENCE_TIMEOUT`'s docs and PR
//! #340 review). `shutdown_requested` is therefore a shared
//! [`AtomicBool`], not a plain `bool` local to the accept loop, and
//! [`SecretCache`] is shared via [`Arc`] across every connection's thread.

use std::io::ErrorKind;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use std::{fs, process, thread};

use serde_json::{json, Value};

use crate::presence::PresenceError;
use crate::protocol::{self, ProtocolError};
use crate::secret_cache::SecretCache;
use crate::{paths, version};

/// The TTL a freshly cached secret is held for before `get_secret` treats
/// it as gone and `unlock` must re-run the presence check. Matches issue
/// #339's stated default (24h); a later layer (PR3) makes this
/// configurable via the `token_reverify_after` config knob - until then,
/// every profile shares this one hardcoded default.
const DEFAULT_SECRET_TTL: Duration = Duration::from_secs(60 * 60 * 24);

/// How long the agent runs with no requests before exiting on its own, so
/// an abandoned agent does not linger as a forgotten background process
/// forever. Comfortably above `DEFAULT_SECRET_TTL` - an idle-exiting agent
/// is never the reason a still-valid cached secret disappears early.
const IDLE_EXIT_SECONDS: u64 = 60 * 60 * 26;

/// How often the accept loop wakes up to re-check the idle/shutdown
/// conditions when nothing is connecting. Small enough that `lock`/idle
/// exit feel immediate, large enough to not busy-spin the CPU.
const POLL_INTERVAL: Duration = Duration::from_millis(100);

/// How long a single accepted connection is given to send its request
/// before the agent gives up on it - a slow/stuck client must never wedge
/// its handler thread indefinitely. Bounds only `recv_message`'s wait for
/// the request to arrive, not `dispatch`'s own processing of it once
/// received (an `unlock`'s presence check runs on this same handler
/// thread and may legitimately take up to `PRESENCE_TIMEOUT` - see the
/// module docs above).
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

    let cache = Arc::new(SecretCache::new(DEFAULT_SECRET_TTL));
    let shutdown_requested = Arc::new(AtomicBool::new(false));
    let mut last_activity = Instant::now();
    // Reaped opportunistically on each loop iteration (never joined
    // eagerly - a still-prompting `unlock` must not hold up accepting new
    // connections) so this does not grow unbounded over a long-lived
    // agent's life.
    let mut connection_threads: Vec<thread::JoinHandle<()>> = Vec::new();
    while !shutdown_requested.load(Ordering::SeqCst) {
        if last_activity.elapsed().as_secs() > IDLE_EXIT_SECONDS {
            break;
        }
        connection_threads.retain(|handle| !handle.is_finished());
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
                let cache = Arc::clone(&cache);
                let shutdown_requested = Arc::clone(&shutdown_requested);
                connection_threads.push(thread::spawn(move || {
                    handle_connection(stream, &cache, &shutdown_requested);
                }));
            }
            Err(e) if e.kind() == ErrorKind::WouldBlock => thread::sleep(POLL_INTERVAL),
            Err(_) => thread::sleep(POLL_INTERVAL),
        }
    }
    // Best-effort: wipe every cached secret before exiting rather than
    // just letting the process's memory go away, in case anything ever
    // reads this process's freed pages before the OS reclaims them.
    cache.lock_all();
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

fn dispatch(request: &Value, shutdown_requested: &AtomicBool, cache: &SecretCache) -> Value {
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
        shutdown_requested.store(true, Ordering::SeqCst);
        return json!({
            "ok": false,
            "error": "protocol_mismatch",
            "agent_protocol_version": protocol::PROTOCOL_VERSION,
            "agent_version": version::package_version(),
            "agent_pid": process::id(),
        });
    }
    match request.get("cmd").and_then(Value::as_str) {
        Some("get_secret") => handle_get_secret(request, cache),
        Some("lock") => handle_lock(request, cache),
        Some("ping") => handle_ping(),
        Some("shutdown") => handle_shutdown(shutdown_requested),
        Some("status") => handle_status(cache),
        Some("unlock") => handle_unlock(request, cache),
        other => json!({
            "ok": false,
            "error": "unknown_command",
            "message": format!("no such command: {other:?}"),
        }),
    }
}

fn handle_connection(mut stream: UnixStream, cache: &SecretCache, shutdown_requested: &AtomicBool) {
    let response = match protocol::recv_message(&mut stream, Some(REQUEST_TIMEOUT)) {
        Ok(request) => dispatch(&request, shutdown_requested, cache),
        Err(ProtocolError(message)) => {
            json!({"ok": false, "error": "protocol_error", "message": message})
        }
    };
    let _ = protocol::send_message(&mut stream, &response);
}

/// Returns `profile`'s cached secret if `unlock` has verified it and its
/// TTL has not yet elapsed.
fn handle_get_secret(request: &Value, cache: &SecretCache) -> Value {
    let profile = match non_empty_str_field(request, "profile") {
        Ok(profile) => profile,
        Err(response) => return response,
    };
    match cache.get(profile) {
        Some(handle) => match std::str::from_utf8(&handle) {
            Ok(secret) => json!({"ok": true, "secret": secret}),
            Err(_) => json!({
                "ok": false,
                "error": "corrupt_secret",
                "message": "cached secret is not valid UTF-8",
            }),
        },
        None => json!({"ok": false, "error": "not_cached"}),
    }
}

/// Wipe cached secret state: one profile if `request` names it, every
/// profile otherwise. No longer requests shutdown - now that a real secret
/// cache exists, "lock" and "shut down" are distinct operations (an
/// operator wiping a stale credential from memory should not also have to
/// wait for a fresh agent to respawn on their next command).
fn handle_lock(request: &Value, cache: &SecretCache) -> Value {
    // An absent `profile` field means "lock everything"; a *present* one
    // must be a non-empty string - `{"profile": 1}` or `{"profile": ""}`
    // are rejected rather than silently falling through to `lock_all` (a
    // malformed non-string) or to a no-op `lock("")` (an empty string),
    // either of which would otherwise misreport what was actually locked
    // (see PR #341 review).
    match request.get("profile") {
        None => cache.lock_all(),
        Some(_) => match non_empty_str_field(request, "profile") {
            Ok(profile) => cache.lock(profile),
            Err(response) => return response,
        },
    }
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

fn handle_shutdown(shutdown_requested: &AtomicBool) -> Value {
    shutdown_requested.store(true, Ordering::SeqCst);
    json!({"ok": true})
}

fn handle_status(cache: &SecretCache) -> Value {
    json!({
        "ok": true,
        "agent_version": version::package_version(),
        "agent_commit": version::source_commit(),
        "agent_pid": process::id(),
        "protocol_version": protocol::PROTOCOL_VERSION,
        "cached_profiles": cache.cached_profiles(),
    })
}

/// Runs the presence check for `profile` (the OS prompt shown to the user
/// is `request`'s `reason` field) and, only on success, caches `secret` for
/// it. Runs on this connection's own handler thread - which may block for
/// up to `PRESENCE_TIMEOUT` waiting on the prompt - never on the shared
/// accept loop (see this module's docs).
fn handle_unlock(request: &Value, cache: &SecretCache) -> Value {
    let profile = match non_empty_str_field(request, "profile") {
        Ok(profile) => profile,
        Err(response) => return response,
    };
    let reason = match non_empty_str_field(request, "reason") {
        Ok(reason) => reason,
        Err(response) => return response,
    };
    let secret = match request.get("secret").and_then(Value::as_str) {
        Some(secret) => secret,
        None => return invalid_request("unlock requires a \"secret\" string"),
    };
    match cache.unlock(profile, reason, secret.as_bytes().to_vec()) {
        Ok(()) => json!({"ok": true}),
        Err(err) => presence_error_response(err),
    }
}

fn invalid_request(message: &str) -> Value {
    json!({"ok": false, "error": "invalid_request", "message": message})
}

/// Extracts a required, non-empty string field, or an `invalid_request`
/// response describing exactly what was missing/malformed.
fn non_empty_str_field<'a>(request: &'a Value, field: &str) -> Result<&'a str, Value> {
    match request.get(field).and_then(Value::as_str) {
        Some(value) if !value.is_empty() => Ok(value),
        _ => Err(invalid_request(&format!(
            "{field} must be present and a non-empty string"
        ))),
    }
}

fn presence_error_response(err: PresenceError) -> Value {
    let error = match err {
        PresenceError::Denied(_) => "presence_denied",
        PresenceError::TimedOut => "presence_timed_out",
        PresenceError::Unsupported => "presence_unsupported",
        PresenceError::Busy => "presence_busy",
    };
    json!({"ok": false, "error": error, "message": err.to_string()})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paths::tests::ENV_LOCK;

    #[test]
    fn dispatch_accepts_a_matching_protocol_version() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request = json!({"cmd": "ping", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], true);
        assert!(!shutdown_requested.load(Ordering::SeqCst));
    }

    #[test]
    fn dispatch_get_secret_reports_not_cached_for_an_unknown_profile() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request = json!({
            "cmd": "get_secret",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "never-unlocked",
        });

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "not_cached");
    }

    #[test]
    fn dispatch_lock_wipes_the_cache_without_requesting_shutdown() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        cache.unlock("work", "unlock", b"s3cr3t".to_vec()).unwrap();
        let request = json!({"cmd": "lock", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], true);
        assert!(!shutdown_requested.load(Ordering::SeqCst));
        assert_eq!(cache.get("work").as_deref(), None);
    }

    #[test]
    fn dispatch_lock_with_a_profile_wipes_only_that_profile() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        cache.unlock("work", "unlock", b"work".to_vec()).unwrap();
        cache.unlock("home", "unlock", b"home".to_vec()).unwrap();
        let request = json!({
            "cmd": "lock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
        });

        dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(cache.get("work").as_deref(), None);
        assert_eq!(cache.get("home").as_deref(), Some(b"home".as_slice()));
    }

    #[test]
    fn dispatch_lock_rejects_a_malformed_profile_without_wiping_anything() {
        // A non-string `profile` (e.g. `1`) must not silently fall through
        // to `lock_all` - that would wipe every profile in response to a
        // malformed request instead of rejecting it (see PR #341 review).
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        cache.unlock("work", "unlock", b"work".to_vec()).unwrap();
        let request = json!({
            "cmd": "lock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": 1,
        });

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "invalid_request");
        assert_eq!(cache.get("work").as_deref(), Some(b"work".as_slice()));
    }

    #[test]
    fn dispatch_rejects_a_protocol_version_mismatch_and_requests_shutdown() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request = json!({"cmd": "ping", "protocol_version": protocol::PROTOCOL_VERSION + 1});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "protocol_mismatch");
        assert!(shutdown_requested.load(Ordering::SeqCst));
    }

    #[test]
    fn dispatch_reports_an_unknown_command() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request =
            json!({"cmd": "not_a_real_command", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "unknown_command");
    }

    #[test]
    fn dispatch_shutdown_requests_shutdown() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request = json!({"cmd": "shutdown", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], true);
        assert!(shutdown_requested.load(Ordering::SeqCst));
    }

    #[test]
    fn dispatch_status_reports_cached_profiles() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        cache.unlock("work", "unlock", b"s3cr3t".to_vec()).unwrap();
        let request = json!({"cmd": "status", "protocol_version": protocol::PROTOCOL_VERSION});

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], true);
        assert_eq!(response["cached_profiles"], json!(["work"]));
    }

    #[test]
    fn dispatch_unlock_rejects_a_missing_profile_field() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let request = json!({
            "cmd": "unlock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "reason": "unlock",
            "secret": "s3cr3t",
        });

        let response = dispatch(&request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "invalid_request");
    }

    #[test]
    fn dispatch_unlock_reports_presence_denial_without_caching_anything() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Err(
                PresenceError::Denied("wrong password".to_string()),
            ))),
        );
        let unlock_request = json!({
            "cmd": "unlock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
            "reason": "unlock the work profile cache",
            "secret": "s3cr3t",
        });

        let response = dispatch(&unlock_request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "presence_denied");

        let get_request = json!({
            "cmd": "get_secret",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
        });
        let get_response = dispatch(&get_request, &shutdown_requested, &cache);
        assert_eq!(get_response["ok"], false);
        assert_eq!(get_response["error"], "not_cached");
    }

    #[test]
    fn dispatch_unlock_reports_presence_busy_when_another_profile_is_checking() {
        // `presence_error_response` maps every `PresenceError` variant to
        // a distinct protocol error code - this exercises `Busy`
        // specifically (see `SecretCache::verify_presence_once`'s docs on
        // why a cross-profile presence check fails fast instead of
        // queuing), separately from `FakePresenceVerifier`'s own coverage
        // of `Denied`/`TimedOut` elsewhere in this file.
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Err(
                PresenceError::Busy,
            ))),
        );
        let unlock_request = json!({
            "cmd": "unlock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
            "reason": "unlock the work profile cache",
            "secret": "s3cr3t",
        });

        let response = dispatch(&unlock_request, &shutdown_requested, &cache);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"], "presence_busy");
    }

    #[test]
    fn dispatch_unlock_then_get_secret_roundtrips_the_secret() {
        let shutdown_requested = AtomicBool::new(false);
        let cache = SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
        );
        let unlock_request = json!({
            "cmd": "unlock",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
            "reason": "unlock the work profile cache",
            "secret": "s3cr3t",
        });

        let unlock_response = dispatch(&unlock_request, &shutdown_requested, &cache);
        assert_eq!(unlock_response["ok"], true);

        let get_request = json!({
            "cmd": "get_secret",
            "protocol_version": protocol::PROTOCOL_VERSION,
            "profile": "work",
        });
        let get_response = dispatch(&get_request, &shutdown_requested, &cache);

        assert_eq!(get_response["ok"], true);
        assert_eq!(get_response["secret"], "s3cr3t");
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
                        let shutdown_requested = AtomicBool::new(false);
                        let cache = SecretCache::with_verifier(
                            Duration::from_secs(60),
                            Box::new(crate::presence::tests::FakePresenceVerifier::always(Ok(()))),
                        );
                        let response = dispatch(&request, &shutdown_requested, &cache);
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
