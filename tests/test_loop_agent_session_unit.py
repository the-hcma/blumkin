"""`blumkin loop agent`: run the operator's agent in a pty and log the session (issue #538)."""

from __future__ import annotations

import fcntl
import json
import shlex
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE
from blumkin.loop.agent_task import agent_task, pending_items
from blumkin.loop.config import LoopConfigError, parse_loop_setup
from blumkin.loop.mail_intake import intake_dir
from blumkin.loop.types import RunContext, TriggerKind, TriggerSpec

_MESSAGE = "AAMkAGI2-message-one"

# A stand-in agent: prints a banner, waits for the prompt, then drives the real CLI.
_AGENT = textwrap.dedent(
    """
    import os, subprocess, sys, threading, time
    mode, item = sys.argv[1], sys.argv[2]
    print("\\x1b[1mready>\\x1b[0m", flush=True)
    if mode == "exit-early":
        sys.exit(3)
    prompt = sys.stdin.readline().strip()
    print("got: " + prompt, flush=True)
    if mode == "hang":
        sys.stdin.readline()
    if mode == "term":
        print("TERM=" + os.environ.get("TERM", "unset"), flush=True)
    if mode == "binary":
        sys.stdout.buffer.write(b"before \\xff\\xfe after\\n")
        sys.stdout.flush()
    if mode == "spinner":
        def spin():
            while True:
                print(".", end="", flush=True)
                time.sleep(0.05)
        threading.Thread(target=spin, daemon=True).start()
    if mode in ("handback", "term", "binary", "spinner"):
        subprocess.run(
            [sys.executable, "-m", "blumkin", "loop", "finish", item, "--needs-user", "unreadable"],
            check=True, capture_output=True,
        )
        print("handed back " + item, flush=True)
    line = sys.stdin.readline().strip()
    print("bye: " + line, flush=True)
    os._exit(0)
    """
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _write_config(config: Path, agent: str, extra: str = "", timeout: int = 120) -> None:
    (config / "agent.py").write_text(_AGENT, encoding="utf-8")
    (config / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "abc-123"',
                "",
                "[loop]",
                f"agent_command = {json.dumps(agent)}",
                "agent_quiet_seconds = 0.3",
                f"agent_timeout_seconds = {timeout}",
                extra,
                "",
                "[loop.triggers.requests]",
                'kind = "loop"',
                'task = "agent"',
                'instance = "requests"',
                'allowlist = ["me@example.com"]',
                'attachment_types = [".jpg"]',
                "",
                "[loop.triggers.requests.templates]",
                'report = "weekly-report"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _stand_in(config: Path, mode: str, item: str = "none") -> str:
    return shlex.join([sys.executable, str(config / "agent.py"), mode, item])


def _queue(config: Path) -> str:
    trigger = TriggerSpec(
        allowlist=("me@example.com",),
        attachment_types=(".jpg",),
        instance="requests",
        kind=TriggerKind.LOOP,
        name="requests",
        task="agent",
        templates=(("report", "weekly-report"),),
    )
    directory = intake_dir(config, trigger="requests", message_id=_MESSAGE)
    (directory / "scan.jpg").write_bytes(b"x")
    agent_task(
        trigger,
        context=RunContext(
            config_dir=config,
            instance="requests",
            message_id=_MESSAGE,
            profile="personal",
            subject="Report's ready",
        ),
    )
    (item,) = pending_items(config)
    return item.id


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", "loop", "agent", *args])


def _log_text(config: Path) -> str:
    (log,) = sorted((config / "loop" / "agent-logs").glob("*.log"))
    return log.read_text(encoding="utf-8")


def test_agent_settings_parse_from_the_loop_table(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "agent_command": "my-agent --flag 'a b'",
                "agent_exit_input": "/quit",
                "agent_prompt": "go",
                "agent_quiet_seconds": 2,
                "agent_timeout_seconds": 60,
            }
        },
    )
    assert setup.loop.agent_command == ("my-agent", "--flag", "a b")
    assert setup.loop.agent_exit_input == "/quit"
    assert setup.loop.agent_prompt == "go"
    assert setup.loop.agent_quiet_seconds == 2
    assert setup.loop.agent_timeout_seconds == 60


def test_agent_settings_default_prompt_and_exit_input(tmp_path: Path) -> None:
    setup = parse_loop_setup(tmp_path, {"loop": {}})
    assert setup.loop.agent_command == ()
    assert setup.loop.agent_prompt == "run the blumkin loop"
    assert setup.loop.agent_exit_input == "/exit"


@pytest.mark.parametrize(
    "bad",
    [
        {"agent_command": ""},
        {"agent_command": 5},
        {"agent_command": "unterminated 'quote"},
        {"agent_prompt": ""},
        {"agent_timeout_seconds": 0},
        {"agent_quiet_seconds": 0},
    ],
)
def test_bad_agent_settings_fail(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(LoopConfigError):
        parse_loop_setup(tmp_path, {"loop": bad})


def test_nothing_waiting_starts_no_agent(config: Path) -> None:
    _write_config(config, "/nonexistent/agent-binary")
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["started"] is False
    assert payload["handled"] == []
    assert not (config / "loop" / "agent-logs").exists()


def test_waiting_items_without_an_agent_command_is_a_usage_error(config: Path) -> None:
    _write_config(config, "x")
    text = (config / "config.toml").read_text(encoding="utf-8")
    (config / "config.toml").write_text(text.replace('agent_command = "x"\n', ""), encoding="utf-8")
    _queue(config)
    result = _run()
    assert result.exit_code == EXIT_USAGE, result.output
    assert "agent_command" in json.loads(result.output)["message"]


def test_kill_switch_stops_the_session(config: Path) -> None:
    _write_config(config, "x", extra="kill_switch = true")
    _queue(config)
    result = _run()
    assert result.exit_code == EXIT_USAGE, result.output
    assert "kill switch" in json.loads(result.output)["message"].lower()


def test_session_sends_the_prompt_closes_the_item_and_logs_both_sides(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "handback", item))
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["handled"] == [item]
    assert payload["remaining"] == []
    log = _log_text(config)
    assert ">> run the blumkin loop" in log
    assert "<< " in log and "got: run the blumkin loop" in log
    assert f"handed back {item}" in log
    assert ">> /exit" in log
    assert "bye: /exit" in log
    assert "\x1b" not in log


def test_log_is_private(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "handback", item))
    _run()
    logs = config / "loop" / "agent-logs"
    (log,) = sorted(logs.glob("*.log"))
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert json.loads(_run().output)["log"] is None


def test_custom_prompt_and_exit_input_are_used(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(
        config,
        _stand_in(config, "handback", item),
        extra='agent_prompt = "please run it"\nagent_exit_input = "/quit"',
    )
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    log = _log_text(config)
    assert "got: please run it" in log
    assert "bye: /quit" in log


def test_agent_that_leaves_the_item_waiting_is_ended_and_reported(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(
        config,
        _stand_in(config, "idle", item),
        timeout=4,
    )
    result = _run()
    assert result.exit_code == EXIT_OTHER, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["remaining"] == [item]
    assert "still waiting" in payload["message"] or "timed out" in payload["message"]
    assert [i.id for i in pending_items(config)] == [item]
    assert "got: run the blumkin loop" in _log_text(config)


def test_agent_that_never_answers_times_out_and_is_terminated(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(
        config,
        _stand_in(config, "hang", item),
        timeout=4,
    )
    result = _run()
    assert result.exit_code == EXIT_OTHER, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "timed out" in payload["message"]
    assert payload["remaining"] == [item]


def test_agent_that_exits_before_the_prompt_is_a_failure(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "exit-early", item))
    result = _run()
    assert result.exit_code == EXIT_OTHER, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "exited" in payload["message"]
    assert payload["remaining"] == [item]
    assert "ready>" in _log_text(config)


def test_agent_command_that_does_not_exist_is_a_clear_failure(config: Path) -> None:
    _write_config(config, "x")
    _queue(config)
    _write_config(config, "/nonexistent/agent-binary")
    result = _run()
    assert result.exit_code in (EXIT_OTHER, EXIT_USAGE), result.output
    assert "agent-binary" in json.loads(result.output)["message"]


def test_human_output_names_the_log(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "handback", item))
    result = CliRunner().invoke(main, ["loop", "agent"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "1 handled" in result.output
    assert "agent-logs" in result.output


def test_invalid_utf8_from_the_agent_does_not_break_the_session(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "binary", item))
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert json.loads(result.output)["handled"] == [item]
    assert "before" in _log_text(config)


def test_a_steadily_printing_agent_is_not_reported_as_timed_out(config: Path) -> None:
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "spinner", item), timeout=30)
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["handled"] == [item]


def test_a_second_session_is_refused_while_one_runs(config: Path) -> None:
    _write_config(config, "x")
    _queue(config)
    lock_dir = config / "loop"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = (lock_dir / "agent.lock").open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run()
    finally:
        fd.close()
    assert result.exit_code == EXIT_USAGE, result.output
    assert "already running" in json.loads(result.output)["message"]


def test_a_default_terminal_type_is_set_when_missing(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TERM", raising=False)
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "term", item))
    assert _run().exit_code == EXIT_SUCCESS
    assert "TERM=xterm-256color" in _log_text(config)


def test_an_existing_log_directory_is_tightened(config: Path) -> None:
    logs = config / "loop" / "agent-logs"
    logs.mkdir(parents=True, mode=0o755)
    logs.chmod(0o755)
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "handback", item))
    _run()
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


def test_a_command_that_cannot_be_executed_is_a_clear_failure(config: Path) -> None:
    _write_config(config, "x")
    _queue(config)
    plain = config / "not-executable"
    plain.write_text("x", encoding="utf-8")
    _write_config(config, str(plain))
    result = _run()
    assert result.exit_code in (EXIT_OTHER, EXIT_USAGE), result.output
    assert "not-executable" in json.loads(result.output)["message"]


def test_a_send_failure_on_the_exit_input_does_not_lose_the_result(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pexpect

    original = pexpect.spawn.send

    def send(self: pexpect.spawn, text: str) -> int:
        if text == "/exit":
            raise OSError("pty closed")
        return original(self, text)

    monkeypatch.setattr(pexpect.spawn, "send", send)
    _write_config(config, "x")
    item = _queue(config)
    _write_config(config, _stand_in(config, "handback", item))
    result = _run()
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["handled"] == [item]
