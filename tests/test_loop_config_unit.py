"""`[loop]` / `[cron.*]` config parsing (issue #501)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.loop.config import LoopConfigError, parse_loop_setup
from blumkin.loop.types import TriggerKind


def test_parse_loop_and_cron_triggers(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "kill_switch": False,
                "max_retries": 1,
                "run_budget_seconds": 120,
                "triggers": {
                    "probe-trigger": {
                        "task": "probe",
                        "instance": "default",
                        "kind": "manual",
                    }
                },
            },
            "cron": {
                "eod": {
                    "task": "time-sheet",
                    "instance": "current",
                    "schedule": "0 18 * * 1-5",
                    "timezone": "America/Toronto",
                    "working_days_only": True,
                }
            },
        },
    )
    assert setup.loop.max_retries == 1
    assert setup.loop.run_budget_seconds == 120.0
    assert set(setup.triggers) == {"probe-trigger", "eod"}
    assert setup.triggers["probe-trigger"].kind is TriggerKind.MANUAL
    assert setup.triggers["eod"].kind is TriggerKind.CRON
    assert setup.triggers["eod"].timezone == "America/Toronto"
    assert setup.triggers["eod"].working_days_only is True


def test_duplicate_trigger_names_across_loop_and_cron_fail(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="unique trigger"):
        parse_loop_setup(
            tmp_path,
            {
                "loop": {"triggers": {"same": {"task": "probe", "instance": "a"}}},
                "cron": {
                    "same": {
                        "task": "probe",
                        "instance": "b",
                        "schedule": "0 1 * * *",
                        "timezone": "UTC",
                    }
                },
            },
        )


def test_unknown_loop_key_fails(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="unknown key"):
        parse_loop_setup(tmp_path, {"loop": {"nope": True}})


def test_invalid_task_name_fails(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="task"):
        parse_loop_setup(
            tmp_path,
            {"loop": {"triggers": {"t": {"task": "../x", "instance": "default"}}}},
        )
