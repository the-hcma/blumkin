//! In-memory, TTL-gated, presence-verified secret cache (issue #328/#339).
//!
//! This is the core `server.rs`'s `unlock`/`get_secret` protocol commands
//! are wired to (via [`SecretCache::unlock`] and [`SecretCache::get`]); a
//! later layer (PR3) has `secret_store.py`/`auth.py` call those commands
//! instead of reading the keychain/file backend directly. Nothing in
//! *this* layer talks to protocol messages or the real keychain itself -
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
use std::sync::{Arc, Condvar, Mutex};
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

/// Per-profile generation bookkeeping guarding against a slow `unlock`
/// committing after it has been superseded.
///
/// `unlock`'s presence check can block for up to
/// [`crate::presence::PRESENCE_TIMEOUT`] (120s); `SecretCache` is `Send +
/// Sync` and meant to be shared across concurrent request handlers, so a
/// second `unlock`/`lock`/`lock_all` can start and finish for the same
/// profile while an earlier `unlock`'s presence check is still pending.
/// Without this bookkeeping, that earlier call would commit its
/// (now-stale) secret afterward, silently overwriting or resurrecting
/// whatever the newer, already-completed operation left behind (see PR
/// #340 review).
struct CacheGenerations {
    next: u64,
    by_profile: HashMap<String, u64>,
}

impl CacheGenerations {
    fn new() -> Self {
        Self {
            next: 0,
            by_profile: HashMap::new(),
        }
    }

    /// Records a fresh generation as the most recent operation to *start*
    /// for `profile`, returning it so the caller can later confirm via
    /// [`Self::is_current`] that no newer operation has started since.
    fn begin(&mut self, profile: &str) -> u64 {
        self.next += 1;
        self.by_profile.insert(profile.to_string(), self.next);
        self.next
    }

    /// Whether `generation` (from an earlier [`Self::begin`] call) is
    /// still the most recent operation recorded for `profile` - `false`
    /// means a newer `unlock`/`lock`/`lock_all` has started since.
    fn is_current(&self, profile: &str, generation: u64) -> bool {
        self.by_profile.get(profile) == Some(&generation)
    }

    /// Bumps every profile with any recorded generation - used by
    /// `lock_all` so no in-flight `unlock`, for any profile, can commit
    /// after it. A profile with a currently in-flight `unlock` is always
    /// already present here: `begin` runs synchronously, under this same
    /// lock, before that `unlock`'s (possibly long) presence check even
    /// starts.
    fn invalidate_all(&mut self) {
        let profiles: Vec<String> = self.by_profile.keys().cloned().collect();
        for profile in profiles {
            self.begin(&profile);
        }
    }
}

/// A presence check for one profile, shared by every `unlock` call
/// concurrent with the one actually running it - see
/// [`SecretCache::verify_presence_once`].
struct PresenceCheckShare {
    result: Mutex<Option<Result<(), PresenceError>>>,
    done: Condvar,
}

/// A presence-gated, TTL-bounded, per-profile secret cache.
pub struct SecretCache {
    ttl: Duration,
    verifier: Box<dyn PresenceVerifier>,
    entries: Mutex<HashMap<String, CachedEntry>>,
    generations: Mutex<CacheGenerations>,
    // Presence checks currently running, keyed by profile - lets a second,
    // concurrent `unlock` for the *same* profile share the one prompt
    // already in flight (do it once, confirm, move on) instead of starting
    // a redundant `evaluatePolicy` of its own, which macOS would cancel
    // the first prompt for (issue #343). A profile's entry is removed as
    // soon as its one real check finishes.
    presence_inflight: Mutex<HashMap<String, Arc<PresenceCheckShare>>>,
    // Held (via `try_lock`, never a blocking `lock`) for the duration of
    // the *one* leader's real `verify` call, across all profiles: two
    // genuinely different profiles unlocked at the same moment would
    // otherwise each start their own `evaluatePolicy` and cancel one
    // another. A leader that cannot acquire this immediately fails fast
    // with `PresenceError::Busy` rather than queuing (see
    // `verify_presence_once`'s docs for why waiting is never bounded
    // safely here).
    presence_lock: Mutex<()>,
}

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
            generations: Mutex::new(CacheGenerations::new()),
            presence_inflight: Mutex::new(HashMap::new()),
            presence_lock: Mutex::new(()),
        }
    }

    /// Runs the presence check for `profile` exactly once even if several
    /// `unlock` calls for it arrive concurrently: the first caller in
    /// becomes the "leader" and actually runs `verifier.verify`; every
    /// other concurrent caller for the same profile is a "follower" that
    /// blocks on the leader's own result and reuses it verbatim, rather
    /// than starting a second, redundant prompt (issue #343) - there is
    /// never more than one real `evaluatePolicy` per profile in flight,
    /// and a same-profile follower never waits any longer than the one
    /// real check takes (bounded by [`crate::presence::PRESENCE_TIMEOUT`]).
    ///
    /// A leader for a *different* profile never queues behind another
    /// profile's in-flight check: `presence_lock` is only ever
    /// `try_lock`ed, never blockingly `lock`ed, so a leader that loses the
    /// race fails fast with [`PresenceError::Busy`] instead of waiting an
    /// unbounded amount of time (which could exceed `client.py`'s own
    /// recv timeout for `unlock` and be reported as an unreachable agent -
    /// see PR review on #343's fix) - the only bound this cache ever
    /// enforces cross-profile is "at most one real prompt at a time",
    /// never "wait your turn".
    fn verify_presence_once(&self, profile: &str, reason: &str) -> Result<(), PresenceError> {
        let share = {
            let mut inflight = self.presence_inflight.lock().unwrap();
            if let Some(existing) = inflight.get(profile) {
                (Arc::clone(existing), false)
            } else {
                let share = Arc::new(PresenceCheckShare {
                    result: Mutex::new(None),
                    done: Condvar::new(),
                });
                inflight.insert(profile.to_string(), Arc::clone(&share));
                (share, true)
            }
        };
        let (share, is_leader) = share;

        if !is_leader {
            let mut result = share.result.lock().unwrap();
            while result.is_none() {
                result = share.done.wait(result).unwrap();
            }
            return result.clone().unwrap();
        }

        // Only the leader ever calls the real verifier - and only if no
        // other profile's leader is already running one; see this
        // method's docs for why this is `try_lock`, not `lock`.
        let result = match self.presence_lock.try_lock() {
            Ok(_guard) => self.verifier.verify(reason),
            Err(std::sync::TryLockError::WouldBlock) => Err(PresenceError::Busy),
            Err(std::sync::TryLockError::Poisoned(poisoned)) => {
                // A prior presence check panicked mid-flight - still
                // fail this one closed rather than let a poisoned lock
                // propagate as an ambiguous panic here too.
                drop(poisoned);
                Err(PresenceError::Busy)
            }
        };
        *share.result.lock().unwrap() = Some(result.clone());
        share.done.notify_all();
        self.presence_inflight.lock().unwrap().remove(profile);
        result
    }

    /// Runs the presence check (`reason` is shown to the user) and, only on
    /// success, caches `secret` for `profile` - replacing any prior entry
    /// and (re)starting its TTL clock from now. On failure, nothing is
    /// cached and any prior entry for `profile` is left exactly as it was.
    ///
    /// The presence check itself is deduplicated per-profile via
    /// [`Self::verify_presence_once`]: a second, concurrent `unlock` for
    /// the same profile never starts a redundant prompt of its own.
    ///
    /// If a newer `unlock`/`lock`/`lock_all` call starts (and, for
    /// `unlock`, finishes) for this same `profile` while this call's
    /// presence check is still pending, this call's result is discarded
    /// once its (now-stale) presence check completes - it neither commits
    /// its secret nor reports an error, since the presence check itself
    /// may well have succeeded; it was simply superseded (see PR #340
    /// review, and [`CacheGenerations`]'s docs for why this can happen at
    /// all).
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
        let generation = self.generations.lock().unwrap().begin(profile);
        self.verify_presence_once(profile, reason)?;
        // `generations` is held across *both* the `is_current` check and
        // the `entries` insert below, not just the check - `lock`/
        // `lock_all` take this same lock before touching `entries`
        // themselves, so holding it here too closes the window where a
        // `lock`/`lock_all` that starts and finishes strictly between
        // "checked current" and "inserted" would otherwise let this
        // now-stale `unlock` re-cache a secret an operator was just told
        // was wiped (see PR #341 review).
        let generations = self.generations.lock().unwrap();
        if !generations.is_current(profile, generation) {
            // Superseded while `verify` was pending - `locked_secret` is
            // dropped (and zeroed) here without ever being committed.
            return Ok(());
        }
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
    /// Lists every profile with a still-live (non-expired) cached secret -
    /// backs `status`'s `cached_profiles` field. Expired entries are wiped
    /// as a side effect of checking them here, same as [`Self::get`],
    /// rather than reported as live and then silently expiring the moment
    /// a caller actually tries to [`Self::get`] them.
    pub fn cached_profiles(&self) -> Vec<String> {
        let mut entries = self.entries.lock().unwrap();
        let expired: Vec<String> = entries
            .iter()
            .filter(|(_, entry)| entry.verified_at.elapsed() > self.ttl)
            .map(|(profile, _)| profile.clone())
            .collect();
        for profile in &expired {
            entries.remove(profile);
        }
        let mut profiles: Vec<String> = entries.keys().cloned().collect();
        profiles.sort();
        profiles
    }

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

    /// Wipes exactly one profile's cached secret, if any. Also bumps its
    /// generation *before* removing it - and holds `generations` locked
    /// across both steps - so a still-pending `unlock` for the same
    /// profile can never commit either before or after this call (see
    /// [`CacheGenerations`]'s docs and the race this closes in
    /// [`Self::unlock`]'s own docs).
    pub fn lock(&self, profile: &str) {
        let mut generations = self.generations.lock().unwrap();
        generations.begin(profile);
        self.entries.lock().unwrap().remove(profile);
    }

    /// Wipes every cached secret - backs the daemon-wide `lock` command.
    /// Also bumps every profile's generation (see
    /// [`CacheGenerations::invalidate_all`]) while `generations` stays
    /// locked across both steps, so no `unlock` in flight for any profile
    /// can commit either before or after this call.
    pub fn lock_all(&self) {
        let mut generations = self.generations.lock().unwrap();
        generations.invalidate_all();
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

    #[test]
    fn concurrent_unlocks_for_the_same_profile_share_one_presence_check() {
        use crate::presence::tests::GatedPresenceVerifier;
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

        /// Wraps `GatedPresenceVerifier` (whose first call blocks until
        /// released) to also count how many times `verify` is actually
        /// entered - proving a concurrent, same-profile `unlock` never
        /// triggers its own redundant presence check (issue #343).
        struct CountingVerifier {
            inner: GatedPresenceVerifier,
            calls: Arc<AtomicUsize>,
        }

        impl PresenceVerifier for CountingVerifier {
            fn verify(&self, reason: &str) -> Result<(), PresenceError> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                self.inner.verify(reason)
            }
        }

        let calls = Arc::new(AtomicUsize::new(0));
        let (gated, release) = GatedPresenceVerifier::new();
        let verifier = CountingVerifier {
            inner: gated,
            calls: Arc::clone(&calls),
        };
        let cache = Arc::new(SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(verifier),
        ));

        let leader_cache = Arc::clone(&cache);
        let leader =
            thread::spawn(move || leader_cache.unlock("work", "unlock", b"secret".to_vec()));

        // Best-effort: give the leader time to enter `verify` (and block on
        // the gate) before the follower starts - see the sibling tests
        // above for why this is only best-effort, not a strict guarantee.
        thread::sleep(Duration::from_millis(50));

        let follower_cache = Arc::clone(&cache);
        let follower =
            thread::spawn(move || follower_cache.unlock("work", "unlock", b"secret".to_vec()));

        thread::sleep(Duration::from_millis(50));
        release.send(()).unwrap();

        leader.join().unwrap().unwrap();
        follower.join().unwrap().unwrap();

        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "a concurrent unlock for the same profile must not start its own presence check"
        );
        assert_eq!(cache.get("work").as_deref(), Some(b"secret".as_slice()));
    }

    #[test]
    fn concurrent_unlocks_for_the_same_profile_share_a_denied_presence_result() {
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::mpsc;
        use std::sync::Arc;

        /// Like `GatedPresenceVerifier`, but the first call returns a
        /// caller-supplied `Err` once released, instead of always
        /// `Ok(())` - proves a follower reuses the leader's *failure*,
        /// not just its success (see PR review on #343's fix: a follower
        /// that ignored the shared result and returned `Ok(())`
        /// unconditionally would still pass a success-only assertion).
        struct GatedDenyingVerifier {
            release: Mutex<Option<mpsc::Receiver<()>>>,
            first_call_started: AtomicBool,
        }

        impl GatedDenyingVerifier {
            fn new() -> (Self, mpsc::Sender<()>) {
                let (sender, receiver) = mpsc::channel();
                (
                    Self {
                        release: Mutex::new(Some(receiver)),
                        first_call_started: AtomicBool::new(false),
                    },
                    sender,
                )
            }
        }

        impl PresenceVerifier for GatedDenyingVerifier {
            fn verify(&self, _reason: &str) -> Result<(), PresenceError> {
                let is_first_call = !self.first_call_started.swap(true, Ordering::SeqCst);
                if is_first_call {
                    if let Some(receiver) = self.release.lock().unwrap().take() {
                        let _ = receiver.recv();
                    }
                }
                Err(PresenceError::Denied("wrong password".to_string()))
            }
        }

        let (verifier, release) = GatedDenyingVerifier::new();
        let cache = Arc::new(SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(verifier),
        ));

        let leader_cache = Arc::clone(&cache);
        let leader =
            thread::spawn(move || leader_cache.unlock("work", "unlock", b"secret".to_vec()));

        thread::sleep(Duration::from_millis(50));

        let follower_cache = Arc::clone(&cache);
        let follower =
            thread::spawn(move || follower_cache.unlock("work", "unlock", b"secret".to_vec()));

        thread::sleep(Duration::from_millis(50));
        release.send(()).unwrap();

        let expected = Err(PresenceError::Denied("wrong password".to_string()));
        assert_eq!(leader.join().unwrap(), expected);
        assert_eq!(follower.join().unwrap(), expected);
        assert!(cache.get("work").is_none());
    }

    #[test]
    fn a_different_profiles_unlock_fails_fast_instead_of_queueing_behind_another() {
        use crate::presence::tests::GatedPresenceVerifier;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::mpsc;
        use std::sync::Arc;

        /// Wraps `GatedPresenceVerifier` to also signal, via `entered`,
        /// the exact moment `verify` is actually entered (and
        /// `presence_lock` is therefore held) - a deterministic
        /// happens-before instead of a fixed sleep, so "work" is
        /// guaranteed to have already become leader before "home" tries
        /// to unlock (see PR #345 review: a losing race would otherwise
        /// make *this test* the one blocked on `GatedPresenceVerifier`'s
        /// gate).
        struct SignalingVerifier {
            inner: GatedPresenceVerifier,
            entered: Arc<AtomicBool>,
        }

        impl PresenceVerifier for SignalingVerifier {
            fn verify(&self, reason: &str) -> Result<(), PresenceError> {
                self.entered.store(true, Ordering::SeqCst);
                self.inner.verify(reason)
            }
        }

        let entered = Arc::new(AtomicBool::new(false));
        let (gated, release) = GatedPresenceVerifier::new();
        let verifier = SignalingVerifier {
            inner: gated,
            entered: Arc::clone(&entered),
        };
        let cache = Arc::new(SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(verifier),
        ));

        let work_cache = Arc::clone(&cache);
        let work_unlock =
            thread::spawn(move || work_cache.unlock("work", "unlock", b"work-secret".to_vec()));

        let deadline = Instant::now() + Duration::from_secs(5);
        while !entered.load(Ordering::SeqCst) {
            assert!(
                Instant::now() < deadline,
                "work's presence check never started"
            );
            thread::sleep(Duration::from_millis(1));
        }

        // Also on its own thread, received with a bounded timeout rather
        // than joined outright: a concurrent unlock for a *different*
        // profile must not block waiting its turn - it fails fast with
        // `Busy` instead (see `verify_presence_once`'s docs on why an
        // unbounded cross-profile queue could exceed `client.py`'s own
        // recv timeout for `unlock`) - and if that guarantee ever
        // regressed, this reports a clean assertion failure rather than
        // hanging the test (see PR #345 review).
        let home_cache = Arc::clone(&cache);
        let (home_tx, home_rx) = mpsc::channel();
        thread::spawn(move || {
            let _ = home_tx.send(home_cache.unlock("home", "unlock", b"home-secret".to_vec()));
        });
        let home_result = home_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("a different profile's unlock must return quickly with Busy, not block");
        assert_eq!(home_result, Err(PresenceError::Busy));

        release.send(()).unwrap();
        work_unlock.join().unwrap().unwrap();

        assert_eq!(
            cache.get("work").as_deref(),
            Some(b"work-secret".as_slice())
        );
        assert!(cache.get("home").is_none());
    }

    fn cache_with(result: Result<(), PresenceError>, ttl: Duration) -> SecretCache {
        SecretCache::with_verifier(ttl, Box::new(FakePresenceVerifier::always(result)))
    }

    #[test]
    fn a_slow_unlock_does_not_overwrite_a_secret_committed_by_a_newer_unlock() {
        use crate::presence::tests::GatedPresenceVerifier;
        use std::sync::Arc;

        let (verifier, release) = GatedPresenceVerifier::new();
        let cache = Arc::new(SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(verifier),
        ));

        let slow_cache = Arc::clone(&cache);
        let slow_unlock =
            thread::spawn(move || slow_cache.unlock("work", "unlock", b"stale".to_vec()));

        // See the sibling test above for why this sleep is only
        // best-effort, not a strict happens-before guarantee.
        thread::sleep(Duration::from_millis(50));

        // Also on its own thread, not the main one: a concurrent unlock
        // for the *same* profile now shares the one presence check already
        // in flight (see `SecretCache::verify_presence_once`) rather than
        // running its own, so it blocks until `release` fires too - only
        // the *order the two calls began* (captured by `generations`
        // before either one blocks on the shared check), not which one
        // happens to finish first, decides which secret ultimately wins.
        let fresh_cache = Arc::clone(&cache);
        let fresh_unlock =
            thread::spawn(move || fresh_cache.unlock("work", "unlock", b"fresh".to_vec()));

        thread::sleep(Duration::from_millis(50));
        release.send(()).unwrap();

        slow_unlock.join().unwrap().unwrap();
        fresh_unlock.join().unwrap().unwrap();

        // The stale unlock's presence check succeeded too (shared with the
        // fresh one, coalesced into a single real check), but it began
        // before the fresh unlock did - the stale value must not overwrite
        // the fresher one that began after it.
        assert_eq!(cache.get("work").as_deref(), Some(b"fresh".as_slice()));
    }

    #[test]
    fn a_slow_unlock_does_not_resurrect_a_profile_a_concurrent_lock_already_wiped() {
        use crate::presence::tests::GatedPresenceVerifier;
        use std::sync::Arc;

        let (verifier, release) = GatedPresenceVerifier::new();
        let cache = Arc::new(SecretCache::with_verifier(
            Duration::from_secs(60),
            Box::new(verifier),
        ));

        let slow_cache = Arc::clone(&cache);
        let slow_unlock =
            thread::spawn(move || slow_cache.unlock("work", "unlock", b"stale".to_vec()));

        // Best-effort: give the spawned unlock time to enter `verify` and
        // block on the gate before racing `lock` in ahead of it - there is
        // no observable "now blocked" signal short of a short sleep.
        thread::sleep(Duration::from_millis(50));
        cache.lock("work");

        release.send(()).unwrap();
        slow_unlock.join().unwrap().unwrap();

        // The slow unlock's presence check did succeed, but by the time it
        // went to commit, `lock` had already superseded it - its secret
        // must not resurrect the profile `lock` just wiped (see PR #340
        // review).
        assert_eq!(cache.get("work").as_deref(), None);
    }

    #[test]
    fn cached_profiles_lists_only_still_live_profiles_sorted() {
        let cache = cache_with(Ok(()), Duration::from_secs(60));
        cache
            .unlock("work", "unlock", b"work-secret".to_vec())
            .unwrap();
        cache
            .unlock("aaa", "unlock", b"aaa-secret".to_vec())
            .unwrap();

        assert_eq!(cache.cached_profiles(), vec!["aaa", "work"]);
    }

    #[test]
    fn cached_profiles_omits_and_wipes_an_expired_entry() {
        let cache = cache_with(Ok(()), Duration::from_millis(50));
        cache
            .unlock("work", "unlock", b"work-secret".to_vec())
            .unwrap();
        thread::sleep(Duration::from_millis(120));

        let profiles = cache.cached_profiles();

        assert!(profiles.is_empty());
        assert!(!cache.is_cached("work"));
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

    #[test]
    fn unlock_then_get_roundtrips_the_secret() {
        let cache = cache_with(Ok(()), Duration::from_secs(60));

        cache
            .unlock("work", "unlock the work profile cache", b"s3cr3t".to_vec())
            .unwrap();

        assert_eq!(cache.get("work").as_deref(), Some(b"s3cr3t".as_slice()));
    }
}
