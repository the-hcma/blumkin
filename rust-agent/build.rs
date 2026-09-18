//! Tells cargo to re-run the build whenever the version/commit stamp env
//! vars change.
//!
//! `option_env!` (used in `src/version.rs`) is evaluated at compile time,
//! but cargo does not fingerprint arbitrary environment variables on its
//! own - without this build script, a repeat build in the same `target/`
//! directory with a changed `BLUMKIN_EMBED_VERSION`/`BLUMKIN_EMBED_COMMIT`
//! (the in-tree `uv sync`/`uv tool install -e .` dev workflow, or a reused
//! CI cache) reports the unit as fresh and reuses the previously compiled
//! binary, silently keeping the *old* stamp even though
//! `hatch_build.py::AgentBuildHook.initialize` re-copies what it thinks is
//! a freshly built binary (see PR #329 review).

fn main() {
    println!("cargo:rerun-if-env-changed=BLUMKIN_EMBED_VERSION");
    println!("cargo:rerun-if-env-changed=BLUMKIN_EMBED_COMMIT");
}
