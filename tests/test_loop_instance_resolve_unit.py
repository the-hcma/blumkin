"""Deterministic instance resolution for loop triggers (issue #504)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.browser.ledger import begin_instance, discard_instance, mark_submitted
from blumkin.loop.config import LoopSetup, parse_loop_setup
from blumkin.loop.instance_resolve import resolve_instance
from blumkin.loop.types import TriggerSpec

_TASK = "expense-report"


def _setup(tmp_path: Path, **trigger: object) -> tuple[LoopSetup, TriggerSpec]:
    table: dict = {"task": _TASK, "kind": "loop", "allowlist": ["me@example.com"]}
    table.update(trigger)
    setup = parse_loop_setup(tmp_path, {"loop": {"triggers": {"receipts": table}}})
    return setup, setup.triggers["receipts"]


def _begin(tmp_path: Path, *names: str) -> None:
    for name in names:
        begin_instance(tmp_path, _TASK, name)


def test_static_returns_the_configured_instance(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance="trip-1")
    result = resolve_instance(setup, trigger)
    assert result.resolved
    assert result.instance == "trip-1"


def test_static_without_an_instance_needs_the_user(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance="trip-1")
    bare = TriggerSpec(instance="", kind=trigger.kind, name="x", task=_TASK)
    result = resolve_instance(setup, bare)
    assert not result.resolved
    assert "no configured instance" in result.reason


def test_newest_open_resolves_the_only_open_instance(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="newest_open")
    _begin(tmp_path, "trip-old", "trip-new")
    mark_submitted(tmp_path, _TASK, "trip-old")
    result = resolve_instance(setup, trigger)
    assert result.instance == "trip-new"


def test_newest_open_ignores_submitted_and_discarded(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="newest_open")
    _begin(tmp_path, "trip-a", "trip-b")
    mark_submitted(tmp_path, _TASK, "trip-a")
    discard_instance(tmp_path, _TASK, "trip-b")
    result = resolve_instance(setup, trigger)
    assert result.instance is None
    assert "no open" in result.reason


def test_newest_open_with_no_ledger_at_all_needs_the_user(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="newest_open")
    assert resolve_instance(setup, trigger).instance is None


def test_newest_open_with_several_open_is_ambiguous(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="newest_open")
    _begin(tmp_path, "trip-a", "trip-b")
    result = resolve_instance(setup, trigger)
    assert result.instance is None
    assert "2 open" in result.reason
    # Counts only: instance names are not echoed.
    assert "trip-a" not in result.reason


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[expense] trip-lisbon taxi", "trip-lisbon"),
        ("[EXPENSE] Trip-Lisbon: taxi receipt", "trip-lisbon"),
        ("Fwd: trip-lisbon, dinner [expense]", "trip-lisbon"),
        ("[expense] trip-lisbon trip-lisbon again", "trip-lisbon"),
        ("[expense] trip-paris", "trip-paris"),
    ],
)
def test_subject_trip_names_exactly_one_open_instance(
    tmp_path: Path, subject: str, expected: str
) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="subject_trip", subject_tag="[expense]")
    _begin(tmp_path, "trip-lisbon", "trip-paris")
    assert resolve_instance(setup, trigger, subject=subject).instance == expected


@pytest.mark.parametrize(
    "subject",
    [
        None,
        "   ",
        "[expense] receipt for lunch",
        "[expense] trip-lisbon and trip-paris",
        "[expense] trip-lisbonx",
        "[expense] trip-berlin",
    ],
)
def test_subject_trip_without_exactly_one_match_needs_the_user(
    tmp_path: Path, subject: str | None
) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="subject_trip", subject_tag="[expense]")
    _begin(tmp_path, "trip-lisbon", "trip-paris")
    result = resolve_instance(setup, trigger, subject=subject)
    assert result.instance is None
    assert result.reason
    # The subject is untrusted data and never lands in a reason.
    assert "berlin" not in result.reason


def test_subject_trip_does_not_match_a_closed_trip(tmp_path: Path) -> None:
    setup, trigger = _setup(tmp_path, instance_resolver="subject_trip")
    _begin(tmp_path, "trip-lisbon", "trip-paris")
    mark_submitted(tmp_path, _TASK, "trip-lisbon")
    assert resolve_instance(setup, trigger, subject="trip-lisbon taxi").instance is None
    assert resolve_instance(setup, trigger, subject="trip-paris taxi").instance == "trip-paris"


def test_subject_trip_ignores_the_tag_text(tmp_path: Path) -> None:
    # An instance named like the tag word must not match just because the tag is present.
    setup, trigger = _setup(tmp_path, instance_resolver="subject_trip", subject_tag="expense")
    _begin(tmp_path, "expense")
    assert resolve_instance(setup, trigger, subject="expense taxi").instance is None
    assert resolve_instance(setup, trigger, subject="expense expense taxi").instance is None
