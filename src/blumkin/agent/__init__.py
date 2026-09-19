"""blumkin-agent: background process for time-boxed local secret access.

Foundation layer for issue #328 (a daily-ish local re-auth so an
already-unlocked laptop can't use blumkin's cached tokens forever). This
package provides the Python-side plumbing an `ssh-agent`-style daemon
needs - wire protocol (:mod:`blumkin.agent.protocol`), socket/path
resolution (:mod:`blumkin.agent.paths`), and the client that spawns/talks
to it (:mod:`blumkin.agent.client`).

The daemon itself is a separate, compiled Rust binary (`rust-agent/` at the
repo root, built by a `hatchling` build hook and bundled at
`blumkin/agent/bin/blumkin-agent`) rather than a Python module: it is the
sole holder of decrypted, time-boxed secrets once later layers land, and a
compiled binary gives that memory real `mlock`/zeroing guarantees Python's
string/GC model cannot (see issue #328's residual-risk discussion). No
secret material flows through any of this yet: the macOS
`LocalAuthentication` presence check and the actual keychain-backed secret
cache are later layers, built on top of this foundation once it is
independently tested and reviewed.
"""

from __future__ import annotations
