//! Device-owner presence check (Touch ID or device password), gating access
//! to cached secrets per issue #328/#339.
//!
//! This is the "hard" mitigation layer: even a peer that has passed the
//! `SO_PEERCRED`/socket-permission checks and can talk to this agent still
//! cannot obtain a cached secret without the *actual device owner*
//! completing a fresh LocalAuthentication prompt (Touch ID, Face ID via
//! companion, or the device password) - this is not something a same-UID
//! process reading agent memory or replaying protocol messages can forge.
//!
//! Kept as its own module, behind the [`PresenceVerifier`] trait, so
//! [`crate::secret_cache::SecretCache`] can be exercised in `cargo test`
//! without ever prompting a real Touch ID dialog (impossible in CI, and
//! undesirable even on a developer machine running `cargo test` unattended).

use std::time::Duration;

/// How long a single presence check is allowed to wait on the user actually
/// responding to the Touch ID/password prompt before giving up - bounded so
/// a request that will never be answered (e.g. the user stepped away, or a
/// non-interactive `SSH_TTY`-less session with no way to show a prompt at
/// all) still gets a timely, clearly-a-timeout error instead of blocking
/// forever.
///
/// This is 24x `server::REQUEST_TIMEOUT` (5s, the daemon's own
/// single-connection budget) and 20x its spawn-probe budget (`
/// REQUEST_TIMEOUT + 1s` = 6s) - deliberately, because a real Touch
/// ID/password prompt can take much longer than either to answer than a
/// pure protocol round-trip. **[`PresenceVerifier::verify`] blocks the
/// calling thread for up to this long**, so whichever later layer wires it
/// into `server::dispatch` (PR2) must run it on a dedicated thread, never
/// directly on the single-threaded accept loop - otherwise a pending
/// prompt makes this agent look unresponsive to every other connection
/// within `REQUEST_TIMEOUT`, and a concurrently spawned second agent's
/// stale-socket probe (`server::agent_is_alive`) would then declare the
/// still-live (merely prompting) agent dead, delete its socket, and bind
/// over it (see PR #340 review).
pub const PRESENCE_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PresenceError {
    /// The user actively declined, canceled, or otherwise failed the
    /// presence check (wrong password, canceled dialog, etc).
    Denied(String),
    /// The presence check did not complete before [`PRESENCE_TIMEOUT`]
    /// elapsed - most likely nobody was present to respond to the prompt.
    TimedOut,
    /// This platform has no presence-check backend at all (v1 is
    /// macOS-only via `LocalAuthentication`; see issue #328's "Platform
    /// scope" section). Only ever constructed by the `not(target_os =
    /// "macos")` arm of `SystemPresenceVerifier::verify` below - on the
    /// macOS build this binary actually ships, nothing constructs it, so
    /// it would otherwise be flagged as dead code on that platform even
    /// though it is real, reachable API surface on every other one.
    #[allow(dead_code)]
    Unsupported,
}

impl std::fmt::Display for PresenceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PresenceError::Denied(message) => write!(f, "presence check failed: {message}"),
            PresenceError::TimedOut => write!(f, "presence check timed out"),
            PresenceError::Unsupported => {
                write!(f, "presence check is not supported on this platform")
            }
        }
    }
}

impl std::error::Error for PresenceError {}

/// A device-owner presence check, abstracted so tests can substitute a
/// deterministic fake for the real, interactive OS prompt.
pub trait PresenceVerifier: Send + Sync {
    /// Prompts for device-owner presence with `reason` shown to the user
    /// (mirrors `LAContext`'s own `localizedReason` requirement: it must be
    /// non-empty and describe *why* - e.g. "unlock the msal profile
    /// cache"). Blocks the calling thread until the user responds or
    /// [`PRESENCE_TIMEOUT`] elapses.
    fn verify(&self, reason: &str) -> Result<(), PresenceError>;
}

impl<T: PresenceVerifier + ?Sized> PresenceVerifier for std::sync::Arc<T> {
    fn verify(&self, reason: &str) -> Result<(), PresenceError> {
        (**self).verify(reason)
    }
}

/// The real, OS-backed presence check.
pub struct SystemPresenceVerifier;

impl PresenceVerifier for SystemPresenceVerifier {
    #[cfg(target_os = "macos")]
    fn verify(&self, reason: &str) -> Result<(), PresenceError> {
        macos::evaluate_device_owner_presence(reason)
    }

    #[cfg(not(target_os = "macos"))]
    fn verify(&self, _reason: &str) -> Result<(), PresenceError> {
        Err(PresenceError::Unsupported)
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use super::{PresenceError, PRESENCE_TIMEOUT};
    use std::sync::mpsc;

    use objc2_foundation::NSString;
    use objc2_local_authentication::{LAContext, LAPolicy};

    /// `kLAPolicyDeviceOwnerAuthentication`: Touch ID/Face ID *or* the
    /// device password, per issue #328's design ("Touch ID or device
    /// password"), rather than `...WithBiometrics`, which would hard-fail
    /// on a Mac with biometrics unavailable/disabled instead of falling
    /// back to the password prompt.
    const DEVICE_OWNER_AUTHENTICATION: LAPolicy = LAPolicy(2);

    pub(super) fn evaluate_device_owner_presence(reason: &str) -> Result<(), PresenceError> {
        // `evaluatePolicy:localizedReason:reply:` throws
        // `NSInvalidArgumentException` for an empty reason - fail closed
        // with a clear Rust-side error instead of letting that become an
        // uncaught Objective-C exception that aborts the whole daemon.
        if reason.trim().is_empty() {
            return Err(PresenceError::Denied(
                "a non-empty reason is required".to_string(),
            ));
        }

        let context = unsafe { LAContext::new() };
        let localized_reason = NSString::from_str(reason);
        let (tx, rx) = mpsc::channel::<Result<(), PresenceError>>();

        // The reply block runs asynchronously on a private queue internal
        // to LocalAuthentication.framework, not necessarily this thread -
        // `mpsc::Sender` (not a shared `&mut`) is what lets that queue hand
        // its result back to this function's blocking `recv_timeout` below
        // without any unsynchronized mutation across threads.
        let block = block2::RcBlock::new(
            move |success: objc2::runtime::Bool, error: *mut objc2_foundation::NSError| {
                let result = if success.as_bool() {
                    Ok(())
                } else {
                    // SAFETY: within the reply callback, `error` (when
                    // non-null) is a valid, live `NSError*` - reading its
                    // description here, before the callback returns and the
                    // object may be autoreleased, is what keeps this safe.
                    let message = unsafe { error.as_ref() }
                        .map(|error| error.localizedDescription().to_string())
                        .unwrap_or_else(|| "policy evaluation failed".to_string());
                    Err(PresenceError::Denied(message))
                };
                // The receiver may already be gone if `recv_timeout` below hit
                // `PRESENCE_TIMEOUT` first - a send to a disconnected channel
                // is a no-op error we deliberately ignore rather than let an
                // unrelated panic unwind out of an Objective-C callback frame.
                let _ = tx.send(result);
            },
        );

        unsafe {
            context.evaluatePolicy_localizedReason_reply(
                DEVICE_OWNER_AUTHENTICATION,
                &localized_reason,
                &block,
            );
        }

        rx.recv_timeout(PRESENCE_TIMEOUT)
            .unwrap_or(Err(PresenceError::TimedOut))
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;

    /// A [`PresenceVerifier`] whose outcome is fixed (or queued) at
    /// construction time, so [`crate::secret_cache::SecretCache`] tests
    /// never touch the real, interactive OS prompt.
    pub struct FakePresenceVerifier {
        results: Mutex<VecDeque<Result<(), PresenceError>>>,
        calls: Mutex<Vec<String>>,
    }

    impl FakePresenceVerifier {
        /// Returns `result` from every `verify` call.
        pub fn always(result: Result<(), PresenceError>) -> Self {
            Self::sequence(vec![result])
        }

        /// Returns each queued result in order, one per `verify` call - the
        /// final queued result repeats for any calls beyond the queue's
        /// length, so a test asserting "then it stays denied" does not need
        /// to size the queue to an exact call count.
        pub fn sequence(results: Vec<Result<(), PresenceError>>) -> Self {
            assert!(!results.is_empty(), "sequence needs at least one result");
            Self {
                results: Mutex::new(results.into()),
                calls: Mutex::new(Vec::new()),
            }
        }

        pub fn reasons_seen(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl PresenceVerifier for FakePresenceVerifier {
        fn verify(&self, reason: &str) -> Result<(), PresenceError> {
            self.calls.lock().unwrap().push(reason.to_string());
            let mut results = self.results.lock().unwrap();
            if results.len() > 1 {
                results.pop_front().unwrap()
            } else {
                results.front().unwrap().clone()
            }
        }
    }
}
