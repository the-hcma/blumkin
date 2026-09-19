"""Hatchling build hook: compiles `rust-agent/` and bundles the binary.

Runs on both `build_editable` (`uv tool install -e .`, this repo's own dev
workflow) and `build_wheel` (`pip install .`, `pipx install`, the PyPI
release job). The compiled `blumkin-agent` daemon (issue #328) is the sole
holder of decrypted, time-boxed secrets once later layers land - a
compiled binary gives that memory real `mlock`/zeroing guarantees Python's
string/GC model cannot (see issue #328's residual-risk discussion for why
this is a Rust binary rather than a `blumkin.agent.server` Python module).

Degrades gracefully rather than failing the install: on a non-macOS
platform, or if `cargo` is not on `PATH`, the wheel simply ships without
the binary. `blumkin.agent.paths.binary_path()` then points at a file that
does not exist, and `blumkin.agent.client._spawn` raises a clear
`AgentUnavailableError` the first time something tries to use the agent -
the rest of blumkin (direct keychain/file secret storage) is unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: `rust-agent/` targets macOS only for now (the next layer on top of this
#: foundation, `LocalAuthentication`, is a macOS-only API) - see the
#: "Platform support" section of issue #328.
_SUPPORTED_DARWIN_TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin")

#: hatchling's own `infer_tag` machinery (`get_best_matching_tag` ->
#: `process_macos_plat_tag`) reads this exact env var to recognize a
#: universal2 build and emit a `universal2` platform tag instead of the
#: single arch of whichever host happens to be compiling - the same
#: mechanism cibuildwheel/setuptools use, rather than hand-building a tag
#: string here (see PR #329 review: `infer_tag` alone yields an
#: arch-specific tag even for a true universal2 binary).
_UNIVERSAL2_ARCHFLAGS = "-arch x86_64 -arch arm64"


class AgentBuildHook(BuildHookInterface):
    PLUGIN_NAME = "blumkin-agent"

    def initialize(self, version: str, build_data: dict) -> None:
        del version  # hatchling's own resolved version; use self.metadata instead
        target_binary = Path(self.root) / "src" / "blumkin" / "agent" / "bin" / "blumkin-agent"
        target_binary.parent.mkdir(parents=True, exist_ok=True)
        if target_binary.exists():
            target_binary.unlink()

        if sys.platform != "darwin":
            self.app.display_info(
                "blumkin-agent: skipping build (agent is macOS-only for now); "
                "shipping without the binary"
            )
            return
        cargo = shutil.which("cargo")
        if cargo is None:
            self.app.display_warning(
                "blumkin-agent: `cargo` not found - shipping without the binary; "
                "the agent feature will report itself unavailable "
                "(install a Rust toolchain, e.g. `brew install rust` or rustup.rs, "
                "and reinstall to build it)"
            )
            return

        try:
            binary = self._build_binary(cargo)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            # A genuine cargo build failure (compile error, no artifact
            # reported, etc.) must degrade the same way a missing `cargo`
            # does - failing the whole install here would be worse than
            # shipping a pure-Python macOS wheel with the agent feature
            # reporting itself unavailable (see PR #329 review).
            self.app.display_warning(
                f"blumkin-agent: build failed ({exc}) - shipping without the binary; "
                "the agent feature will report itself unavailable"
            )
            return
        shutil.copy2(binary, target_binary)
        target_binary.chmod(0o755)
        # Marks this wheel as platform-specific (not `py3-none-any`), so pip
        # resolves a matching per-OS wheel instead of assuming pure Python.
        build_data["pure_python"] = False
        build_data["infer_tag"] = True

    def _build_binary(self, cargo: str) -> Path:
        crate_dir = Path(self.root) / "rust-agent"
        env = dict(os.environ)
        env.setdefault("BLUMKIN_EMBED_VERSION", self.metadata.version)
        env.setdefault("BLUMKIN_EMBED_COMMIT", _git_commit(self.root))
        try:
            binary = self._build_universal2(cargo, crate_dir, env)
        except subprocess.CalledProcessError, FileNotFoundError:
            # Most likely a missing cross-compilation target (no `rustup`,
            # or `rustup target add` was never run for the other arch) -
            # common on a bare `brew install rust` dev machine. A
            # native-arch-only binary still exercises the full agent for
            # local development; the real release wheel (built in CI,
            # where both targets are installed) still gets a true
            # universal2 binary. `FileNotFoundError` covers `lipo` itself
            # being missing (unlike `cargo`, it is not checked with
            # `shutil.which` up front) - a partial Command Line Tools
            # install, or a trimmed build-environment `PATH` - which would
            # otherwise escape uncaught and hard-fail the whole install,
            # contradicting this module's documented degrade-gracefully
            # behavior (see PR #329 review).
            self.app.display_warning(
                "blumkin-agent: universal2 (arm64+x86_64) build failed, likely a missing "
                "cross-compilation target or `lipo` - falling back to a native-arch-only "
                "build for local development (see `rustup target add` in "
                "rust-agent/README.md)"
            )
            # An inherited two-arch ARCHFLAGS (e.g. a dev/CI box that
            # exports it for universal Python builds) must not survive
            # onto this native-arch-only binary, or hatchling's
            # `infer_tag` would still stamp the wheel `universal2` even
            # though only one arch was actually built (see PR #329 review).
            os.environ.pop("ARCHFLAGS", None)
            return self._build_native(cargo, crate_dir, env)
        os.environ["ARCHFLAGS"] = _UNIVERSAL2_ARCHFLAGS
        return binary

    def _build_native(self, cargo: str, crate_dir: Path, env: dict) -> Path:
        return _cargo_build_binary(cargo, crate_dir, env)

    def _build_universal2(self, cargo: str, crate_dir: Path, env: dict) -> Path:
        """Build both Apple Silicon and Intel targets, then `lipo` them together.

        A single universal2 binary (rather than two separate wheels) keeps
        the release pipeline to one macOS wheel, matching how CPython's own
        official macOS installers ship.
        """
        built_binaries = []
        for target in _SUPPORTED_DARWIN_TARGETS:
            self._ensure_rust_target(target)
            built_binaries.append(_cargo_build_binary(cargo, crate_dir, env, target=target))

        universal_binary = _cargo_target_dir(cargo, crate_dir, env) / "blumkin-agent-universal2"
        subprocess.run(
            ["lipo", "-create", "-output", str(universal_binary), *map(str, built_binaries)],
            check=True,
        )
        return universal_binary

    def _ensure_rust_target(self, target: str) -> None:
        """Best-effort `rustup target add` - a no-op (and harmless) without rustup."""
        rustup = shutil.which("rustup")
        if rustup is None:
            return
        subprocess.run([rustup, "target", "add", target], check=False)


def _cargo_build_binary(
    cargo: str, crate_dir: Path, env: dict, *, target: str | None = None
) -> Path:
    """Build the crate and return the compiled executable's actual path.

    `--message-format=json` makes cargo report every artifact it produces,
    including the final `compiler-artifact` message's `executable` field -
    the one place that already accounts for `CARGO_TARGET_DIR`, a
    `~/.cargo/config.toml` `[build] target-dir`, *and* a configured
    `[build] target` (which makes cargo write to `<target_dir>/<triple>/
    release/...` even for a same-arch `cargo build --release` with no
    explicit `--target` flag - a case `_cargo_target_dir` alone cannot
    detect). Reading the path back from cargo's own output rather than
    guessing keeps `_build_native`/`_build_universal2` correct regardless
    of how the caller's cargo is configured (see PR #329 review).
    """
    command = [cargo, "build", "--release", "--message-format=json"]
    if target is not None:
        command += ["--target", target]
    result = subprocess.run(
        command, cwd=crate_dir, env=env, check=True, capture_output=True, text=True
    )
    executable: str | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("reason") == "compiler-artifact" and message.get("executable"):
            executable = message["executable"]
    if executable is None:
        raise RuntimeError(f"cargo build for {crate_dir} did not report a compiled executable path")
    return Path(executable)


def _cargo_target_dir(cargo: str, crate_dir: Path, env: dict) -> Path:
    """Ask cargo itself where it will place build artifacts.

    Hardcoding `crate_dir / "target"` assumes cargo's default, but a
    relocated target dir - `CARGO_TARGET_DIR` in the environment, or a
    `[build] target-dir` in `~/.cargo/config.toml` (which cargo reads
    itself; a plain `dict(os.environ)` copy cannot see it) - would then
    have `_build_native`/`_build_universal2` look for the binary in the
    wrong place: `lipo`/`shutil.copy2` then fail on a nonexistent path
    instead of this module degrading gracefully (see PR #329 review).
    Falls back to the conventional `crate_dir / "target"` if `cargo
    metadata` itself is unavailable or fails, matching the previous
    hardcoded behavior rather than raising.
    """
    try:
        result = subprocess.run(
            [cargo, "metadata", "--format-version", "1", "--no-deps"],
            cwd=crate_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(json.loads(result.stdout)["target_directory"])
    except subprocess.CalledProcessError, FileNotFoundError, KeyError, ValueError:
        return crate_dir / "target"


def _git_commit(repo_root: str) -> str:
    """Best-effort short commit hash for `BLUMKIN_EMBED_COMMIT`'s default.

    Deliberately does *not* fall back to `GITHUB_SHA` - see
    `blumkin.version`'s docstring on why only `BLUMKIN_GIT_SHA`/
    `BLUMKIN_EMBED_COMMIT` (both explicitly opted into by this project's own
    release workflow) are trusted: an installed-from-sdist `pip install`
    running inside *any* unrelated GitHub Actions job would otherwise bake
    that job's own repo commit into the agent binary (see PR #329 review).
    `"unknown"` matches `blumkin.version._normalize_commit`'s output for the
    same not-available case, keeping the Python and Rust version stamps in
    agreement.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return result.stdout.strip()[:12]
