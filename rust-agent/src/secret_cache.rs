//! In-memory, TTL-gated, presence-verified secret cache (issue #328/#339).
//!
//! This is the core the later stack layers build on: PR2 wires new
//! `unlock`/`get_secret` protocol commands to [`SecretCache::unlock`] and
//! [`SecretCache::get`]; PR3 has `secret_store.py`/`auth.py` call those
//! commands instead of reading the keychain/file backend directly. Nothing
//! in *this* layer talks to protocol messages or the real keychain yet -
//! callers already have the plaintext secret bytes in hand (from wherever
//! a later layer fetched them) and are only asking this cache to gate and
//! time-box holding them in memory.
//!
//! Two properties, both required by issue #328's design:
//! - **Presence-gated**: [`SecretCache::unlock`] never caches anything
//!   without first getting an affirmative [`PresenceVerifier`] result (a
//!   real Touch ID/password prompt in production).
//! - **TTL-gated, on the agent's own clock**: [`SecretCache::get`] treats
//!   an entry older than `ttl` as gone - wiping it - rather than trusting
//!   Apple's own ~10 minute Touch-ID reuse window, which this daemon does
//!   not rely on at all (every re-verify after TTL expiry re-runs the full
//!   presence check).
//!
//! Secrets are held only as [`LockedSecret`] - `mlock(2)`ed best-effort and
//! zeroed on drop - per the "softer mitigations" v1 scope in issue #328
//! (code-signing/hardened-runtime, which would close the residual
//! same-UID-ptrace risk entirely, is an explicit v2 non-goal).

use std::collections::HashMap;
use std::ffi::c_void;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::presence::{PresenceError, PresenceVerifier, SystemPresenceVerifier};

/// Secret bytes held only for as long as they are cached, `mlock(2)`ed
/// best-effort for that duration, and always zeroed before the underlying
/// allocation is freed.
struct LockedSecret {
    bytes: Vec<u8>,
    mlocked: bool,
}

impl LockedSecret {
    fn new(bytes: Vec<u8>) -> Self {
        // Locking zero bytes is a no-op everywhere and some `mlock(2)`
        // implementations reject a zero-length range outright - treat an
        // empty secret as trivially "locked" rather than logging a bogus
        // failure for it.
        let mlocked = bytes.is_empty() || {
            // SAFETY: `bytes.as_ptr()` is valid for `bytes.len()` bytes for
            // the lifetime of this call, which is all `mlock(2)` requires.
            let result = unsafe { libc::mlock(bytes.as_ptr() as *const c_void, bytes.len()) };
            if result != 0 {
                // `mlock(2)` commonly fails once a process's `RLIMIT_MEMLOCK`
                // is exhausted. This is a best-effort hardening layer, not a
                // correctness requirement (see module docs) - degrade to an
                // unlocked-but-still-zeroed-on-drop buffer rather than
                // failing the whole unlock over it.
                eprintln!(
                    "blumkin-agent: mlock failed ({}); continuing without it (best-effort only)",
                    std::io::Error::last_os_error()
                );
            }
            result == 0
        };
        Self { bytes, mlocked }
    }

    fn as_slice(&self) -> &[u8] {
        &self.bytes
    }
}

impl Drop for LockedSecret {
    fn drop(&mut self) {
        secure_zero(&mut self.bytes);
        if self.mlocked && !self.bytes.is_empty() {
            // SAFETY: this is the same pointer/length that was
            // successfully `mlock`ed in `new`, only ever unlocked here.
            unsafe {
                libc::munlock(self.bytes.as_ptr() as *const c_void, self.bytes.len());
            }
        }
    }
}

/// A short-lived handle to a secret returned by [`SecretCache::get`] -
/// itself `mlock(2)`ed best-effort and zeroed on drop, exactly like the
/// copy resident in the cache, so a caller reading a secret out of the
/// cache cannot leave a second, unwiped plaintext copy on the heap once
/// this handle is dropped (see PR #340 review).
pub struct SecretHandle(LockedSecret);

impl std::ops::Deref for SecretHandle {
    type Target = [u8];

    fn deref(&self) -> &[u8] {
        self.0.as_slice()
    }
}

/// Overwrites `buf` with zeroes in a way the compiler cannot optimize away
/// as a dead store (unlike a plain `buf.fill(0)` on a buffer about to be
/// dropped, which an optimizer is free to elide entirely since nothing
/// reads it afterwards).
fn secure_zero(buf: &mut [u8]) {
    for byte in buf.iter_mut() {
        // SAFETY: `byte` is a valid, aligned reference for the duration of
        // this write.
        unsafe { std::ptr::write_volatile(byte, 0) };
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

struct CachedEntry {
    secret: LockedSecret,
    verified_at: Instant,
}

/// A presence-gated, TTL-bounded, per-profile secret cache.
#[allow(dead_code)] // wired to new `unlock`/`get_secret` protocol commands in PR2 (#339).
pub struct SecretCache {
    ttl: Duration,
    verifier: Box<dyn PresenceVerifier>,
    entries: Mutex<HashMap<String, CachedEntry>>,
}

#[allow(dead_code)] // wired to new `unlock`/`get_secret` protocol commands in PR2 (#339).
impl SecretCache {
    /// Builds a cache backed by the real OS presence check.
    pub fn new(ttl: Duration) -> Self {
        Self::with_verifier(ttl, Box::new(SystemPresenceVerifier))
    }

    pub fn with_verifier(ttl: Duration, verifier: Box<dyn PresenceVerifier>) -> Self {
        Self {
            ttl,
            verifier,
            entries: Mutex::new(HashMap::new()),
        }
    }

    /// Runs the presence check (`reason` is shown to the user) and, only on
    /// success, caches `secret` for `profile` - replacing any prior entry
    /// and (re)starting its TTL clock from now. On failure, nothing is
    /// cached and any prior entry for `profile` is left exactly as it was.
    pub fn unlock(
        &self,
        profile: &str,
        reason: &str,
        secret: Vec<u8>,
    ) -> Result<(), PresenceError> {
        // Wrap `secret` before verifying, not after: `LockedSecret::new`
        // takes ownership immediately, so on a `verify` failure (denial,
        // timeout, or an unsupported platform) the early return below
        // drops `locked_secret` - zeroing it via `Drop` - instead of
        // returning early on the bare `Vec<u8>`, whose bytes would
        // otherwise be freed unzeroed straight out of `unlock`'s caller
        // (see PR #340 review).
        let locked_secret = LockedSecret::new(secret);
        self.verifier.verify(reason)?;
        self.entries.lock().unwrap().insert(
            profile.to_string(),
            CachedEntry {
                secret: locked_secret,
                verified_at: Instant::now(),
            },
        );
        Ok(())
    }

    /// Returns a handle to the cached secret for `profile` if it exists
    /// and is still within `ttl`. An entry older than `ttl` is wiped
    /// (zeroed + `munlock`ed via `LockedSecret`'s `Drop`) and reported as
    /// absent - this cache never serves a secret past its own TTL,
    /// regardless of how recently the OS itself would still honor a cached
    /// Touch ID result. The returned [`SecretHandle`] is itself
    /// `mlock`ed/zeroed-on-drop, so the copy handed to the caller is wiped
    /// exactly like the one still resident in the cache, rather than
    /// leaking an unprotected second copy on the heap (see PR #340
    /// review).
    pub fn get(&self, profile: &str) -> Option<SecretHandle> {
        let mut entries = self.entries.lock().unwrap();
        let is_expired = entries
            .get(profile)
            .map(|entry| entry.verified_at.elapsed() > self.ttl)?;
        if is_expired {
            entries.remove(profile);
            return None;
        }
        entries
            .get(profile)
            .map(|entry| SecretHandle(LockedSecret::new(entry.secret.as_slice().to_vec())))
    }

    /// Wipes exactly one profile's cached secret, if any.
    pub fn lock(&self, profile: &str) {
        self.entries.lock().unwrap().remove(profile);
    }

    /// Wipes every cached secret - backs the daemon-wide `lock` command.
    pub fn lock_all(&self) {
        self.entries.lock().unwrap().clear();
    }

    /// Whether `profile` currently has *any* cached entry, expired or not -
    /// for tests only; production callers must go through [`Self::get`] so
    /// TTL expiry is always honored.
    #[cfg(test)]
    fn is_cached(&self, profile: &str) -> bool {
        self.entries.lock().unwrap().contains_key(profile)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::presence::tests::FakePresenceVerifier;
    use std::thread;

    fn cache_with(result: Result<(), PresenceError>, ttl: Duration) -> SecretCache {
        SecretCache::with_verifier(ttl, Box::new(FakePresenceVerifier::always(result)))
    }

    #[test]
    fn unlock_then_get_roundtrips_the_secret() {
        let cache = cache_with(Ok(()), Duration::from_secs(60));

        cache
            .unlock("work", "unlock the work profile cache", b"s3cr3t".to_vec())
            .unwrap();

        assert_eq!(cache.get("work").as_deref(), Some(b"s3cr3t".as_slice()));
    }

    #[test]
    fn unlock_passes_the_reason_through_to_the_presence_check_unchanged() {
        let verifier = std::sync::Arc::new(FakePresenceVerifier::always(Ok(())));
        let cache = SecretCache::with_verifier(Duration::from_secs(60), Box::new(verifier.clone()));

        cache
            .unlock("work", "unlock the work profile cache", b"x".to_vec())
            .unwrap();

        assert_eq!(
            verifier.reasons_seen(),
            vec!["unlock the work profile cache".to_string()]
        );
    }

    #[test]
    fn unlock_does_not_cache_anything_when_presence_is_denied() {
        let cache = cache_with(
            Err(PresenceError::Denied("wrong password".to_string())),
            Duration::from_secs(60),
        );

        let result = cache.unlock("work", "unlock the work profile cache", b"s3cr3t".to_vec());

        assert!(result.is_err());
        assert_eq!(cache.get("work").as_deref(), None);
        assert!(!cache.is_cached("work"));
    }

    #[test]
    fn unlock_failure_leaves_a_prior_entry_for_the_same_profile_untouched() {
        // A denial after an already-successful unlock must not clear the
        // existing, still-live cached secret - only a fresh success may
        // replace it (see PR #340 review).
        let verifier = FakePresenceVerifier::sequence(vec![
            Ok(()),
            Err(PresenceError::Denied("wrong password".to_string())),
        ]);
        let cache = SecretCache::with_verifier(Duration::from_secs(60), Box::new(verifier));
        cache.unlock("work", "unlock", b"first".to_vec()).unwrap();

        let result = cache.unlock("work", "unlock", b"second".to_vec());

        assert!(result.is_err());
        assert_eq!(cache.get("work").as_deref(), Some(b"first".as_slice()));
    }

    #[test]
    fn get_wipes_and_returns_none_once_the_ttl_has_elapsed() {
        let cache = cache_with(Ok(()), Duration::from_millis(20));
        cache.unlock("work", "unlock", b"s3cr3t".to_vec()).unwrap();
        assert!(cache.is_cached("work"));

        thread::sleep(Duration::from_millis(60));

        assert_eq!(cache.get("work").as_deref(), None);
        // The expired entry must be actually removed (and thus wiped via
        // `Drop`), not merely reported as absent while still resident.
        assert!(!cache.is_cached("work"));
    }

    #[test]
    fn lock_wipes_only_the_named_profile() {
        let cache = cache_with(Ok(()), Duration::from_secs(60));
        cache
            .unlock("work", "unlock", b"work-secret".to_vec())
            .unwrap();
        cache
            .unlock("home", "unlock", b"home-secret".to_vec())
            .unwrap();

        cache.lock("work");

        assert!(!cache.is_cached("work"));
        assert_eq!(
            cache.get("home").as_deref(),
            Some(b"home-secret".as_slice())
        );
    }

    #[test]
    fn lock_all_wipes_every_profile() {
        let cache = cache_with(Ok(()), Duration::from_secs(60));
        cache
            .unlock("work", "unlock", b"work-secret".to_vec())
            .unwrap();
        cache
            .unlock("home", "unlock", b"home-secret".to_vec())
            .unwrap();

        cache.lock_all();

        assert!(!cache.is_cached("work"));
        assert!(!cache.is_cached("home"));
    }

    #[test]
    fn unlock_replaces_a_prior_entry_and_resets_its_ttl_clock() {
        // Both the TTL and the margin between "did reset" and "did not
        // reset" are wide (2s TTL, 1.4s sleeps, so a bug that skips the
        // reset would only be caught after 2.8s total elapsed, well past
        // the 2s TTL) so this only fails if the clock genuinely was not
        // reset, never merely because a loaded CI runner overshot a tight
        // sleep window (see PR #340 review: the original 200ms TTL / 120ms
        // sleeps left only an ~80ms margin).
        let cache = cache_with(Ok(()), Duration::from_secs(2));
        cache.unlock("work", "unlock", b"first".to_vec()).unwrap();
        thread::sleep(Duration::from_millis(1400));

        // Re-unlocking must reset the TTL clock, not just replace the
        // bytes - otherwise a profile re-verified just before its old TTL
        // would expire could still be wiped by the *original* deadline.
        cache.unlock("work", "unlock", b"second".to_vec()).unwrap();
        thread::sleep(Duration::from_millis(1400));

        assert_eq!(cache.get("work").as_deref(), Some(b"second".as_slice()));
    }
}
