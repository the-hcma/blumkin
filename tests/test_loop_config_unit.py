"""`[loop]` / `[cron.*]` config parsing (issue #501)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.loop.config import LoopConfigError, parse_loop_setup
from blumkin.loop.types import InstanceResolver, TriggerKind


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


def _mail_trigger(**extra: object) -> dict:
    table: dict = {
        "task": "expense-report",
        "kind": "loop",
        "allowlist": ["me@example.com"],
    }
    table.update(extra)
    return {"loop": {"triggers": {"receipts": table}}}


def test_instance_resolver_defaults_to_static_and_needs_instance(tmp_path: Path) -> None:
    setup = parse_loop_setup(tmp_path, _mail_trigger(instance="trip-1"))
    spec = setup.triggers["receipts"]
    assert spec.instance_resolver is InstanceResolver.STATIC
    assert spec.as_dict()["instance_resolver"] == "static"
    with pytest.raises(LoopConfigError, match="instance"):
        parse_loop_setup(tmp_path, _mail_trigger())


@pytest.mark.parametrize("resolver", ["newest_open", "subject_trip"])
def test_dynamic_resolvers_omit_instance(tmp_path: Path, resolver: str) -> None:
    setup = parse_loop_setup(tmp_path, _mail_trigger(instance_resolver=resolver))
    spec = setup.triggers["receipts"]
    assert spec.instance_resolver.value == resolver
    assert spec.instance == ""


def test_dynamic_resolver_rejects_a_configured_instance(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="only used with"):
        parse_loop_setup(
            tmp_path, _mail_trigger(instance="trip-1", instance_resolver="newest_open")
        )


def test_unknown_resolver_fails(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="instance_resolver must be one of"):
        parse_loop_setup(tmp_path, _mail_trigger(instance_resolver="latest"))


def test_subject_trip_needs_a_mail_trigger(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="requires kind"):
        parse_loop_setup(
            tmp_path,
            {
                "loop": {
                    "triggers": {
                        "t": {
                            "task": "expense-report",
                            "kind": "manual",
                            "instance_resolver": "subject_trip",
                        }
                    }
                }
            },
        )


def test_cron_triggers_stay_static(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="unknown key"):
        parse_loop_setup(
            tmp_path,
            {
                "cron": {
                    "eod": {
                        "task": "time-sheet",
                        "instance": "current",
                        "instance_resolver": "newest_open",
                        "schedule": "0 18 * * 1-5",
                        "timezone": "UTC",
                    }
                }
            },
        )


def _agent_trigger(**extra: object) -> dict:
    table: dict = {
        "task": "agent",
        "kind": "loop",
        "instance": "requests",
        "allowlist": ["me@example.com"],
        "attachment_types": [".jpg", ".pdf"],
        "templates": {
            "Report": "weekly-report",
            "summary": "weekly-report",
            "Digest": "daily-digest",
        },
    }
    table.update(extra)
    return {"loop": {"triggers": {"requests": table}}}


def test_agent_templates_are_parsed_with_folded_keywords(tmp_path: Path) -> None:
    spec = parse_loop_setup(tmp_path, _agent_trigger()).triggers["requests"]
    assert spec.templates == (
        ("digest", "daily-digest"),
        ("report", "weekly-report"),
        ("summary", "weekly-report"),
    )
    assert spec.as_dict()["templates"] == {
        "digest": "daily-digest",
        "report": "weekly-report",
        "summary": "weekly-report",
    }


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"templates": {}}, "templates"),
        ({"templates": "weekly-report"}, "templates"),
        ({"templates": {"two words": "weekly-report"}}, "keyword"),
        ({"templates": {"": "weekly-report"}}, "keyword"),
        ({"templates": {"report": "../x"}}, "template"),
        ({"templates": {"report": 3}}, "template"),
        ({"templates": {"Report": "a", "report": "b"}}, "twice"),
        ({"kind": "manual", "allowlist": None}, "kind"),
        ({"attachment_types": []}, "attachment_types"),
    ],
)
def test_a_bad_agent_trigger_is_refused(tmp_path: Path, change: dict, fragment: str) -> None:
    table = _agent_trigger(**change)
    if change.get("allowlist", 1) is None:
        del table["loop"]["triggers"]["requests"]["allowlist"]
    with pytest.raises(LoopConfigError, match=fragment):
        parse_loop_setup(tmp_path, table)


def test_templates_belong_to_the_agent_task_only(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="agent"):
        parse_loop_setup(
            tmp_path,
            _mail_trigger(instance="trip-1", templates={"report": "weekly-report"}),
        )
