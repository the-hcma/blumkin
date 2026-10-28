"""Real-browser run of the transcript commands against a recorded-shape Teams pane (#544).

Needs an installed Chrome, Edge or Chromium and `BLUMKIN_BROWSER_TESTS=1`; it is never run in
CI. The page imitates what shaped the transcript commands, with synthetic text only: a pane
served from another host inside an iframe that attaches late, a virtualized list that keeps
only the rows near the viewport in the page, a tab that reloads the pane only when another tab
was shown first, and a control that appears after the page loaded. The pane is served from
`localhost` while the meeting is on `127.0.0.1`, which Chrome treats as different sites, so the
pane is an out-of-process iframe like the real one.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from blumkin.browser.discovery import (
    BrowserKind,
    BrowserNotFoundError,
    ResolvedBrowser,
    resolve_browser,
)
from blumkin.browser.launcher import launch
from blumkin.browser.profiles import create_profile
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS

pytestmark = pytest.mark.browser

#: How long after the meeting page loads (or a tab switch) the pane's iframe is attached. The
#: commands under test attach to the browser fresh, and a connection made after the iframe
#: exists cannot see it (#551), so this has to comfortably outlast one command's start-up.
_PANE_DELAY_MS = 4000
_TURNS = 60
_SECONDS_PER_TURN = 20
_SPEAKERS = ("Lovelace, Ada (Example Corp)", "Turing, Alan (Example Corp)")

_MEETING = """<!doctype html><title>Example meeting</title>
<div role="tablist">
<button role="tab" id="notes" aria-selected="false">Notes</button>
<button role="tab" id="transcript" aria-selected="true">Transcript</button>
</div>
<p id="mounts">mounts: 0</p>
<div id="host"></div>
<script>
let mounts = 0;
const host = document.getElementById('host');
function mount() {
  mounts += 1;
  document.getElementById('mounts').textContent = 'mounts: ' + mounts;
  host.innerHTML = '';
  setTimeout(() => {
    const frame = document.createElement('iframe');
    frame.src = 'http://localhost:__PANE_PORT__/pane';
    frame.width = '600'; frame.height = '320';
    host.appendChild(frame);
  }, __PANE_DELAY_MS__);
}
function select(id) {
  for (const tab of document.querySelectorAll('[role=tab]')) {
    tab.setAttribute('aria-selected', String(tab.id === id));
  }
}
document.getElementById('notes').addEventListener('click', () => {
  select('notes'); host.innerHTML = '<p>Meeting notes</p>';
});
document.getElementById('transcript').addEventListener('click', () => {
  if (document.getElementById('transcript').getAttribute('aria-selected') === 'true') return;
  select('transcript'); mount();
});
setTimeout(() => {
  const late = document.createElement('button');
  late.textContent = 'Late action';
  document.body.appendChild(late);
}, 1500);
mount();
</script>"""

_PANE = """<!doctype html><title>Pane</title>
<style>
#list { height: 300px; overflow-y: auto; position: relative; }
.row { position: absolute; left: 0; right: 0; height: 40px; }
</style>
<div id="list"><div id="spacer" style="height: __HEIGHT__px"></div></div>
<script>
const speakers = __SPEAKERS__;
const list = document.getElementById('list');
function clock(seconds) {
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
}
function words(seconds) {
  return Math.floor(seconds / 60) + ' minutes ' + (seconds % 60) + ' seconds';
}
function render() {
  const first = Math.max(0, Math.floor(list.scrollTop / 40) - 1);
  const last = Math.min(__TURNS__ - 1, first + 10);
  for (const old of list.querySelectorAll('.row')) old.remove();
  for (let i = first; i <= last; i++) {
    const at = i * __STEP__;
    const speaker = speakers[i % 2];
    const row = document.createElement('div');
    row.className = 'row';
    row.style.top = (i * 40) + 'px';
    row.innerText = speaker + '\\n' + words(at) + '\\n' + clock(at) + '\\n' +
      speaker + ' ' + words(at) + '\\nTurn ' + i + ' point ' + i + '.';
    list.appendChild(row);
  }
  const end = document.getElementById('end');
  if (end) end.remove();
  if (last === __TURNS__ - 1) {
    const stop = document.createElement('div');
    stop.id = 'end'; stop.className = 'row'; stop.style.top = (__TURNS__ * 40) + 'px';
    stop.innerText = speakers[0] + '\\n\\n stopped transcription';
    list.appendChild(stop);
  }
}
list.addEventListener('scroll', render);
setTimeout(render, 800);
</script>"""


def _meeting_page(pane_port: int) -> bytes:
    return (
        _MEETING.replace("__PANE_PORT__", str(pane_port))
        .replace("__PANE_DELAY_MS__", str(_PANE_DELAY_MS))
        .encode()
    )


def _pane_page() -> bytes:
    return (
        _PANE.replace("__SPEAKERS__", json.dumps(list(_SPEAKERS)))
        .replace("__TURNS__", str(_TURNS))
        .replace("__STEP__", str(_SECONDS_PER_TURN))
        .replace("__HEIGHT__", str((_TURNS + 1) * 40))
        .encode()
    )


def _serve(body: Callable[[], bytes]) -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body())

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, cast(tuple[str, int], server.server_address)[1]


@pytest.fixture
def meeting_url() -> Iterator[str]:
    """The meeting page on 127.0.0.1, whose pane iframe is served from `localhost`."""
    pane, pane_port = _serve(_pane_page)
    meeting, meeting_port = _serve(lambda: _meeting_page(pane_port))
    try:
        yield f"http://127.0.0.1:{meeting_port}/"
    finally:
        for server in (meeting, pane):
            server.shutdown()
            server.server_close()


def _installed_browser() -> ResolvedBrowser:
    """The first of Chrome, Edge and Chromium that is installed, else skip the test."""
    for kind in BrowserKind:
        try:
            return resolve_browser(kind.value)
        except BrowserNotFoundError:
            continue
    pytest.skip("no Chrome, Edge or Chromium installed")


@pytest.fixture
def meeting_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meeting_url: str
) -> Iterator[Callable[..., Any]]:
    """`blumkin --json browser ...` against a headless browser already on the meeting page."""
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[browser.tasks.meeting]\nallowed_hosts = ["127.0.0.1", "localhost"]\n'
        'allowed_controls = ["Notes", "Transcript", "Late action"]\n'
    )
    profile = create_profile(config, "meeting")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    runner = CliRunner()

    def cli(*args: str) -> Any:
        return runner.invoke(main, ["--json", "browser", *args])

    try:
        assert cli("goto", "meeting", meeting_url).exit_code == EXIT_SUCCESS
        yield cli
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(result.session.pid, signal.SIGTERM)


def _poll(condition: Callable[[], bool], *, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def test_the_transcript_is_captured_whole_from_a_late_cross_origin_virtualized_pane(
    meeting_cli: Callable[..., Any], tmp_path: Path
) -> None:
    out = tmp_path / "transcript.txt"
    duration = str(_TURNS * _SECONDS_PER_TURN)

    result = meeting_cli("transcript", "meeting", "--duration", duration, "--out", str(out))

    assert result.exit_code == EXIT_SUCCESS, result.output
    coverage = json.loads(result.output)["coverage"]
    assert coverage["entries"] == _TURNS
    assert len(coverage["speakers"]) == 2
    assert coverage["complete"] is True, coverage["problems"]
    lines = out.read_text().splitlines()
    assert len(lines) == _TURNS
    assert lines[0].startswith("[0:00] Lovelace, Ada (Example Corp): Turn 0 point 0.")
    last = (_TURNS - 1) * _SECONDS_PER_TURN
    assert lines[-1].startswith(f"[{last // 60}:{last % 60:02d}] Turing, Alan")
    assert out.stat().st_mode & 0o777 == 0o600


def test_frames_waits_for_the_pane_to_attach_and_lists_only_its_host(
    meeting_cli: Callable[..., Any],
) -> None:
    frames = meeting_cli("frames", "meeting", "--wait", "20")

    assert frames.exit_code == EXIT_SUCCESS, frames.output
    listed = json.loads(frames.output)["frames"]
    assert [(f["host"], f["allowed"]) for f in listed] == [("localhost", True)]
    assert all("/pane" not in f["origin"] for f in listed)


def test_a_frame_read_waits_for_the_pane_to_attach_and_load(
    meeting_cli: Callable[..., Any],
) -> None:
    read = meeting_cli("read", "meeting", "--frame", "1", "--host", "localhost", "--wait", "20")

    assert read.exit_code == EXIT_SUCCESS, read.output
    assert "Turn 0 point 0." in json.loads(read.output)["text"]


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="a fresh attach cannot see a cross-origin frame that already exists (#551)",
)
def test_frames_on_a_page_that_has_settled_still_lists_the_pane(
    meeting_cli: Callable[..., Any],
) -> None:
    primed = meeting_cli("read", "meeting", "--frame", "1", "--wait", "20")
    if primed.exit_code != EXIT_SUCCESS:  # not the #551 symptom: a real regression
        raise RuntimeError(primed.output)

    frames = json.loads(meeting_cli("frames", "meeting", "--wait", "3").output)["frames"]

    assert [(f["host"], f["allowed"]) for f in frames] == [("localhost", True)]


def test_a_late_control_is_found_by_name_and_a_wrong_role_fails_fast(
    meeting_cli: Callable[..., Any],
) -> None:
    started = time.monotonic()
    wrong = meeting_cli("click", "meeting", "Late action", "--role", "link", "--wait", "2")
    assert wrong.exit_code == EXIT_OTHER, wrong.output
    assert time.monotonic() - started < 20

    clicked = meeting_cli("click", "meeting", "Late action", "--wait", "20")
    assert clicked.exit_code == EXIT_SUCCESS, clicked.output
    assert json.loads(clicked.output)["role"] == "button"


def test_refresh_tab_reloads_the_pane_that_clicking_the_selected_tab_does_not(
    meeting_cli: Callable[..., Any],
) -> None:
    def mounts() -> str:
        text = json.loads(meeting_cli("read", "meeting").output)["text"]
        return next(line for line in text.splitlines() if line.startswith("mounts:"))

    assert meeting_cli("click", "meeting", "Transcript", "--wait", "10").exit_code == EXIT_SUCCESS
    assert mounts() == "mounts: 1"

    refreshed = meeting_cli("refresh-tab", "meeting", "Transcript", "--via", "Notes")

    assert refreshed.exit_code == EXIT_SUCCESS, refreshed.output
    assert _poll(lambda: mounts() == "mounts: 2")
