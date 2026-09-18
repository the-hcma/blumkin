//! Build metadata embedded into the compiled agent binary.
//!
//! Mirrors `blumkin.version`'s embedding pattern: the hatchling build hook
//! that invokes `cargo build` passes the *same* `BLUMKIN_EMBED_VERSION` /
//! `BLUMKIN_EMBED_COMMIT` environment variables it already exports for
//! `scripts/embed_build_metadata`, so `blumkin agent status` always reports
//! a version/commit that matches the Python package it shipped with - never
//! a separately drifting Cargo crate version.
//!
//! A bare `cargo build` outside that hook (local development) still works:
//! it falls back to `CARGO_PKG_VERSION` and `"unknown"` rather than failing
//! to compile, so `cargo test`/`cargo run` do not require the full Python
//! build pipeline.

/// `option_env!` is evaluated at compile time, so a dev build genuinely run
/// without the hook's env vars set falls back here rather than failing.
pub fn package_version() -> &'static str {
    option_env!("BLUMKIN_EMBED_VERSION").unwrap_or(env!("CARGO_PKG_VERSION"))
}

pub fn source_commit() -> &'static str {
    option_env!("BLUMKIN_EMBED_COMMIT").unwrap_or("unknown")
}
