"""Real-browser check of launch, attach, fill, upload and detach (issues #465 and #466).

Needs an installed Chrome, Edge or Chromium and `BLUMKIN_BROWSER_TESTS=1`; it is never
run in CI. It launches a headless browser on a throwaway profile, serves a local form
from an ephemeral port, and always stops the browser it started.
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.cdp import attach
from blumkin.browser.discovery import BrowserNotFoundError, resolve_browser
from blumkin.browser.guard import GuardedDriver, GuardPolicy, SubmitBlockedError
from blumkin.browser.launcher import launch
from blumkin.browser.profiles import create_profile, read_session
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS

pytestmark = pytest.mark.browser

_FORM = b"""<!doctype html><title>Expenses</title>
<label for="amount">Amount</label><input id="amount">
<label for="receipt">Receipt</label><input id="receipt" type="file">
<p>Total due 12.50</p>"""

_SUBMIT_FORM = b"""<!doctype html><title>Report</title>
<form action="/submit" method="post">
<label for="amount">Amount</label><input id="amount" name="amount">
<button type="button">Add line</button>
<button type="submit">Send report</button>
<input type="submit" value="Go">
</form>"""


_BYPASS_FORM = b"""<!doctype html><title>Bypass</title>
<form action="/submit" method="post">
<label for="amount">Amount</label><input id="amount" name="amount">
<button type="bogus">Next</button>
<button type="button" formaction="/other">Alt</button>
<button type="button" id="tidy">Tidy</button>
<input id="go" type="submit" value="Go now" aria-label="Go now">
<button type="button">Plain</button>
</form>
<a href="javascript:document.forms[0].submit()">Continue</a>
<script>
document.getElementById('tidy').addEventListener('click', () => document.forms[0].submit());
</script>"""


_EXPENSE_FORM = b"""<!doctype html><title>Expense report</title>
<form action="/submit" method="post">
<label for="amount">Amount</label><input id="amount" name="amount">
<label for="receipt">Receipt</label><input id="receipt" type="file">
<button type="button" id="add">Add line</button>
<p id="lines">0 lines</p><p id="picked">no file</p>
<button type="submit">Submit report</button>
</form>
<script>
let lines = 0;
document.getElementById('add').addEventListener('click', () => {
  lines += 1; document.getElementById('lines').textContent = lines + ' lines';
});
document.getElementById('receipt').addEventListener('change', (event) => {
  document.getElementById('picked').textContent = 'picked ' + event.target.files[0].name;
});
</script>"""

_TIMESHEET_FORM = b"""<!doctype html><title>Time sheet</title>
<form action="/submit" method="post">
<label for="mon">Monday</label><input id="mon" name="mon">
<label for="tue">Tuesday</label><input id="tue" name="tue">
<button type="submit">Submit time sheet</button>
</form>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    posts: list[str] = []

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        pages = {
            "/bypass": _BYPASS_FORM,
            "/expense": _EXPENSE_FORM,
            "/report": _SUBMIT_FORM,
            "/timesheet": _TIMESHEET_FORM,
        }
        self.wfile.write(pages.get(self.path, _FORM))

    def do_POST(self) -> None:
        type(self).posts.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def form_url() -> Iterator[str]:
    _Handler.posts = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launch_attach_fill_upload_read_and_detach_leaves_the_browser_running(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    profile = create_profile(tmp_path, "expense-report")
    receipt = tmp_path / "receipt.png"
    receipt.write_bytes(b"not really a png")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        driver = attach(profile)
        driver.goto(form_url)
        driver.fill("Amount", "12.50")
        driver.upload("Receipt", receipt)
        page_text = driver.read_text()
        assert driver.title() == "Expenses"
        assert "Total due 12.50" in page_text.text
        assert page_text.injection_warning is None
        driver.close()
        assert read_session(profile.path) == result.session
        os.kill(result.session.pid, 0)
        # Detaching must leave the user's session intact: a fresh attach finds the same page.
        again = attach(profile)
        assert again.title() == "Expenses"
        assert again.url == form_url
        again.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_the_guard_stops_every_way_of_submitting_a_real_form(tmp_path: Path, form_url: str) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        driver = GuardedDriver(
            attach(profile),
            GuardPolicy(allowed_controls=frozenset({"Add line", "Send report", "Go"})),
        )
        driver.goto(form_url + "report")
        driver.fill("Amount", "12.50")
        driver.click("Add line")
        with pytest.raises(SubmitBlockedError):
            driver.click("Send report")
        with pytest.raises(SubmitBlockedError):
            driver.click("Go")
        with pytest.raises(SubmitBlockedError):
            driver.press("Amount", "Enter")
        driver.close()
        assert _Handler.posts == []
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_the_guard_stops_the_known_bypasses_and_the_net_is_gone_after_detaching(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    allowed = frozenset({"Next", "Alt", "Tidy", "Continue", "Plain", "Go now"})
    try:
        driver = GuardedDriver(attach(profile), GuardPolicy(allowed_controls=allowed))
        driver.goto(form_url + "bypass")
        driver.fill("Amount", "12.50")
        driver.click("Plain")
        # A button whose invalid type the browser treats as submit, a formaction override,
        # a script link and a submit input reached by its aria-label.
        for name, role in (
            ("Next", "button"),
            ("Alt", "button"),
            ("Continue", "link"),
            ("Go now", "button"),
        ):
            with pytest.raises(SubmitBlockedError):
                driver.click(name, role)
        # Keys that activate a submit control or submit from a field.
        for key in ("Enter", "\n", "\r", " ", "Space", "Control+Enter"):
            with pytest.raises(SubmitBlockedError):
                driver.press("Amount", key)
        with pytest.raises(SubmitBlockedError):
            driver.press("Go now", "a")
        driver.press("Amount", "Tab")
        # No static check sees this one (a plain button whose listener submits the form):
        # the guard lets the click through and the safety net swallows the POST.
        driver.click("Tidy")
        driver.read_text()
        assert _Handler.posts == []
        driver.close()

        # Detached, the swallowed submit left the user's page in place and nothing blocks
        # the user's own final click.
        raw = attach(profile)
        assert raw.title() == "Bypass"
        assert raw.url == form_url + "bypass"
        raw.checked_click("Go now", "button", lambda info: None)
        raw.close()
        assert _wait_for(lambda: _Handler.posts == ["/submit"])
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def _wait_for(condition: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    """Poll a condition against a deadline (the real browser's request is asynchronous)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_the_cli_commands_fill_a_real_form_and_never_submit_it(
    tmp_path: Path, form_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[browser.tasks.expense-report]\nallowed_controls = ["Add line", "Send report", "Go"]\n\n'
        '[profiles.default]\nclient_id = "abc-123"\n'
    )
    profile = create_profile(config, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    runner = CliRunner()

    def cli(*args: str) -> Any:
        return runner.invoke(main, ["--json", "browser", *args])

    try:
        assert cli("goto", "expense-report", form_url + "report").exit_code == EXIT_SUCCESS
        assert cli("fill", "expense-report", "Amount", "12.50").exit_code == EXIT_SUCCESS
        assert cli("click", "expense-report", "Add line").exit_code == EXIT_SUCCESS
        read = cli("read", "expense-report")
        assert read.exit_code == EXIT_SUCCESS
        assert "Add line" in json.loads(read.output)["text"]
        for name in ("Send report", "Go"):
            refused = cli("click", "expense-report", name)
            assert refused.exit_code == EXIT_OTHER
            assert json.loads(refused.output)["error"] == "submit_blocked"
        pressed = cli("press", "expense-report", "Amount", "Enter")
        assert pressed.exit_code == EXIT_OTHER
        assert json.loads(pressed.output)["error"] == "submit_blocked"
        assert _Handler.posts == []
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def _launched_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str, policy: str
) -> tuple[Any, Any, Any]:
    """A throwaway config with one task policy, its profile and a headless browser on it."""
    try:
        browser = resolve_browser(None)
    except BrowserNotFoundError:
        pytest.skip("no Chrome, Edge or Chromium installed")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(f"[browser.tasks.{task}]\n{policy}\n")
    profile = create_profile(config, task)
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    runner = CliRunner()

    def cli(*args: str) -> Any:
        return runner.invoke(main, ["--json", "browser", *args])

    return cli, result, config


def test_the_expense_report_skill_dry_run_fills_the_form_and_ends_in_the_handoff(
    tmp_path: Path, form_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    receipt = receipts / "taxi.png"
    receipt.write_bytes(b"not really a png")
    policy = (
        'allowed_controls = ["Add line"]\n'
        f'upload_dirs = ["{receipts}"]\n'
        'allowed_hosts = ["127.0.0.1"]\n'
    )
    cli, result, _ = _launched_cli(tmp_path, monkeypatch, "expense-report", policy)
    try:
        assert cli("goto", "expense-report", form_url + "expense").exit_code == EXIT_SUCCESS
        assert cli("fill", "expense-report", "Amount", "12.50").exit_code == EXIT_SUCCESS
        upload = cli("upload", "expense-report", "Receipt", str(receipt))
        assert upload.exit_code == EXIT_SUCCESS, upload.output
        assert cli("click", "expense-report", "Add line").exit_code == EXIT_SUCCESS
        page = json.loads(cli("read", "expense-report").output)
        assert "1 lines" in page["text"]
        assert "picked taxi.png" in page["text"]
        refused = cli("click", "expense-report", "Submit report")
        assert refused.exit_code == EXIT_OTHER
        assert json.loads(refused.output)["error"] == "submit_blocked"
        outside = cli("upload", "expense-report", "Receipt", str(tmp_path / "other.png"))
        assert outside.exit_code == EXIT_OTHER
        handoff = cli(
            "handoff",
            "expense-report",
            "--filled",
            "1 line, 12.50",
            "--step",
            "Review the line.",
            "--step",
            "Press Submit report.",
        )
        payload = json.loads(handoff.output)
        assert handoff.exit_code == EXIT_SUCCESS
        assert payload["submitted"] is False
        assert payload["needs_user_action"] is True
        assert payload["instructions"] == ["Review the line.", "Press Submit report."]
        assert _Handler.posts == []
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_the_time_sheet_skill_dry_run_fills_the_hours_and_ends_in_the_handoff(
    tmp_path: Path, form_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    cli, result, _ = _launched_cli(
        tmp_path, monkeypatch, "time-sheet", 'allowed_hosts = ["127.0.0.1"]\n'
    )
    try:
        assert cli("goto", "time-sheet", form_url + "timesheet").exit_code == EXIT_SUCCESS
        assert cli("fill", "time-sheet", "Monday", "8").exit_code == EXIT_SUCCESS
        assert cli("fill", "time-sheet", "Tuesday", "--", "-1").exit_code == EXIT_SUCCESS
        refused = cli("click", "time-sheet", "Submit time sheet")
        assert refused.exit_code == EXIT_OTHER
        assert json.loads(refused.output)["error"] == "submit_blocked"
        handoff = cli("handoff", "time-sheet", "--step", "Review the week.")
        payload = json.loads(handoff.output)
        assert payload["submitted"] is False
        assert _Handler.posts == []
    finally:
        os.kill(result.session.pid, signal.SIGTERM)
