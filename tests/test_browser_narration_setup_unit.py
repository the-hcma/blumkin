"""The Kokoro narration setup and engine (#816): hermetic, nothing is downloaded or spoken."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import types
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser import narration, narration_setup
from blumkin.browser.narration import NarrationError
from blumkin.browser.narration_kokoro import KokoroEngine
from blumkin.browser.narration_setup import (
    DOWNLOADS,
    MODEL_NAME,
    VOICES_NAME,
    Download,
    SetupError,
    fetch,
    is_installed,
    narration_dir,
    run_setup,
    site_dir,
    unpack_wheel,
)
from blumkin.cli import main


def _fake_fetch(item: Download, dest: Path, progress: Any = None) -> None:
    dest.write_bytes(b"x" * 4)


def _good_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narration_setup, "_matches", lambda path, item: False)


def _wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _install_fake(config: Path) -> None:
    root = narration_dir(config)
    (root / "kokoro_onnx").parent.mkdir(parents=True, exist_ok=True)
    (site_dir(config) / "kokoro_onnx").mkdir(parents=True, exist_ok=True)
    (root / MODEL_NAME).write_bytes(b"m")
    (root / VOICES_NAME).write_bytes(b"v")
    (root / "setup.json").write_text(
        json.dumps({"when": "t", "kokoro_onnx": "0.6.1", "python": narration_setup._python_tag()})
    )


def _full_run(config: Path, **kw: Any) -> dict[str, Any]:
    calls: dict[str, Any] = {"fetched": [], "installed": 0}

    def fetch_file(item: Download, dest: Path, progress: Any = None) -> None:
        calls["fetched"].append(item.name)
        if item.name.endswith(".whl"):
            _wheel(dest, {"kokoro_onnx/__init__.py": b""})
        else:
            dest.write_bytes(b"x")

    def install(target: Path) -> None:
        calls["installed"] += 1

    result = run_setup(
        config,
        confirm=kw.get("confirm", lambda plan: True),
        fetch_file=kw.get("fetch_file", fetch_file),
        install=kw.get("install", install),
        check=kw.get("check", lambda c: 1.5),
    )
    result["calls"] = calls
    return result


def test_the_plan_lists_every_download_with_its_size() -> None:
    plan = narration_setup.plan()
    assert [d["name"] for d in plan["downloads"]] == [d.name for d in DOWNLOADS]
    assert plan["total_bytes"] == sum(d.size for d in DOWNLOADS)
    assert any(p.startswith("onnxruntime") for p in plan["packages"])


def test_every_pinned_download_is_https_on_an_allowed_host() -> None:
    for item in DOWNLOADS:
        narration_setup._require_allowed(item.url)
        assert len(item.sha256) == 64


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",
        "https://evil.example.com/x",
        "file:///etc/passwd",
        "https://github.com.evil.example/x",
    ],
)
def test_a_download_address_off_the_list_is_refused(url: str) -> None:
    with pytest.raises(SetupError, match="HTTPS address"):
        narration_setup._require_allowed(url)


def test_a_redirect_off_the_list_is_refused() -> None:
    handler = narration_setup._AllowedRedirects()
    with pytest.raises(SetupError):
        handler.redirect_request(
            SimpleNamespace(), None, 302, "Found", {}, "https://evil.example.com/x"
        )


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _serve(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    opener = SimpleNamespace(open=lambda url, timeout: _Response(data))
    monkeypatch.setattr(narration_setup.urllib.request, "build_opener", lambda *handlers: opener)


def _item(data: bytes, **over: Any) -> Download:
    base: dict[str, Any] = {
        "name": "f.bin",
        "url": "https://github.com/f.bin",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "purpose": "test",
    }
    base.update(over)
    return Download(**base)


def test_a_download_that_matches_its_size_and_hash_is_kept_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"hello" * 10
    _serve(monkeypatch, data)
    seen: list[tuple[int, int]] = []
    dest = tmp_path / "f.bin"
    fetch(_item(data), dest, lambda got, total: seen.append((got, total)))
    assert dest.read_bytes() == data
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert seen[-1] == (len(data), len(data))


@pytest.mark.parametrize("change", ["hash", "short", "long"])
def test_a_download_that_does_not_match_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    data = b"hello" * 10
    _serve(monkeypatch, data)
    over = {
        "hash": {"sha256": "0" * 64},
        "short": {"size": len(data) + 1},
        "long": {"size": len(data) - 1},
    }[change]
    dest = tmp_path / "f.bin"
    with pytest.raises(SetupError):
        fetch(_item(data, **over), dest)
    assert not dest.exists()


def test_a_download_address_off_the_list_fetches_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "f.bin"
    with pytest.raises(SetupError):
        fetch(_item(b"x", url="https://evil.example.com/f.bin"), dest)
    assert not dest.exists()


def test_a_network_failure_is_a_setup_error_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str, timeout: float) -> Any:
        raise OSError("offline")

    monkeypatch.setattr(
        narration_setup.urllib.request, "build_opener", lambda *h: SimpleNamespace(open=boom)
    )
    dest = tmp_path / "f.bin"
    with pytest.raises(SetupError, match="offline"):
        fetch(_item(b"x"), dest)
    assert not dest.exists()


def test_a_wheel_is_unpacked_inside_its_folder(tmp_path: Path) -> None:
    wheel = tmp_path / "w.whl"
    _wheel(wheel, {"kokoro_onnx/__init__.py": b"x = 1"})
    target = tmp_path / "site"
    target.mkdir()
    unpack_wheel(wheel, target)
    assert (target / "kokoro_onnx" / "__init__.py").read_bytes() == b"x = 1"


def test_a_wheel_member_that_escapes_the_folder_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "w.whl"
    _wheel(wheel, {"ok.py": b"", "../evil.py": b"boom"})
    target = tmp_path / "site"
    target.mkdir()
    with pytest.raises(SetupError, match="inside the package"):
        unpack_wheel(wheel, target)
    assert not (tmp_path / "evil.py").exists()
    assert not (target / "ok.py").exists()


def test_the_installer_runs_pinned_requirements_into_the_private_folder(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **kw: Any) -> Any:
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    narration_setup.install_requirements(tmp_path, run)
    assert str(tmp_path) in seen[0]
    assert "--target" in seen[0]
    assert set(narration_setup.REQUIREMENTS) <= set(seen[0])


def test_a_failed_install_is_a_setup_error_naming_the_tail(tmp_path: Path) -> None:
    def run(command: list[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=2, stdout="", stderr="a\nb\nno such package")

    with pytest.raises(SetupError, match="exit 2.*no such package"):
        narration_setup.install_requirements(tmp_path, run)


def test_the_smoke_test_reports_the_seconds_spoken(tmp_path: Path) -> None:
    def run(command: list[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout="warn\n0.75\n", stderr="")

    assert narration_setup.smoke_test(tmp_path, run) == 0.75


@pytest.mark.parametrize(
    "done",
    [
        SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nValueError: bad"),
        SimpleNamespace(returncode=0, stdout="0\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ],
)
def test_a_voice_that_cannot_speak_fails_the_smoke_test(tmp_path: Path, done: Any) -> None:
    with pytest.raises(SetupError, match="test word"):
        narration_setup.smoke_test(tmp_path, lambda command, **kw: done)


def test_a_declined_confirmation_downloads_nothing(tmp_path: Path) -> None:
    calls: list[str] = []
    with pytest.raises(SetupError, match="confirmation"):
        run_setup(
            tmp_path,
            confirm=lambda plan: False,
            fetch_file=lambda item, dest, progress=None: calls.append(item.name),
        )
    assert calls == []
    assert not narration_dir(tmp_path).exists()


def test_the_confirmation_is_shown_the_plan(tmp_path: Path) -> None:
    shown: list[dict[str, Any]] = []
    _full_run(tmp_path, confirm=lambda plan: shown.append(plan) or True)
    assert shown[0]["total_bytes"] == sum(d.size for d in DOWNLOADS)


def test_a_setup_downloads_installs_checks_and_marks_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _good_match(monkeypatch)
    result = _full_run(tmp_path)
    assert result["ok"] and not result["already"]
    assert result["test_seconds"] == 1.5
    assert result["calls"]["fetched"] == [d.name for d in DOWNLOADS]
    assert is_installed(tmp_path)
    root = narration_dir(tmp_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert not (root / narration_setup.WHEEL_NAME).exists()
    assert (site_dir(tmp_path) / "kokoro_onnx" / "__init__.py").is_file()


def test_a_finished_setup_runs_again_and_fetches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _good_match(monkeypatch)
    _full_run(tmp_path)
    again = _full_run(
        tmp_path,
        confirm=lambda plan: pytest.fail("must not ask"),
        fetch_file=lambda *a, **k: pytest.fail("must not download"),
        install=lambda t: pytest.fail("must not install"),
    )
    assert again["already"] and again["ok"]


def test_a_failed_check_leaves_no_marker_so_the_engine_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _good_match(monkeypatch)

    def check(config: Path) -> float:
        raise SetupError("no sound")

    with pytest.raises(SetupError, match="no sound"):
        _full_run(tmp_path, check=check)
    assert not is_installed(tmp_path)
    assert not (narration_dir(tmp_path) / "setup.json").exists()


def test_a_failed_install_leaves_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _good_match(monkeypatch)

    def install(target: Path) -> None:
        raise SetupError("pip failed")

    with pytest.raises(SetupError):
        _full_run(tmp_path, install=install)
    assert not is_installed(tmp_path)


def test_a_verified_file_already_there_is_kept_not_fetched_again(tmp_path: Path) -> None:
    root = narration_dir(tmp_path)
    root.mkdir(parents=True)
    (root / VOICES_NAME).write_bytes(b"verified")
    fetched: list[str] = []
    saved = narration_setup._matches
    try:
        narration_setup._matches = lambda path, item: path.name == VOICES_NAME
        _full_run(
            tmp_path,
            fetch_file=lambda item, dest, progress=None: (
                fetched.append(item.name),
                _wheel(dest, {"kokoro_onnx/__init__.py": b""})
                if item.name.endswith(".whl")
                else dest.write_bytes(b"x"),
            )[0],
        )
    finally:
        narration_setup._matches = saved
    assert VOICES_NAME not in fetched
    assert (root / VOICES_NAME).read_bytes() == b"verified"


def test_a_file_that_does_not_match_its_hash_is_replaced(tmp_path: Path) -> None:
    item = _item(b"right")
    path = tmp_path / "f.bin"
    path.write_bytes(b"wrong")
    assert not narration_setup._matches(path, item)
    path.write_bytes(b"right")
    assert narration_setup._matches(path, item)


def test_the_status_has_the_version_once_set_up(tmp_path: Path) -> None:
    assert narration_setup.status(tmp_path) == {"installed": False}
    _install_fake(tmp_path)
    found = narration_setup.status(tmp_path)
    assert found["installed"] and found["kokoro_onnx"] == "0.6.1"


def test_a_missing_piece_means_not_installed(tmp_path: Path) -> None:
    _install_fake(tmp_path)
    (narration_dir(tmp_path) / MODEL_NAME).unlink()
    assert not is_installed(tmp_path)


def test_the_private_packages_are_on_the_path_only_while_imported(tmp_path: Path) -> None:
    folder = str(site_dir(tmp_path))
    with narration_setup.site_on_path(tmp_path):
        assert sys.path[0] == folder
    assert folder not in sys.path


# ----------------------------------------------------------------------------- the engine


def _fake_modules(monkeypatch: pytest.MonkeyPatch, voices: list[str]) -> list[Any]:
    created: list[Any] = []

    class Kokoro:
        def __init__(self, model: str, voices_file: str) -> None:
            created.append((model, voices_file))

        def create(self, text: str, voice: str, speed: float, lang: str) -> Any:
            created.append((text, voice, speed, lang))
            return [0.0, 0.5, -2.0, 2.0], 24000

    class _Loaded:
        files = voices

        def __enter__(self) -> _Loaded:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _Array(list):
        def astype(self, kind: str) -> _Array:
            return self

        def tobytes(self) -> bytes:
            import struct

            return struct.pack(f"<{len(self)}h", *self)

    numpy = types.ModuleType("numpy")
    numpy.clip = lambda a, lo, hi: [max(lo, min(hi, x)) for x in a]  # type: ignore[attr-defined]
    numpy.load = lambda path, allow_pickle: _Loaded()  # type: ignore[attr-defined]

    class _Scaled(list):
        def __mul__(self, k: float) -> _Array:  # type: ignore[override]
            return _Array(int(x * k) for x in self)

    real_clip = numpy.clip
    numpy.clip = lambda a, lo, hi: _Scaled(real_clip(a, lo, hi))  # type: ignore[attr-defined]
    kokoro = types.ModuleType("kokoro_onnx")
    kokoro.Kokoro = Kokoro  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", kokoro)
    return created


def test_the_engine_not_set_up_says_how_to_set_it_up(tmp_path: Path) -> None:
    engine = KokoroEngine(tmp_path)
    assert not engine.is_installed()
    with pytest.raises(NarrationError, match="narration setup.*--engine say"):
        engine.ensure_ready()
    with pytest.raises(NarrationError):
        engine.synthesize("hi", voice="af_bella", speed=1.0, out=tmp_path / "o.wav")


def test_the_default_voice_is_bella() -> None:
    assert KokoroEngine(Path("/nowhere")).default_voice == "af_bella"


def test_a_voice_is_checked_against_the_voices_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(tmp_path)
    _fake_modules(monkeypatch, [f"af_v{i}" for i in range(10)] + ["af_bella"])
    engine = KokoroEngine(tmp_path)
    engine.check_voice("af_bella")
    with pytest.raises(NarrationError, match="'nobody'.*and 3 more"):
        engine.check_voice("nobody")


def test_a_cue_becomes_a_mono_wav_and_the_model_loads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(tmp_path)
    created = _fake_modules(monkeypatch, ["af_bella", "bf_emma"])
    engine = KokoroEngine(tmp_path)
    out = tmp_path / "a.wav"
    engine.synthesize("Hello there", voice="af_bella", speed=1.1, out=out)
    engine.synthesize("Again", voice="bf_emma", speed=1.0, out=tmp_path / "b.wav")
    loads = [c for c in created if len(c) == 2]
    assert len(loads) == 1
    assert created[1] == ("Hello there", "af_bella", 1.1, "en-us")
    assert created[2][3] == "en-gb"
    with wave.open(str(out)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.getnframes() == 4
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_model_that_fails_names_no_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(tmp_path)
    _fake_modules(monkeypatch, ["af_bella"])

    def boom(self: Any, text: str, voice: str, speed: float, lang: str) -> Any:
        raise RuntimeError(f"cannot say {text}")

    sys.modules["kokoro_onnx"].Kokoro.create = boom  # type: ignore[attr-defined]
    with pytest.raises(NarrationError) as caught:
        KokoroEngine(tmp_path).synthesize(
            "SECRET words", voice="af_bella", speed=1.0, out=tmp_path / "o.wav"
        )
    assert "SECRET" not in str(caught.value)


def test_a_model_that_cannot_load_is_a_narration_error_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(tmp_path)
    _fake_modules(monkeypatch, ["af_bella"])

    def broken(self: Any, model: str, voices: str) -> None:
        raise ValueError("bad model file")

    sys.modules["kokoro_onnx"].Kokoro.__init__ = broken  # type: ignore[attr-defined]
    engine = KokoroEngine(tmp_path)
    with pytest.raises(NarrationError, match="model to load.*bad model file"):
        engine.synthesize("x", voice="af_bella", speed=1.0, out=tmp_path / "o.wav")
    assert engine._model is None


# ------------------------------------------------------------------------ engine selection


def test_auto_is_kokoro_only_once_it_is_set_up(tmp_path: Path) -> None:
    assert narration.get_engine("auto", tmp_path).name == "say"
    _install_fake(tmp_path)
    assert narration.get_engine("auto", tmp_path).name == "kokoro"


def test_auto_without_a_config_dir_is_say() -> None:
    assert narration.get_engine("auto").name == "say"


def test_kokoro_by_name_is_kokoro_even_before_setup(tmp_path: Path) -> None:
    assert narration.get_engine("kokoro", tmp_path).name == "kokoro"
    with pytest.raises(ValueError, match="config directory"):
        narration.get_engine("kokoro")


# --------------------------------------------------------------------------------- the CLI


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, list(args))


def test_setup_with_json_needs_yes() -> None:
    result = _run("--json", "browser", "narration", "setup")
    assert result.exit_code != 0
    assert "--yes" in result.output


def test_setup_asks_and_a_no_downloads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.cli.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        narration_setup.urllib.request,
        "build_opener",
        lambda *handlers: pytest.fail("must not download"),
    )
    result = CliRunner().invoke(main, ["browser", "narration", "setup"], input="n\n")
    assert result.exit_code != 0
    assert "SHA-256" in result.output
    assert not narration_dir(tmp_path).exists()


def test_setup_yes_json_reports_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("blumkin.cli.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "blumkin.cli.run_setup",
        lambda config, confirm, say: {
            "action": "narration-setup",
            "already": False,
            "ok": confirm({}) is True,
            "test_seconds": 1.0,
        },
    )
    result = _run("--json", "browser", "narration", "setup", "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_a_setup_failure_is_reported_with_its_own_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.cli.config_dir", lambda: tmp_path)

    def fail(config: Path, confirm: Any, say: Any) -> Any:
        raise SetupError("Expected a thing, got none")

    monkeypatch.setattr("blumkin.cli.run_setup", fail)
    result = _run("--json", "browser", "narration", "setup", "--yes")
    assert result.exit_code != 0
    assert "narration_setup_failed" in result.output


def test_status_says_which_engine_auto_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.cli.config_dir", lambda: tmp_path)
    before = json.loads(_run("--json", "browser", "narration", "status").output)
    assert before["installed"] is False and before["auto"] == "say"
    _install_fake(tmp_path)
    after = json.loads(_run("--json", "browser", "narration", "status").output)
    assert after["installed"] is True and after["auto"] == "kokoro"
    text = _run("browser", "narration", "status")
    assert "set up" in text.output


def test_the_setups_own_test_word_is_spoken_before_the_marker_exists(tmp_path: Path) -> None:
    _install_fake(tmp_path)
    (narration_dir(tmp_path) / "setup.json").unlink()
    assert not KokoroEngine(tmp_path).is_installed()
    KokoroEngine(tmp_path, before_marker=True).ensure_ready()
    captured: dict[str, Any] = {}

    def run(command: list[str], **kw: Any) -> Any:
        captured["code"] = command[3]
        captured["command"] = command
        captured["cwd"] = kw.get("cwd")
        return SimpleNamespace(returncode=0, stdout="1.0\n", stderr="")

    narration_setup.smoke_test(tmp_path, run)
    assert "before_marker=True" in captured["code"]
    assert captured["command"][1] == "-I"  # no PYTHONPATH, no current directory on the path
    assert captured["cwd"] == narration_dir(tmp_path)
    assert "mkdtemp" not in captured["code"]  # the scratch folder is removed after the word


def test_a_stale_marker_is_removed_before_a_repair_so_it_cannot_vouch_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(tmp_path)
    (narration_dir(tmp_path) / MODEL_NAME).unlink()  # the install lost its model
    states: list[bool] = []

    def fetch_file(item: Download, dest: Path, progress: Any = None) -> None:
        states.append((narration_dir(tmp_path) / "setup.json").exists())
        if item.name.endswith(".whl"):
            _wheel(dest, {"kokoro_onnx/__init__.py": b""})
        else:
            dest.write_bytes(b"x")

    _good_match(monkeypatch)
    _full_run(tmp_path, fetch_file=fetch_file)
    assert states and not any(states)
    assert is_installed(tmp_path)


def test_a_setup_made_under_another_python_is_not_reused(tmp_path: Path) -> None:
    _install_fake(tmp_path)
    assert is_installed(tmp_path)
    marker = narration_dir(tmp_path) / "setup.json"
    marker.write_text(json.dumps({"python": "2.7", "when": "t"}))
    assert not is_installed(tmp_path)
    marker.write_text("not json")
    assert not is_installed(tmp_path)


def test_a_retry_installs_into_an_empty_package_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _good_match(monkeypatch)
    leftover = site_dir(tmp_path) / "numpy"
    leftover.mkdir(parents=True)
    (leftover / "half.py").write_text("partial")
    seen: list[list[str]] = []
    _full_run(
        tmp_path, install=lambda target: seen.append(sorted(p.name for p in target.iterdir()))
    )
    assert seen == [[]]


def test_the_default_voice_passes_the_voice_check() -> None:
    assert narration.validate_voice(KokoroEngine(Path("/nowhere")).default_voice) == "af_bella"


def test_the_plan_the_person_confirms_counts_the_packages_in_the_size() -> None:
    from blumkin.cli import _narration_plan_lines

    text = "\n".join(_narration_plan_lines(narration_setup.plan()))
    assert "plus about 130 MB for the packages" in text
    assert "about 500 MB on disk" in text
