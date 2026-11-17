//! The agent's own access to the macOS keychain (issue #773).
//!
//! A keychain item's access list names the *binaries* allowed to use it, and an ad-hoc signed
//! Python is a new binary after every update, so every process that read the keychain itself
//! raised a "wants to use your confidential information" dialog. With the agent as the only
//! keychain client, the identity macOS sees is this binary, which changes only when blumkin
//! itself is upgraded.
//!
//! This module is the thin edge: a [`KeychainBackend`] that gets, sets and deletes a generic
//! password, a real implementation over Security.framework (macOS only), and nothing else. What
//! may be asked of it (only the `blumkin` service, only after a presence check) is decided by
//! the callers in `server.rs` and `secret_cache.rs`.

use std::fmt;

/// `errSecItemNotFound`: no item matched. Not a failure for a read or a delete.
#[cfg(target_os = "macos")]
const ERR_SEC_ITEM_NOT_FOUND: i32 = -25300;

#[derive(Debug)]
pub enum KeychainError {
    /// This platform has no keychain (only constructed off macOS).
    #[cfg_attr(target_os = "macos", allow(dead_code))]
    Unsupported,
    /// The keychain refused or failed; `code` is the OS status.
    Failed { code: i32, message: String },
}

impl fmt::Display for KeychainError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeychainError::Unsupported => write!(f, "this platform has no keychain"),
            KeychainError::Failed { code, message } => {
                write!(f, "the keychain failed (status {code}): {message}")
            }
        }
    }
}

impl std::error::Error for KeychainError {}

/// What the agent needs of the keychain, behind a trait so tests never touch the real one.
pub trait KeychainBackend: Send + Sync {
    /// The item's value, or `None` when there is no such item.
    fn get(&self, service: &str, account: &str) -> Result<Option<Vec<u8>>, KeychainError>;
    /// Create or replace the item.
    fn set(&self, service: &str, account: &str, value: &[u8]) -> Result<(), KeychainError>;
    /// Remove the item; true when one was removed, false when there was none.
    fn delete(&self, service: &str, account: &str) -> Result<bool, KeychainError>;
}

impl<T: KeychainBackend + ?Sized> KeychainBackend for std::sync::Arc<T> {
    fn get(&self, service: &str, account: &str) -> Result<Option<Vec<u8>>, KeychainError> {
        (**self).get(service, account)
    }

    fn set(&self, service: &str, account: &str, value: &[u8]) -> Result<(), KeychainError> {
        (**self).set(service, account, value)
    }

    fn delete(&self, service: &str, account: &str) -> Result<bool, KeychainError> {
        (**self).delete(service, account)
    }
}

/// The real login keychain, through Security.framework.
pub struct SystemKeychain;

#[cfg(target_os = "macos")]
impl KeychainBackend for SystemKeychain {
    fn get(&self, service: &str, account: &str) -> Result<Option<Vec<u8>>, KeychainError> {
        match security_framework::passwords::get_generic_password(service, account) {
            Ok(value) => Ok(Some(value)),
            Err(err) if err.code() == ERR_SEC_ITEM_NOT_FOUND => Ok(None),
            Err(err) => Err(failed(err)),
        }
    }

    fn set(&self, service: &str, account: &str, value: &[u8]) -> Result<(), KeychainError> {
        security_framework::passwords::set_generic_password(service, account, value).map_err(failed)
    }

    fn delete(&self, service: &str, account: &str) -> Result<bool, KeychainError> {
        match security_framework::passwords::delete_generic_password(service, account) {
            Ok(()) => Ok(true),
            Err(err) if err.code() == ERR_SEC_ITEM_NOT_FOUND => Ok(false),
            Err(err) => Err(failed(err)),
        }
    }
}

#[cfg(target_os = "macos")]
fn failed(err: security_framework::base::Error) -> KeychainError {
    KeychainError::Failed {
        code: err.code(),
        message: err.message().unwrap_or_default(),
    }
}

#[cfg(not(target_os = "macos"))]
impl KeychainBackend for SystemKeychain {
    fn get(&self, _service: &str, _account: &str) -> Result<Option<Vec<u8>>, KeychainError> {
        Err(KeychainError::Unsupported)
    }

    fn set(&self, _service: &str, _account: &str, _value: &[u8]) -> Result<(), KeychainError> {
        Err(KeychainError::Unsupported)
    }

    fn delete(&self, _service: &str, _account: &str) -> Result<bool, KeychainError> {
        Err(KeychainError::Unsupported)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// An in-memory keychain that records how it was called.
    #[derive(Default)]
    pub struct FakeKeychain {
        items: Mutex<HashMap<(String, String), Vec<u8>>>,
        failure: Mutex<Option<(i32, String)>>,
    }

    impl FakeKeychain {
        pub fn with_item(service: &str, account: &str, value: &[u8]) -> Self {
            let fake = Self::default();
            fake.items
                .lock()
                .unwrap()
                .insert((service.to_string(), account.to_string()), value.to_vec());
            fake
        }

        pub fn failing(code: i32, message: &str) -> Self {
            let fake = Self::default();
            *fake.failure.lock().unwrap() = Some((code, message.to_string()));
            fake
        }

        pub fn value(&self, service: &str, account: &str) -> Option<Vec<u8>> {
            self.items
                .lock()
                .unwrap()
                .get(&(service.to_string(), account.to_string()))
                .cloned()
        }

        fn check(&self) -> Result<(), KeychainError> {
            match &*self.failure.lock().unwrap() {
                Some((code, message)) => Err(KeychainError::Failed {
                    code: *code,
                    message: message.clone(),
                }),
                None => Ok(()),
            }
        }
    }

    impl KeychainBackend for FakeKeychain {
        fn get(&self, service: &str, account: &str) -> Result<Option<Vec<u8>>, KeychainError> {
            self.check()?;
            Ok(self.value(service, account))
        }

        fn set(&self, service: &str, account: &str, value: &[u8]) -> Result<(), KeychainError> {
            self.check()?;
            self.items
                .lock()
                .unwrap()
                .insert((service.to_string(), account.to_string()), value.to_vec());
            Ok(())
        }

        fn delete(&self, service: &str, account: &str) -> Result<bool, KeychainError> {
            self.check()?;
            Ok(self
                .items
                .lock()
                .unwrap()
                .remove(&(service.to_string(), account.to_string()))
                .is_some())
        }
    }

    #[test]
    fn the_fake_round_trips_and_reports_a_missing_item() {
        let fake = FakeKeychain::default();
        assert!(fake.get("blumkin", "a").unwrap().is_none());
        fake.set("blumkin", "a", b"v").unwrap();
        assert_eq!(fake.get("blumkin", "a").unwrap(), Some(b"v".to_vec()));
        assert!(fake.delete("blumkin", "a").unwrap());
        assert!(!fake.delete("blumkin", "a").unwrap());
    }
}
