//! Filesystem/socket paths for the blumkin-agent background process.
//!
//! Mirrors `blumkin.agent.paths` (Python) exactly, including the
//! `BLUMKIN_AGENT_RUNTIME_DIR` test override, so both implementations agree
//! on where the socket lives without sharing any code.

use std::env;
use std::fs;
use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

/// Private, per-user directory the agent's socket lives under.
///
/// Scoped by the real uid (not just `$USER`, which a same-named account on
/// a different uid could spoof) so two accounts that happen to share
/// `$TMPDIR` can never collide on the same socket path.
/// `BLUMKIN_AGENT_RUNTIME_DIR` is an override for tests, not something an
/// operator needs to set.
///
/// The directory name is predictable under a base that is often
/// world-writable (`/tmp`), so a same-privilege local attacker could
/// pre-create it (or a symlink through it) before this process ever runs -
/// `ensure_private_owned_dir` refuses that rather than silently binding
/// into a directory this process does not own (see PR #329 review).
pub fn runtime_dir() -> PathBuf {
    let directory = base_dir().join(format!("blumkin-agent-{}", current_uid()));
    if let Err(err) = ensure_private_owned_dir(&directory) {
        eprintln!(
            "blumkin-agent: refusing to use runtime dir {}: {err}",
            directory.display()
        );
        std::process::exit(1);
    }
    directory
}

/// Unix domain socket path the agent listens on and clients connect to.
pub fn socket_path() -> PathBuf {
    runtime_dir().join("agent.sock")
}

/// Base directory temporary files live under.
///
/// Mirrors Python's `runtime_base_dir()` exactly (issue #402): on macOS,
/// `os.confstr(CS_DARWIN_USER_TEMP_DIR)` / `darwin_user_temp_dir()` below
/// read the same per-user, per-boot directory straight from the OS,
/// bypassing `$TMPDIR` entirely - a GUI-launched `blumkin mcp serve` with no
/// `TMPDIR` in its environment previously spawned this daemon into `/tmp`
/// while the Python client resolved `/var/folders/.../T/`, leaving the
/// client unable to ever reach the daemon it just started. Only when that
/// lookup is unavailable does this fall back to the old `TMPDIR`/`TEMP`/
/// `TMP` order (mirroring Python's `tempfile.gettempdir()`), then `/tmp`,
/// `/var/tmp`, `/usr/tmp`, then the current directory. Both implementations
/// must keep agreeing here, or they can resolve different socket paths
/// (see PR #329 review).
fn base_dir() -> PathBuf {
    if let Some(dir) = env::var_os("BLUMKIN_AGENT_RUNTIME_DIR") {
        return PathBuf::from(dir);
    }
    #[cfg(target_os = "macos")]
    if let Some(dir) = darwin_user_temp_dir() {
        return dir;
    }
    for envname in ["TMPDIR", "TEMP", "TMP"] {
        if let Some(dir) = env::var_os(envname) {
            let candidate = PathBuf::from(dir);
            if candidate.is_dir() {
                return candidate;
            }
        }
    }
    for fallback in ["/tmp", "/var/tmp", "/usr/tmp"] {
        let candidate = PathBuf::from(fallback);
        if candidate.is_dir() {
            return candidate;
        }
    }
    env::current_dir().unwrap_or_else(|_| PathBuf::from("/tmp"))
}

/// The per-user, per-boot temp directory macOS hands out via `confstr(3)`
/// (the same one `TMPDIR` normally points a login shell at) - mirrors
/// Python's `agent.paths.runtime_base_dir` exactly. `CS_DARWIN_USER_TEMP_DIR`
/// is not exposed by the `libc` crate's constant tables (Darwin-specific,
/// outside POSIX's `_CS_*` set), so it is passed as the raw value from
/// macOS's `<unistd.h>`. Returns `None` on any failure (unrecognized
/// `confstr` name, non-UTF-8 result) so `base_dir` can fall through to the
/// environment-dependent candidates below.
#[cfg(target_os = "macos")]
fn darwin_user_temp_dir() -> Option<PathBuf> {
    const CS_DARWIN_USER_TEMP_DIR: libc::c_int = 65537;
    // SAFETY: `confstr` with a null buffer and `len` 0 only queries the
    // required buffer size and never writes through the pointer.
    let needed = unsafe { libc::confstr(CS_DARWIN_USER_TEMP_DIR, std::ptr::null_mut(), 0) };
    if needed == 0 {
        return None;
    }
    let mut buf = vec![0u8; needed];
    // SAFETY: `buf` is `needed` bytes long, matching the `len` argument, so
    // `confstr` cannot write past the end of the allocation.
    let written = unsafe {
        libc::confstr(
            CS_DARWIN_USER_TEMP_DIR,
            buf.as_mut_ptr() as *mut libc::c_char,
            needed,
        )
    };
    if written == 0 || written > needed {
        return None;
    }
    // `written` includes the trailing NUL confstr(3) always writes.
    std::str::from_utf8(&buf[..written - 1])
        .ok()
        .map(PathBuf::from)
}

fn current_uid() -> u32 {
    // SAFETY: `getuid()` takes no arguments and cannot fail.
    unsafe { libc::getuid() }
}

/// Create (or adopt) `directory` at `0700`, refusing anything an attacker
/// could have planted there first.
///
/// Mirrors `secret_store._refuse_symlinked_path_components`'s symlink
/// refusal, plus an explicit ownership/mode check plain `mkdir` cannot
/// provide: `mkdir` on an already-existing path is a no-op, and a failed
/// `chmod` was previously swallowed silently by this module.
fn ensure_private_owned_dir(directory: &Path) -> io::Result<()> {
    if is_symlink(directory) {
        return Err(io::Error::other(format!(
            "{} is a symlink - refusing to follow it",
            directory.display()
        )));
    }
    match fs::create_dir(directory) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {}
        Err(err) => return Err(err),
    }
    // Re-check after the (possibly no-op) create: a racing attacker could
    // have swapped the path for a symlink between the check above and now.
    if is_symlink(directory) {
        return Err(io::Error::other(format!(
            "{} is a symlink - refusing to follow it",
            directory.display()
        )));
    }
    let metadata = fs::metadata(directory)?;
    if !metadata.is_dir() {
        return Err(io::Error::other(format!(
            "{} is not a directory",
            directory.display()
        )));
    }
    if metadata.uid() != current_uid() {
        return Err(io::Error::other(format!(
            "{} is owned by uid {}, not this process's uid {}",
            directory.display(),
            metadata.uid(),
            current_uid()
        )));
    }
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

fn is_symlink(path: &Path) -> bool {
    fs::symlink_metadata(path)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::sync::Mutex;

    // `base_dir` reads process-wide env vars; serialize the tests that
    // mutate them so they cannot race each other under `cargo test`'s
    // default parallel execution.
    pub(crate) static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct EnvGuard(Vec<(&'static str, Option<std::ffi::OsString>)>);

    impl EnvGuard {
        fn capture(names: &[&'static str]) -> Self {
            let saved = names.iter().map(|&n| (n, env::var_os(n))).collect();
            for &n in names {
                env::remove_var(n);
            }
            Self(saved)
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (name, value) in &self.0 {
                match value {
                    Some(v) => env::set_var(name, v),
                    None => env::remove_var(name),
                }
            }
        }
    }

    fn scratch_dir(label: &str) -> PathBuf {
        let dir = env::temp_dir().join(format!(
            "blumkin-agent-basedir-test-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    // On macOS, `darwin_user_temp_dir()` always succeeds (it is the OS's own
    // per-user temp dir, not something a test can make disappear), so it
    // wins over every `TMPDIR`/`TEMP`/`TMP` candidate below - the fallback
    // order those two tests exercise only ever applies on a non-Darwin
    // Unix, or if `confstr(3)` itself somehow failed.
    #[cfg(target_os = "macos")]
    #[test]
    fn base_dir_ignores_tmpdir_on_macos_matching_the_python_client() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _guard = EnvGuard::capture(&["BLUMKIN_AGENT_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP"]);
        let unset_tmpdir_result = base_dir();

        // Issue #402's exact regression: a GUI/MCP-launched process with no
        // `TMPDIR` in its environment must resolve the *same* base dir as
        // one that does have `TMPDIR` set to something else - otherwise the
        // Python client and this spawned daemon disagree on the socket path.
        let decoy = scratch_dir("decoy-tmpdir");
        env::set_var("TMPDIR", &decoy);

        assert_eq!(base_dir(), unset_tmpdir_result);
        assert_ne!(base_dir(), decoy);

        fs::remove_dir_all(&decoy).ok();
    }

    #[test]
    fn base_dir_override_wins_over_darwin_confstr() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _guard = EnvGuard::capture(&["BLUMKIN_AGENT_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP"]);
        let overridden = scratch_dir("override");
        env::set_var("BLUMKIN_AGENT_RUNTIME_DIR", &overridden);

        assert_eq!(base_dir(), overridden);

        fs::remove_dir_all(&overridden).ok();
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn base_dir_prefers_tmpdir_over_temp_and_tmp() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _guard = EnvGuard::capture(&["BLUMKIN_AGENT_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP"]);
        let preferred = scratch_dir("preferred");
        let other = scratch_dir("other");
        env::set_var("TMPDIR", &preferred);
        env::set_var("TEMP", &other);

        assert_eq!(base_dir(), preferred);

        fs::remove_dir_all(&preferred).ok();
        fs::remove_dir_all(&other).ok();
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn base_dir_skips_a_nonexistent_tmpdir_and_falls_through_to_temp() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _guard = EnvGuard::capture(&["BLUMKIN_AGENT_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP"]);
        let fallback = scratch_dir("fallback");
        // A nonexistent leaf under the real system temp dir, not an
        // unwritable absolute path: `server.rs`'s tests run concurrently in
        // this same test binary and independently call
        // `std::env::temp_dir().join(..).create_dir_all()`, which re-reads
        // this same process-wide `TMPDIR`. A leaf here still lets those
        // `create_dir_all` calls succeed (the real temp dir's parent is
        // writable), unlike an unwritable root path, which would otherwise
        // make an unrelated concurrent test panic with `EACCES`.
        let missing = env::temp_dir().join("blumkin-agent-basedir-test-missing-leaf");
        env::set_var("TMPDIR", &missing);
        env::set_var("TEMP", &fallback);

        assert_eq!(base_dir(), fallback);

        fs::remove_dir_all(&fallback).ok();
    }
}
