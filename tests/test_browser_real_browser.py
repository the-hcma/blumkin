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
from typing import Any, cast

import pytest
from click.testing import CliRunner

from blumkin.browser.cdp import attach
from blumkin.browser.discovery import (
    BrowserKind,
    BrowserNotFoundError,
    ResolvedBrowser,
    resolve_browser,
)
from blumkin.browser.drift import Grade, check_page
from blumkin.browser.explore import (
    Explorer,
    ExploreRefusedError,
    ExploringDriver,
    Inspection,
    ModelDraft,
    parse_aria_snapshot,
)
from blumkin.browser.guard import GuardedDriver, GuardPolicy, SubmitBlockedError
from blumkin.browser.launcher import close_browser, launch
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


def _explore_page(port: int) -> bytes:
    return (
        "<!doctype html><title>Explore</title><h1>Expenses</h1>"
        '<a href="/help">Help</a>'
        f'<a href="http://localhost:{port}/elsewhere">Other host</a>'
        '<a href="/redir">Redirecting</a>'
        '<form action="/submit" method="post"><label for="a">Amount</label>'
        '<input id="a" name="a"><button type="button">Add line</button>'
        '<button type="submit">Submit report</button></form>'
    ).encode()


_DRIFT_BASE = (
    "<!doctype html><title>Drift</title><h1>Expenses</h1>"
    '<nav><a href="/help">Help</a></nav>'
    '<label for="a">Amount</label><input id="a">'
    '<button type="button">{button}</button>'
    "{extra}"
)


def _drift_page(button: str = "Add line", extra: str = "", reverse: bool = False) -> bytes:
    page = _DRIFT_BASE.format(button=button, extra=extra)
    if reverse:
        page = (
            "<!doctype html><title>Drift</title>"
            f'<button type="button">{button}</button>'
            '<label for="a">Amount</label><input id="a">'
            '<nav><a href="/help">Help</a></nav><h1>Expenses</h1>' + extra
        )
    return page.encode()


_BACKGROUND_FORM = b"""<!doctype html><title>Background</title>
<label for="amount">Amount</label><input id="amount" name="amount">
<button type="button" id="save">Save line</button>
<p id="state">idle</p>
<script>
document.getElementById('save').addEventListener('click', () => {
  fetch('/save', {method: 'POST', body: document.getElementById('amount').value})
    .then((r) => { document.getElementById('state').textContent = 'saved ' + r.status; });
});
</script>"""


_GET_FORM = b"""<!doctype html><title>Search</title>
<form id="search" action="/search" method="get">
<label for="q">Query</label><input id="q" name="q">
<button type="button" id="go">Search now</button>
</form>
<script>
document.getElementById('go').addEventListener('click', () => {
  document.getElementById('search').submit();
});
</script>"""


_REPEATED_NAMES = b"""<!doctype html><title>Repeated</title>
<ul>
<li><button type="button" onclick="pick(1)">More actions</button></li>
<li><button type="button" onclick="pick(2)">More actions</button></li>
<li><button type="button" onclick="pick(3)">More actions</button></li>
</ul>
<form action="/submit" method="post"><button type="submit">More actions</button></form>
<p id="picked">none</p>
<script>
function pick(n) { document.getElementById('picked').textContent = 'picked ' + n; }
</script>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    posts: list[str] = []
    hosts: list[str] = []

    def do_GET(self) -> None:
        port = cast(tuple[str, int], self.server.server_address)[1]
        type(self).hosts.append(f"{self.headers.get('Host', '')} {self.path}")
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/landing")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        pages = {
            "/background": _BACKGROUND_FORM,
            "/bypass": _BYPASS_FORM,
            "/drift-a": _drift_page(),
            "/drift-b": _drift_page(button="Add row"),
            "/drift-c": _drift_page(extra='<label for="c">Cost center</label><input id="c">'),
            "/drift-d": _drift_page(reverse=True),
            "/drift-e": _drift_page(extra='<button type="button">Export</button>'),
            "/explore": _explore_page(port),
            "/expense": _EXPENSE_FORM,
            "/getform": _GET_FORM,
            "/repeated": _REPEATED_NAMES,
            "/report": _SUBMIT_FORM,
            "/timesheet": _TIMESHEET_FORM,
        }
        self.wfile.write(pages.get(self.path, _FORM))

    def do_POST(self) -> None:
        type(self).posts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def form_url() -> Iterator[str]:
    _Handler.posts = []
    _Handler.hosts = []
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
    browser = _installed_browser()
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
    browser = _installed_browser()
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
    browser = _installed_browser()
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


def _installed_browser() -> ResolvedBrowser:
    """The first installed browser of any supported kind; skip the test when there is none."""
    for kind in BrowserKind:
        try:
            return resolve_browser(kind.value)
        except BrowserNotFoundError:
            continue
    pytest.skip("no Chrome, Edge or Chromium installed")


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
    browser = _installed_browser()
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
    browser = _installed_browser()
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


class _NoQuestions:
    def confirm(self, prompt: str) -> bool:
        raise AssertionError(f"nothing here should ask: {prompt}")


def test_exploration_reads_a_real_page_inspects_without_clicking_and_stays_on_the_host(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        raw = attach(profile)
        raw.goto(form_url + "explore")
        driver = ExploringDriver(raw, allowed_hosts=("127.0.0.1",))
        snapshot = driver.aria_snapshot()
        assert 'link "Help"' in snapshot
        assert 'button "Add line"' in snapshot
        assert 'textbox "Amount"' in snapshot

        assert driver.inspect("link", "Help") == Inspection(refusal=None, href="/help", tag="a")
        assert driver.inspect("button", "Submit report").refusal == "it is a submit control"
        assert driver.url == form_url + "explore"
        assert _Handler.posts == []

        explorer = Explorer(driver, _NoQuestions(), allowed_hosts=("127.0.0.1",))
        assert explorer.click("link", "Help") is True
        assert driver.url == form_url + "help"
        raw.goto(form_url + "explore")

        # A link to another host is refused before it is clicked.
        assert explorer.click("link", "Other host") is False
        assert "localhost" in explorer.last_outcome

        # Even clicked directly, the navigation is aborted before the request reaches that host.
        inspection = driver.inspect("link", "Other host")
        try:
            driver.click_vetted("link", "Other host", inspection)
        except Exception:  # noqa: BLE001 - an aborted navigation may surface as an error
            pass
        assert not any(entry.startswith("localhost") for entry in _Handler.hosts), _Handler.hosts

        # A redirect from an allowed host to another one is not intercepted (the browser follows
        # it inside its network stack), so the other host does see that GET. It is detected as
        # soon as the click settles, and the command says the click happened.
        raw.goto(form_url + "explore")
        with pytest.raises(ExploreRefusedError, match="click happened"):
            explorer.click("link", "Redirecting")
        assert _Handler.posts == []
        driver.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_drift_is_graded_against_a_real_site_that_is_then_edited(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        raw = attach(profile)
        driver = ExploringDriver(raw, allowed_hosts=("127.0.0.1",))

        def observe(path: str) -> Any:
            raw.goto(form_url + path)
            return parse_aria_snapshot(driver.aria_snapshot())

        draft = ModelDraft(task="expense-report", hosts=("127.0.0.1",))
        draft.add_page("expense-list", form_url + "drift-a", observe("drift-a"))
        page = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]

        assert check_page(page, observe("drift-a")).grade is Grade.COMPATIBLE
        # The same controls in another order: layout is ignored.
        assert check_page(page, observe("drift-d")).grade is Grade.COMPATIBLE
        # A new button is not drift.
        assert check_page(page, observe("drift-e")).grade is Grade.COMPATIBLE
        # A renamed control is gone.
        renamed = check_page(page, observe("drift-b"))
        assert renamed.grade is Grade.INCOMPATIBLE
        missing = [f.control for f in renamed.findings if f.kind.value == "missing"]
        assert missing == ["expense-list.add-line"]
        # A new field may be required.
        added = check_page(page, observe("drift-c"))
        assert added.grade is Grade.INCOMPATIBLE
        assert "new_field" in [f.kind.value for f in added.findings]
        assert _Handler.posts == []
        driver.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_a_background_write_goes_through_by_default_and_not_in_strict_mode(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    allowed = frozenset({"Save line"})
    try:
        # Default mode: the declared button's handler posts with fetch and the server gets it
        # (documented: the net only stops navigations).
        driver = GuardedDriver(attach(profile), GuardPolicy(allowed_controls=allowed))
        driver.goto(form_url + "background")
        driver.fill("Amount", "12.50")
        driver.click("Save line")
        assert _wait_for(lambda: "saved 200" in driver.read_text().text)
        assert _Handler.posts == ["/save"]
        driver.close()

        # Strict mode: the same click is answered with an empty 204 and the server receives
        # nothing, while the page keeps what the user typed.
        _Handler.posts = []
        strict = GuardedDriver(
            attach(profile),
            GuardPolicy(allowed_controls=allowed, block_background_writes=True),
        )
        strict.goto(form_url + "background")
        strict.fill("Amount", "99.00")
        strict.click("Save line")
        assert _wait_for(lambda: "saved 204" in strict.read_text().text)
        assert _Handler.posts == []

        strict.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_a_script_submitted_get_form_reaches_the_server_with_the_net_installed(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    allowed = frozenset({"Search now"})
    try:
        # A GET form is a GET navigation, which the net lets through in both modes (#562).
        for strict in (False, True):
            _Handler.hosts = []
            driver = GuardedDriver(
                attach(profile),
                GuardPolicy(allowed_controls=allowed, block_background_writes=strict),
            )
            driver.goto(form_url + "getform")
            driver.fill("Query", "rent")
            driver.click("Search now")
            # The server answers /search with the generic form page; reading the page on each
            # poll also keeps Playwright servicing the route while the navigation happens.
            assert _wait_for(lambda: "Total due" in driver.read_text().text), _Handler.hosts
            assert any(h.endswith("/search?q=rent") for h in _Handler.hosts), _Handler.hosts
            driver.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_a_repeated_name_is_refused_unless_nth_says_which_one(
    tmp_path: Path, form_url: str
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "expense-report")
    result = launch(profile, browser, extra_args=("--headless=new", "--no-sandbox"))
    try:
        driver = GuardedDriver(
            attach(profile), GuardPolicy(allowed_controls=frozenset({"More actions"}))
        )
        driver.goto(form_url + "repeated")
        # Default: refuse rather than guess, and click nothing.
        with pytest.raises(Exception, match="got 4 buttons named 'More actions'"):
            driver.click("More actions", "button", 1.0)
        assert "none" in driver.read_text().text
        # Explicit: the second match in page order is the one clicked (#573).
        driver.click("More actions", "button", 1.0, nth=2)
        assert _wait_for(lambda: "picked 2" in driver.read_text().text)
        # The fourth match is a submit button in a form: --nth does not get past the vetting.
        with pytest.raises(SubmitBlockedError):
            driver.click("More actions", "button", 1.0, nth=4)
        assert _Handler.posts == []
        # Beyond the matches: refused with the real count.
        with pytest.raises(Exception, match="at least 5 buttons named 'More actions', got 4"):
            driver.click("More actions", "button", 1.0, nth=5)
        driver.close()
    finally:
        os.kill(result.session.pid, signal.SIGTERM)


def test_close_ends_a_real_browser_and_keeps_the_profile_for_the_next_launch(
    tmp_path: Path,
) -> None:
    if os.environ.get("BLUMKIN_BROWSER_TESTS") != "1":
        pytest.skip("set BLUMKIN_BROWSER_TESTS=1 to run real-browser tests")
    browser = _installed_browser()
    profile = create_profile(tmp_path, "youtube-transcript")
    extra = ("--headless=new", "--no-sandbox")
    first = launch(profile, browser, extra_args=extra)
    pid = first.session.pid
    try:
        # A file in the profile stands for the saved sign-in: closing must not touch it.
        (profile.path / "Saved sign-in").write_text("kept", encoding="utf-8")
        closed = close_browser(profile)
        assert closed.was_running is True and closed.forced is False
        assert read_session(profile.path) is None
        assert not _process_is_running(pid)
        assert (profile.path / "Saved sign-in").read_text(encoding="utf-8") == "kept"
        # Closing again is a calm no-op, and the profile launches again.
        assert close_browser(profile).was_running is False
        second = launch(profile, browser, extra_args=extra)
        assert second.already_running is False
        pid = second.session.pid
    finally:
        close_browser(profile, force=True)
    assert not _process_is_running(pid)


def _process_is_running(pid: int) -> bool:
    """Alive and not a zombie: the launched browser is a child of this test process."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        done = 0
    if done == pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
