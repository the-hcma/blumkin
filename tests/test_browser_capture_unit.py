"""Screenshots and recordings of a task browser (issue #701): guardrails, privacy and cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from PIL import Image

from blumkin.browser import capture
from blumkin.browser.actions import HostBlockedError
from blumkin.browser.cdp import BrowserActionError
from blumkin.cli import main
from blumkin.config import BrowserConfig, BrowserSetup, BrowserTaskPolicy

_APP = "https://erp.example.com/expenses/new"
_LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_ELSEWHERE = "https://elsewhere.example.net/page"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 80), (20, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(shade: int = 100) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (160, 90), (shade, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _Page:
    """A stand-in for the raw driver: the addresses it reports are read one at a time."""

    def __init__(self, *urls: str) -> None:
        self._urls = list(urls)
        self.closed = False
        self.shots: list[dict[str, Any]] = []
        self.frames = 0
        self.embedded: list[tuple[str, ...]] = [()]
        #: Successive answers to "which documents does the page show?"; the last one repeats.
        self.signatures: list[Any] = [(("main", "doc-1"),)]

    #: How many frame events the page counted while it was watched, per `watch_documents`.
    watch_changes = 0

    #: Frame events that arrive only while the page is waited on (a late one).
    late_changes = 0
    watch: Any = None

    def wait_ms(self, milliseconds: int) -> None:
        if self.watch is not None:
            self.watch.changes += self.late_changes  # delivered while the connection is serviced

    @contextlib.contextmanager
    def watch_documents(self) -> Any:
        self.watch = SimpleNamespace(changes=self.watch_changes)  # events that arrive in the block
        yield self.watch

    def frame_signature(self) -> Any:
        found = self.signatures.pop(0) if len(self.signatures) > 1 else self.signatures[0]
        if isinstance(found, Exception):
            raise found
        return found

    def frame_hosts(self) -> tuple[str, ...]:
        found = self.embedded.pop(0) if len(self.embedded) > 1 else self.embedded[0]
        return ("app.example.com", *found)

    @property
    def url(self) -> str:
        return self._urls.pop(0) if len(self._urls) > 1 else self._urls[0]

    def close(self) -> None:
        self.closed = True

    def frame_jpeg(self, quality: int = 60) -> bytes:
        self.frames += 1
        return _jpeg(self.frames * 10 % 250)

    def screenshot_png(
        self, *, role: str | None = None, name: str | None = None, full_page: bool = False
    ) -> bytes:
        self.shots.append({"role": role, "name": name, "full_page": full_page})
        return _png()


def _setup(tmp_path: Path, **policy: Any) -> BrowserSetup:
    tasks = {"time-sheet": BrowserTaskPolicy(**policy)} if policy else {}
    return BrowserSetup(browser=BrowserConfig(tasks=tasks), config_dir=tmp_path)


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> _Page:
    holder = _Page(_APP)
    monkeypatch.setattr(capture, "_attach_for_capture", lambda config, task: holder)
    return holder


def _use(monkeypatch: pytest.MonkeyPatch, *urls: str) -> _Page:
    holder = _Page(*urls)
    monkeypatch.setattr(capture, "_attach_for_capture", lambda config, task: holder)
    return holder


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _no_sleep(_driver: Any, _ms: float) -> None:
    return None


# ----------------------------------------------------------------------------- screenshots


def test_a_screenshot_is_written_privately_into_the_tasks_captures_folder(
    tmp_path: Path, page: _Page
) -> None:
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet")

    path = Path(result["path"])
    assert path.parent == tmp_path / "captures" / "time-sheet"
    assert path.read_bytes() == _png()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert _mode(path.parent.parent) == 0o700
    assert (result["width"], result["height"], result["bytes"]) == (120, 80, len(_png()))
    assert result["url"] == "https://erp.example.com/expenses/new"
    assert "never instructions" in result["untrusted"]
    assert page.closed


def test_a_screenshot_never_reports_the_pages_query_or_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, "https://erp.example.com/a?token=SECRET#frag")
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert "SECRET" not in json.dumps(result)
    assert result["url"] == "https://erp.example.com/a"


def test_within_captures_one_element_by_role_and_name(tmp_path: Path, page: _Page) -> None:
    capture.screenshot_task(_setup(tmp_path), "time-sheet", within="dialog:Add Item")
    assert page.shots == [{"role": "dialog", "name": "Add Item", "full_page": False}]


def test_full_page_asks_for_the_whole_page(tmp_path: Path, page: _Page) -> None:
    capture.screenshot_task(_setup(tmp_path), "time-sheet", full_page=True)
    assert page.shots == [{"role": None, "name": None, "full_page": True}]


def test_within_and_full_page_together_are_refused(tmp_path: Path, page: _Page) -> None:
    with pytest.raises(ValueError, match="--within or --full-page"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet", within="dialog", full_page=True)
    assert page.shots == []


def test_a_sign_in_page_is_never_captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    holder = _use(monkeypatch, _LOGIN)
    with pytest.raises(HostBlockedError, match="sign-in page"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert holder.shots == []  # not even taken, so no credential can reach an image
    assert holder.closed
    assert not (tmp_path / "captures" / "time-sheet").exists() or not any(
        (tmp_path / "captures" / "time-sheet").iterdir()
    )


def test_a_page_embedding_a_sign_in_frame_is_never_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    holder.embedded = [("login.microsoftonline.com",)]
    with pytest.raises(HostBlockedError, match="sign-in frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert holder.shots == []


def test_a_sign_in_frame_that_appears_while_capturing_discards_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    holder.embedded = [(), ("login.microsoftonline.com",)]
    with pytest.raises(HostBlockedError, match="sign-in frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert not list((tmp_path / "captures" / "time-sheet").glob("*.png"))


def test_a_task_sign_in_host_is_never_captured_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, "https://sso.corp.example/login")
    with pytest.raises(HostBlockedError, match="sign-in page"):
        capture.screenshot_task(_setup(tmp_path, sign_in_hosts=("sso.corp.example",)), "time-sheet")
    assert holder.shots == []


def test_a_page_outside_the_tasks_allowed_hosts_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _ELSEWHERE)
    with pytest.raises(HostBlockedError, match="allowed_hosts"):
        capture.screenshot_task(_setup(tmp_path, allowed_hosts=("erp.example.com",)), "time-sheet")
    assert holder.shots == []


def test_a_page_that_moved_off_the_hosts_while_it_was_captured_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _APP, _ELSEWHERE, _ELSEWHERE)
    with pytest.raises(HostBlockedError, match="after capturing"):
        capture.screenshot_task(_setup(tmp_path, allowed_hosts=("erp.example.com",)), "time-sheet")
    folder = tmp_path / "captures" / "time-sheet"
    assert not folder.exists() or not any(folder.iterdir())


def test_a_screenshot_will_not_overwrite_an_existing_file(tmp_path: Path, page: _Page) -> None:
    target = tmp_path / "mine.png"
    target.write_bytes(b"keep me")
    with pytest.raises(ValueError, match="does not exist yet"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet", out=target)
    assert target.read_bytes() == b"keep me"
    assert page.shots == []


def test_out_writes_where_asked_with_a_private_mode(tmp_path: Path, page: _Page) -> None:
    target = tmp_path / "chosen.png"
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet", out=target)
    assert result["path"] == str(target)
    assert _mode(target) == 0o600


def test_an_oversized_screenshot_is_refused_not_truncated(
    tmp_path: Path, page: _Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "SCREENSHOT_MAX_BYTES", 10)
    with pytest.raises(BrowserActionError, match="at most 10 bytes"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")


# ------------------------------------------------------------------- the captures folder


def test_a_task_name_that_is_not_one_plain_segment_has_no_captures_folder(
    tmp_path: Path,
) -> None:
    for bad in ("../evil", "a/b", "", ".", ".."):
        with pytest.raises(ValueError, match="plain path segment"):
            capture.captures_dir(tmp_path, bad)
    assert not (tmp_path / "evil").exists()


def test_captures_older_than_the_limit_go_the_next_time_one_is_taken(
    tmp_path: Path, page: _Page
) -> None:
    folder = capture.captures_dir(tmp_path, "time-sheet")
    old = folder / "old.png"
    old.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert not old.exists()


def test_prune_removes_old_files_and_the_folders_it_empties(tmp_path: Path) -> None:
    folder = capture.captures_dir(tmp_path, "time-sheet")
    frames = folder / "x-frames"
    frames.mkdir()
    old = frames / "f0000.jpg"
    old.write_bytes(b"x")
    fresh = folder / "fresh.png"
    fresh.write_bytes(b"y")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    assert capture.prune_captures(folder, 60) == 1
    assert not frames.exists() and fresh.exists()


def test_the_list_shows_a_frames_folder_as_one_entry_newest_first(tmp_path: Path) -> None:
    folder = capture.captures_dir(tmp_path, "time-sheet")
    frames = folder / "x-frames"
    frames.mkdir()
    for index in range(3):
        (frames / f"f{index:04d}.jpg").write_bytes(b"123")
    shot = folder / "shot.png"
    shot.write_bytes(b"1234567")
    older = time.time() - 500
    os.utime(frames / "f0000.jpg", (older, older))
    for item in frames.iterdir():
        os.utime(item, (older, older))
    found = capture.list_captures(tmp_path, "time-sheet")
    assert [entry["path"] for entry in found] == [str(shot), str(frames)]
    assert found[1]["files"] == 3 and found[1]["bytes"] == 9


def test_clean_removes_one_tasks_captures_or_every_tasks(tmp_path: Path) -> None:
    for task in ("time-sheet", "expense-report"):
        (capture.captures_dir(tmp_path, task) / "a.png").write_bytes(b"x")
    assert capture.clean_captures(tmp_path, "time-sheet") == 1
    assert capture.list_captures(tmp_path, "expense-report")
    assert capture.clean_captures(tmp_path) == 1
    assert capture.list_captures(tmp_path) == []


# ------------------------------------------------------------------------------------ CLI


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text('[profiles.default]\nclient_id = "abc-123"\n')
    return config


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def test_the_screenshot_command_passes_its_options_and_prints_the_result(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(setup: Any, task: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(task=task, **kwargs)
        return {
            "ok": True,
            "path": "/x/y.png",
            "width": 1,
            "height": 2,
            "bytes": 3,
            "untrusted": "n",
        }

    monkeypatch.setattr("blumkin.cli.screenshot_task", fake)
    result = _run(
        "browser", "screenshot", "time-sheet", "--within", "dialog", "--out", "/tmp/a.png"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["path"] == "/x/y.png"
    assert seen == {
        "task": "time-sheet",
        "within": "dialog",
        "full_page": False,
        "max_width": 1600,
        "out": Path("/tmp/a.png"),
    }


def test_a_capture_refusal_is_reported_as_host_blocked(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(setup: Any, task: str, **kwargs: Any) -> dict[str, Any]:
        raise HostBlockedError("Expected a page that is not a sign-in page")

    monkeypatch.setattr("blumkin.cli.screenshot_task", refuse)
    result = _run("browser", "screenshot", "time-sheet")
    assert result.exit_code != 0
    assert json.loads(result.output)["error"] == "host_blocked"


def test_captures_list_and_clean_work_on_the_config_folder(config_dir: Path) -> None:
    (capture.captures_dir(config_dir, "time-sheet") / "a.png").write_bytes(b"12345")
    listed = _run("browser", "captures", "list", "time-sheet")
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["count"] == 1
    cleaned = _run("browser", "captures", "clean", "time-sheet")
    assert json.loads(cleaned.output)["removed"] == 1
    assert json.loads(_run("browser", "captures", "list").output)["count"] == 0


def test_captures_clean_needs_a_task_or_all_and_not_both(config_dir: Path) -> None:
    assert _run("browser", "captures", "clean").exit_code == 2
    assert _run("browser", "captures", "clean", "time-sheet", "--all").exit_code == 2


def _wide_png(width: int = 3000, height: int = 900) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 100, 190)).save(buffer, format="PNG")
    return buffer.getvalue()


class _WidePage(_Page):
    def screenshot_png(self, **kwargs: Any) -> bytes:
        super().screenshot_png(**kwargs)
        return _wide_png()

    def frame_jpeg(self, quality: int = 60) -> bytes:
        self.frames += 1
        buffer = io.BytesIO()
        Image.new("RGB", (2400, 700), (200, 120, 20)).save(buffer, format="JPEG")
        return buffer.getvalue()


def test_a_wide_screenshot_is_shrunk_after_it_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _WidePage(_APP)
    monkeypatch.setattr(capture, "_attach_for_capture", lambda config, task: holder)
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert (result["width"], result["height"]) == (1600, 480)


def test_max_width_zero_keeps_the_displays_own_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _WidePage(_APP)
    monkeypatch.setattr(capture, "_attach_for_capture", lambda config, task: holder)
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet", max_width=0)
    assert (result["width"], result["height"]) == (3000, 900)


def test_a_narrow_screenshot_is_left_exactly_as_taken(tmp_path: Path, page: _Page) -> None:
    result = capture.screenshot_task(_setup(tmp_path), "time-sheet")
    assert Path(result["path"]).read_bytes() == _png()


def test_the_page_is_never_asked_to_resize_for_a_capture() -> None:
    """Asking Chromium for CSS-pixel shots resizes the page around each one: it flickers."""
    import ast
    import inspect
    import textwrap

    from blumkin.browser import cdp

    code = ""
    for name in (
        "screenshot_png",
        "screencast_start",
        "screencast_keyframe",
        "screencast_pump",
        "screencast_stop",
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(cdp.PlaywrightDriver, name))))
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef)
        function.body = [
            node
            for node in function.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        code += ast.unparse(function)
    assert "scale" not in code.replace("screencast", "").replace("Screencast", "")
    assert "Emulation" not in code and "setDeviceMetrics" not in code
    assert "clip" not in code and "set_viewport_size" not in code


# ----------------------------------------------------------------------- cleanup must stay inside


@pytest.mark.parametrize("task", ["..", ".", "", "a/b", "../evil", "/etc"])
def test_clean_and_list_refuse_a_task_that_is_not_one_plain_segment(
    tmp_path: Path, task: str
) -> None:
    """`captures clean ..` once pruned the whole config directory (token cache included)."""
    sentinel = tmp_path / "config.toml"
    sentinel.write_text("keep me")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "token").write_text("secret")
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "important.txt").write_text("keep me too")
    capture.captures_dir(tmp_path, "time-sheet")

    with pytest.raises(ValueError, match="plain path segment"):
        capture.clean_captures(tmp_path, task)
    with pytest.raises(ValueError, match="plain path segment"):
        capture.list_captures(tmp_path, task)

    assert sentinel.read_text() == "keep me"
    assert (tmp_path / "profiles" / "token").read_text() == "secret"
    assert (outside / "important.txt").exists()


def test_a_symlinked_captures_root_or_task_folder_is_refused_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "keep.txt").write_text("x")
    config = tmp_path / "config"
    config.mkdir()
    (config / "captures").symlink_to(real, target_is_directory=True)

    for call in (
        lambda: capture.captures_dir(config, "time-sheet"),
        lambda: capture.clean_captures(config),
        lambda: capture.list_captures(config),
    ):
        with pytest.raises(ValueError, match="not a symlink"):
            call()
    assert (real / "keep.txt").exists()

    (config / "captures").unlink()
    (config / "captures").mkdir()
    (config / "captures" / "time-sheet").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="not a symlink"):
        capture.clean_captures(config, "time-sheet")
    assert (real / "keep.txt").exists()


def test_clean_all_skips_a_task_folder_that_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "keep.txt").write_text("x")
    folder = capture.captures_dir(tmp_path, "time-sheet")
    (folder / "old.png").write_bytes(b"x")
    (tmp_path / "captures" / "linked").symlink_to(real, target_is_directory=True)

    removed = capture.clean_captures(tmp_path)

    assert removed == 1 and (real / "keep.txt").exists()
    assert [item["task"] for item in capture.list_captures(tmp_path)] == []


def _running_recording(folder: Path) -> Path:
    session = folder / "20261120T000000000Z-recording"
    (session / "frames").mkdir(parents=True)
    (session / "index.jsonl").write_text("{}\n")
    (folder / "recording.json").write_text(
        json.dumps({"pid": 1, "session": session.name, "started": 0.0, "task": "time-sheet"})
    )
    return session


def test_cleanup_leaves_a_running_recording_alone(tmp_path: Path) -> None:
    """`captures clean` deleted a recording's state and index while its worker kept running."""
    folder = capture.captures_dir(tmp_path, "time-sheet")
    session = _running_recording(folder)
    (folder / "old.png").write_bytes(b"x")

    removed = capture.clean_captures(tmp_path, "time-sheet")

    assert removed == 1  # only the old screenshot
    assert (folder / "recording.json").exists()
    assert (session / "index.jsonl").exists() and (session / "frames").is_dir()


def test_pruning_old_files_keeps_a_running_recordings_empty_frames_folder(tmp_path: Path) -> None:
    folder = capture.captures_dir(tmp_path, "time-sheet")
    session = _running_recording(folder)

    capture.prune_captures(folder, 0.0)

    assert (session / "frames").is_dir()


def test_a_recording_that_ended_is_cleaned_like_any_other_capture(tmp_path: Path) -> None:
    folder = capture.captures_dir(tmp_path, "time-sheet")
    session = _running_recording(folder)
    (folder / "recording.json").unlink()  # `record stop` removed its state

    capture.clean_captures(tmp_path, "time-sheet")

    assert not session.exists()


def test_the_clean_command_refuses_a_task_that_would_leave_the_captures_folder(
    config_dir: Path,
) -> None:
    sentinel = config_dir / "keep.txt"
    sentinel.write_text("keep me")

    result = _run("browser", "captures", "clean", "..")

    assert result.exit_code != 0 and sentinel.read_text() == "keep me"


def test_listing_skips_a_file_that_a_clean_removed_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "captures" / "time-sheet"
    directory.mkdir(parents=True)
    (directory / "keep.png").write_bytes(b"x")
    (directory / "gone.png").write_bytes(b"x")
    real_stat = Path.stat

    def stat(self: Path, **kwargs: Any) -> Any:
        if self.name == "gone.png":
            raise FileNotFoundError(self)
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    listed = capture.list_captures(tmp_path, "time-sheet")

    assert [Path(item["path"]).name for item in listed] == ["keep.png"]


def test_a_screenshot_that_cannot_be_written_leaves_no_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "shot.png"
    real_fdopen = capture.os.fdopen

    def failing(descriptor: int, mode: str) -> Any:
        handle = real_fdopen(descriptor, mode)
        handle.write = lambda data: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(capture.os, "fdopen", failing)

    with pytest.raises(OSError, match="disk full"):
        capture._write_private_bytes(target, b"x")

    assert not target.exists()  # so a retry to the same name is not refused


def test_a_page_that_loaded_another_document_during_the_capture_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)  # the address reads the same at both ends...
    holder.signatures = [
        (("main", "doc-1"),),
        (("main", "doc-2"),),
    ]  # ...but a document came between

    with pytest.raises(BrowserActionError, match="loaded or changed a frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert holder.shots != []  # the picture was taken
    assert not list((tmp_path / "captures" / "time-sheet").glob("*.png"))  # and never saved


def test_a_new_frame_in_the_page_during_the_capture_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    holder.signatures = [(("main", "doc-1"),), (("child", "doc-9"), ("main", "doc-1"))]

    with pytest.raises(BrowserActionError, match="loaded or changed a frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert not list((tmp_path / "captures" / "time-sheet").glob("*.png"))


def test_a_page_whose_documents_cannot_be_read_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    holder.signatures = [BrowserActionError("no answer")]

    with pytest.raises(BrowserActionError, match="no answer"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert holder.shots == []  # nothing was even taken


def test_the_same_documents_before_and_after_keep_the_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    holder.signatures = [(("main", "doc-1"), ("child", "doc-2"))]

    result = capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert Path(result["path"]).exists()


def test_a_frame_that_came_and_went_during_the_capture_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sign-in iframe attached and detached between the two reads leaves both reads equal."""
    holder = _use(monkeypatch, _APP)
    holder.watch_changes = 2  # attached, then detached
    holder.signatures = [(("main", "doc-1"),)]  # the same tree at both ends

    with pytest.raises(BrowserActionError, match="loaded or changed a frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert not list((tmp_path / "captures" / "time-sheet").glob("*.png"))


def test_a_capture_waits_briefly_at_its_end_for_an_event_still_on_its_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _use(monkeypatch, _APP)
    waits: list[int] = []
    holder.wait_ms = waits.append  # type: ignore[method-assign]

    capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert waits == [capture._EVENT_SETTLE_MS]


def test_a_frame_event_that_arrives_only_during_the_settle_wait_is_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count is read after the wait: an event still on its way when the picture ends counts."""
    holder = _use(monkeypatch, _APP)
    holder.late_changes = 1  # nothing before the wait; one delivered during it
    holder.signatures = [(("main", "doc-1"),)]  # and the same tree at both ends

    with pytest.raises(BrowserActionError, match="loaded or changed a frame"):
        capture.screenshot_task(_setup(tmp_path), "time-sheet")

    assert not list((tmp_path / "captures" / "time-sheet").glob("*.png"))
