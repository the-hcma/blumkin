# blumkin-agent (Rust)

The background daemon for issue #328: an `ssh-agent`/`gpg-agent`-style process that will hold time-boxed, decrypted secrets so `blumkin` only needs to re-verify local presence (Touch ID / device password) roughly once a day instead of on every command.

Not a published crate (`publish = false`) - this is a private implementation detail of the `blumkin` Python package. It is built and bundled automatically by `../hatch_build.py` (a `hatchling` build hook) whenever `blumkin` itself is installed on macOS with a Rust toolchain available; the compiled binary lands at `../src/blumkin/agent/bin/blumkin-agent`.

## Why Rust, not Python

`blumkin.agent.client` (Python) only needs a process on the other end of a Unix domain socket that speaks the wire protocol in `src/protocol.rs` (mirrored 1:1 in `blumkin.agent.protocol`); it never cares what language implements it. This daemon is compiled rather than a `blumkin.agent.server` Python module specifically because it will become the sole holder of decrypted secrets: a compiled binary makes it *possible* to give that in-memory secret real `mlock`/zeroing guarantees (guarantees Python's immutable-string/GC memory model cannot reliably provide at all), but Rust does not grant them automatically. The future secret-cache layer will still need to explicitly call `mlock`/zeroize the secret's memory (e.g. via the `memsec`/`zeroize` crates) and have that verified; this foundation build holds no secrets yet, so nothing here is locked or zeroized today. See issue #328's "Residual risk" section.

## Local development

```bash
cd rust-agent
cargo build          # debug build, fast iteration
cargo test
cargo clippy -- -D warnings
cargo fmt
```

Building a true universal2 (Apple Silicon + Intel) binary (what the release pipeline ships) needs both targets installed:

```bash
rustup target add aarch64-apple-darwin x86_64-apple-darwin
```

Without `rustup` (e.g. a bare `brew install rust`), `hatch_build.py` falls back to a native-arch-only build for local development and prints a warning; the release workflow's macOS leg (`.github/workflows/release-please.yml`, `build-dists`'s `macos-latest` matrix entry - lands alongside this foundation layer in issue #328's stacked PRs, not in this crate's own diff) installs both targets via `dtolnay/rust-toolchain`, so the actual published wheel gets a true universal2 binary.

## Keychain access (issue #773)

The agent is blumkin's keychain client. Besides the secret cache, protocol version 3 adds `keychain_get`, `keychain_set` and `keychain_delete`, which read, write and delete a generic-password item through Security.framework (the `security-framework` crate, macOS only; elsewhere there is no presence check to pass, so a request answers `presence_unsupported` before the keychain is reached). Only the service `blumkin` is served, so the agent is not a keychain proxy for other applications, and every request first needs a presence check: one that succeeded within the reverify window (the request's `ttl_seconds`, which may only shorten the cache TTL) lets it through without a prompt, otherwise Touch ID or the device password is asked for. A `lock` (one profile or all) ends the window. A keychain item's access list names binaries, so with the agent as the only client the identity macOS sees changes when blumkin is upgraded, not with every Python build.

## Protocol parity

There is no shared code between this crate and `blumkin.agent.protocol`/`paths.py` - both independently implement the same newline-delimited JSON framing and `BLUMKIN_AGENT_RUNTIME_DIR`-aware socket path resolution. Changing the wire format (`PROTOCOL_VERSION`) or the runtime-directory layout requires updating both sides.
