"""The one-time setup of Kokoro, the offline speech engine for narration (#816).

Kokoro (an open, 82M-parameter text-to-speech model, run through `kokoro-onnx`) is not a
dependency of blumkin: it needs a model of about 325 MB and a few packages, so it is installed
on request, with `blumkin browser narration setup`, into a private folder under the config
directory (`narration/kokoro/`, owner only). Nothing is downloaded at any other time: not while
recording, not by `narrate`, not by `record stop`.

What is fetched is fixed here and checked:

- the model and the voices file come from the `kokoro-onnx` project's release page, and the
  `kokoro-onnx` wheel from PyPI, each with a pinned size and SHA-256 that must match before the
  file is used (a file that does not match is removed);
- the packages Kokoro needs (`onnxruntime`, `numpy`, `espeakng-loader`, `phonemizer`) are
  installed by `uv` or `pip` into the same private folder, within pinned version ranges (the
  tool's own index settings apply, and what those packages depend on is resolved by the tool, so
  only the three files above are hash-checked);
- every download is HTTPS, only from the hosts listed below, redirects included.

`kokoro-onnx` declares `Requires-Python <3.14` although its code, and every package it needs,
works on 3.14, so its pure-Python wheel is unpacked from the verified file instead of being
installed by a tool that would enforce that cap. `espeak-ng`, which the phonemizer loads as a
library, is GPL software: it is installed on demand into this folder and is never bundled with
blumkin.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NARRATION_DIRNAME = "narration"
KOKORO_DIRNAME = "kokoro"
SITE_DIRNAME = "site"
MARKER_NAME = "setup.json"
MODEL_NAME = "kokoro-v1.0.onnx"
VOICES_NAME = "voices-v1.0.bin"
WHEEL_NAME = "kokoro_onnx-0.6.1-py3-none-any.whl"
#: The packages Kokoro needs, installed within these ranges: exactly the `Requires-Dist` of the
#: pinned kokoro-onnx 0.6.1 wheel (its wheel is unpacked, not installed, so nothing resolves them).
REQUIREMENTS = (
    "onnxruntime>=1.20.1,<2",
    "numpy>=2.0.2,<3",
    "espeakng-loader>=0.2.4,<1",
    "phonemizer>=3.4.0,<4",
)
#: The hosts a download may come from (and be redirected to), all over HTTPS.
ALLOWED_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "files.pythonhosted.org",
)
DOWNLOAD_TIMEOUT_S = 60.0
INSTALL_TIMEOUT_S = 900.0
SMOKE_TIMEOUT_S = 180.0
CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class Download:
    """One file the setup fetches: where from, what it must be, and what it is for."""

    name: str
    url: str
    size: int
    sha256: str
    purpose: str


DOWNLOADS = (
    Download(
        name=WHEEL_NAME,
        url=(
            "https://files.pythonhosted.org/packages/60/e1/a27e5a70a525a5ee1fd5357596f07b724d02ff"
            "317f134e86cb6e3d9db968/kokoro_onnx-0.6.1-py3-none-any.whl"
        ),
        size=26084,
        sha256="50c8de4950d601df41428ee5462a48c8a78bef441bf671f2492e070ef44d8a32",
        purpose="the kokoro-onnx package (pure Python)",
    ),
    Download(
        name=MODEL_NAME,
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
        + MODEL_NAME,
        size=325532387,
        sha256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        purpose="the speech model",
    ),
    Download(
        name=VOICES_NAME,
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
        + VOICES_NAME,
        size=28214398,
        sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        purpose="the voices",
    ),
)


class SetupError(RuntimeError):
    """The Kokoro setup could not be completed (nothing half-installed is left usable)."""


def narration_dir(config_dir: Path) -> Path:
    return config_dir / NARRATION_DIRNAME / KOKORO_DIRNAME


def site_dir(config_dir: Path) -> Path:
    return narration_dir(config_dir) / SITE_DIRNAME


def _python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _marker_python(root: Path) -> str | None:
    try:
        return json.loads((root / MARKER_NAME).read_text()).get("python")
    except OSError, ValueError, AttributeError:
        return None


def is_installed(config_dir: Path) -> bool:
    """Whether the setup finished here, for this Python: marker, model, voices and packages.

    The packages include native code built for one Python minor version, so a setup made
    under another one is not reused: running the setup again repairs it.
    """
    root = narration_dir(config_dir)
    return (
        _marker_python(root) == _python_tag()
        and (root / MODEL_NAME).is_file()
        and (root / VOICES_NAME).is_file()
        and (root / SITE_DIRNAME / "kokoro_onnx").is_dir()
    )


def status(config_dir: Path) -> dict[str, Any]:
    """What is installed (counts and versions, never a path outside the config directory)."""
    root = narration_dir(config_dir)
    found: dict[str, Any] = {"installed": is_installed(config_dir)}
    marker = root / MARKER_NAME
    if marker.is_file():
        with contextlib.suppress(OSError, ValueError):
            info = json.loads(marker.read_text())
            found["since"] = info.get("when")
            found["kokoro_onnx"] = info.get("kokoro_onnx")
    return found


def plan() -> dict[str, Any]:
    """What the setup would fetch and install, for the person to confirm."""
    return {
        "downloads": [
            {"name": d.name, "purpose": d.purpose, "size": d.size, "url": d.url} for d in DOWNLOADS
        ],
        "packages": list(REQUIREMENTS),
        "total_bytes": sum(d.size for d in DOWNLOADS),
    }


# --------------------------------------------------------------------------- downloading


class _AllowedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to HTTPS and only to a host the setup is allowed to use."""

    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        _require_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _require_allowed(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "") not in ALLOWED_HOSTS:
        raise SetupError(
            f"Expected an HTTPS address on one of {', '.join(ALLOWED_HOSTS)}, got a download "
            f"from {parts.hostname or 'no host'!r}"
        )


def fetch(item: Download, dest: Path, progress: Callable[[int, int], None] | None = None) -> None:
    """Download `item` to `dest` (a new private file), checking its size and SHA-256 as it comes.

    A file that is too long, too short or has another hash is removed, never kept.
    """
    _require_allowed(item.url)
    opener = urllib.request.build_opener(_AllowedRedirects)
    digest = hashlib.sha256()
    received = 0
    descriptor = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with (
            os.fdopen(descriptor, "wb") as handle,
            opener.open(item.url, timeout=DOWNLOAD_TIMEOUT_S) as response,
        ):
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > item.size:
                    raise SetupError(
                        f"Expected {item.name} to be {item.size} bytes, got more; it was removed"
                    )
                digest.update(chunk)
                handle.write(chunk)
                if progress is not None:
                    progress(received, item.size)
        if received != item.size or digest.hexdigest() != item.sha256:
            raise SetupError(
                f"Expected {item.name} to match its pinned size and SHA-256, got a file that "
                "does not; it was removed, so nothing from it is used"
            )
    except (OSError, urllib.error.URLError) as exc:
        dest.unlink(missing_ok=True)
        raise SetupError(f"Expected to download {item.name}, got {exc}") from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- installing


def unpack_wheel(wheel: Path, target: Path) -> None:
    """Unpack a verified pure-Python wheel into `target`, refusing any path that escapes it."""
    root = target.resolve()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if destination != root and root not in destination.parents:
                raise SetupError(f"Expected files inside the package, got {member.filename!r}")
        archive.extractall(target)  # noqa: S202 - every member was checked above


def _installer_command(target: Path) -> list[str]:
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, "--target", str(target)]
    return [sys.executable, "-m", "pip", "install", "--target", str(target)]


def install_requirements(target: Path, run: Callable[..., Any] = subprocess.run) -> None:
    """Install the packages Kokoro needs into `target` with `uv` (or `pip`)."""
    command = [*_installer_command(target), *REQUIREMENTS]
    try:
        done = run(  # noqa: S603 - a fixed command with pinned requirements
            command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Expected the packages to install, got {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        raise SetupError(
            "Expected the packages to install, got exit "
            f"{done.returncode}: {' | '.join(line[:160] for line in tail)}"
        )


def smoke_test(config_dir: Path, run: Callable[..., Any] = subprocess.run) -> float:
    """Speak one short word with the installed engine, in a process of its own.

    Returns how many seconds of speech came out; a model that fails to load fails here, not in
    the middle of someone's recording.
    """
    code = (
        "import sys, tempfile, wave, pathlib\n"
        "from blumkin.browser.narration_kokoro import KokoroEngine\n"
        "engine = KokoroEngine(pathlib.Path(sys.argv[1]), before_marker=True)\n"
        "engine.ensure_ready()\n"
        "with tempfile.TemporaryDirectory() as scratch:\n"
        "    out = pathlib.Path(scratch) / 'ok.wav'\n"
        "    engine.synthesize('Ready.', voice=engine.default_voice, speed=1.0, out=out)\n"
        "    with wave.open(str(out)) as w:\n"
        "        print(w.getnframes() / w.getframerate())\n"
    )
    try:
        done = run(  # noqa: S603 - this interpreter, a fixed program and a path
            [sys.executable, "-I", "-c", code, str(config_dir)],
            cwd=narration_dir(config_dir),  # never the caller's folder, whose files could shadow
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
            check=False,
        )
        seconds = float((done.stdout or "0").strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise SetupError(f"Expected the voice to speak a test word, got {exc}") from exc
    if done.returncode != 0 or seconds <= 0:
        tail = (done.stderr or "").strip().splitlines()[-2:]
        raise SetupError(
            "Expected the voice to speak a test word, got "
            + (" | ".join(line[:160] for line in tail) or "no sound")
        )
    return seconds


# --------------------------------------------------------------------------- the setup


def run_setup(
    config_dir: Path,
    *,
    confirm: Callable[[dict[str, Any]], bool],
    fetch_file: Callable[..., None] = fetch,
    install: Callable[[Path], None] = install_requirements,
    check: Callable[[Path], float] = smoke_test,
    say: Callable[[str], None] = lambda text: None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Download, verify and install Kokoro, after `confirm` is shown what that means.

    Idempotent: files already there that match their hash are kept, and a finished install
    is reported as it is. Anything half-done is never left usable (the marker is written last).
    """
    if is_installed(config_dir):
        return {"action": "narration-setup", "already": True, "ok": True, **status(config_dir)}
    if not confirm(plan()):
        raise SetupError("Expected a confirmation to download the narration engine, got none")
    root = narration_dir(config_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / MARKER_NAME).unlink(missing_ok=True)  # a stale marker must not vouch for a repair
    os.chmod(root, 0o700)
    os.chmod(root.parent, 0o700)
    wheel = root / WHEEL_NAME
    for item in DOWNLOADS:
        path = root / item.name
        if path.is_file() and _matches(path, item):
            say(f"{item.name}: already here and verified")
            continue
        path.unlink(missing_ok=True)
        say(f"{item.name}: downloading ({item.size // 1024} KiB) for {item.purpose}")
        fetch_file(item, path)
    site = site_dir(config_dir)
    shutil.rmtree(site, ignore_errors=True)  # a retry starts from an empty folder
    site.mkdir(mode=0o700)
    say("installing the packages Kokoro needs")
    install(site)
    say("unpacking kokoro-onnx")
    unpack_wheel(wheel, site)
    wheel.unlink(missing_ok=True)  # the unpacked package is what is used
    say("speaking a test word")
    seconds = check(config_dir)
    marker = {
        "python": _python_tag(),
        "kokoro_onnx": "0.6.1",
        "model_sha256": DOWNLOADS[1].sha256,
        "voices_sha256": DOWNLOADS[2].sha256,
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
    }
    descriptor = os.open(root / MARKER_NAME, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(marker))
    return {
        "action": "narration-setup",
        "already": False,
        "ok": True,
        "test_seconds": round(seconds, 2),
        **status(config_dir),
    }


def _matches(path: Path, item: Download) -> bool:
    if path.stat().st_size != item.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == item.sha256


@contextlib.contextmanager
def site_on_path(config_dir: Path) -> Iterator[None]:
    """Put the private package folder first on `sys.path` while Kokoro is imported."""
    folder = str(site_dir(config_dir))
    sys.path.insert(0, folder)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(folder)
