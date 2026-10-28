"""`blumkin loop pending` and `blumkin loop finish` for the lesson task (issue #506)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE
from blumkin.loop.lesson import lesson
from blumkin.loop.mail_intake import intake_dir
from blumkin.loop.types import RunContext, TriggerKind, TriggerSpec

_MESSAGE = "AAMkAGI2-message-one"
_DOC = "https://docs.google.com/document/d/1AbC_def-123/edit"


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "abc-123"',
                "",
                "[loop.triggers.lessons]",
                'kind = "loop"',
                'task = "lesson"',
                'instance = "lessons"',
                'allowlist = ["me@example.com"]',
                "",
                "[loop.triggers.lessons.templates]",
                'bia = "eb-lesson"',
                'ana = "aj-lesson"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str, str, tuple[str, ...]]]:
    """Replies prepared; each one is also "sent" (the draft id is `draft-N`)."""
    calls: list[tuple[str | None, str, str, tuple[str, ...]]] = []

    def prepare(profile: str | None, message_id: str, body: str, allowed: Any) -> str:
        calls.append((profile, message_id, body, tuple(allowed)))
        return f"draft-{len(calls)}"

    monkeypatch.setattr("blumkin.loop.lesson.prepare_reply", prepare)
    monkeypatch.setattr("blumkin.loop.lesson.send_draft", lambda profile, draft_id: None)
    return calls


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _queue(config: Path) -> str:
    trigger = TriggerSpec(
        allowlist=("me@example.com",),
        instance="lessons",
        kind=TriggerKind.LOOP,
        name="lessons",
        task="lesson",
        templates=(("bia", "eb-lesson"),),
    )
    directory = intake_dir(config, trigger="lessons", message_id=_MESSAGE)
    (directory / "page.jpg").write_bytes(b"x")
    lesson(
        trigger,
        context=RunContext(
            config_dir=config,
            instance="lessons",
            message_id=_MESSAGE,
            profile="personal",
            subject="Bia's lesson",
        ),
    )
    payload = _json(_run("loop", "pending"))
    return payload["items"][0]["id"]


def test_pending_is_empty_before_any_lesson_mail() -> None:
    result = _run("loop", "pending")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["items"] == []
    text = CliRunner().invoke(main, ["loop", "pending"])
    assert "no lessons waiting" in text.output


def test_pending_lists_the_template_and_the_saved_page(_config_dir: Path) -> None:
    item_id = _queue(_config_dir)
    payload = _json(_run("loop", "pending"))
    (item,) = payload["items"]
    assert item["id"] == item_id
    assert item["template"] == "eb-lesson"
    assert item["files"][0].endswith("page-1.jpg")
    assert "message_id" not in item
    assert "blumkin loop finish" in payload["instructions"]
    assert "data" in payload["instructions"]


def test_finishing_with_a_link_replies_and_closes_the_item(
    _config_dir: Path, sent: list[tuple[str | None, str, str, tuple[str, ...]]]
) -> None:
    item_id = _queue(_config_dir)
    result = _run("loop", "finish", item_id, "--doc", _DOC)
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["item"]["state"] == "done"
    ((profile, message_id, body, allowed),) = sent
    assert (profile, message_id) == ("personal", _MESSAGE)
    assert allowed == ("me@example.com",)
    assert _DOC in body
    assert _json(_run("loop", "pending"))["items"] == []
    text = CliRunner().invoke(main, ["loop", "finish", item_id, "--doc", _DOC])
    assert text.exit_code == EXIT_SUCCESS
    assert len(sent) == 1


def test_handing_an_item_back_sends_nothing(
    _config_dir: Path, sent: list[tuple[str | None, str, str, tuple[str, ...]]]
) -> None:
    item_id = _queue(_config_dir)
    result = _run("loop", "finish", item_id, "--needs-user", "the photo is blurry")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["item"]["state"] == "needs_user"
    assert sent == []
    assert _json(_run("loop", "pending"))["items"] == []


@pytest.mark.parametrize(
    "extra",
    [[], ["--doc", _DOC, "--needs-user", "why"], ["--doc", "http://docs.google.com/x"]],
)
def test_finish_needs_exactly_one_valid_outcome(
    _config_dir: Path, sent: list[tuple[str | None, str, str, tuple[str, ...]]], extra: list[str]
) -> None:
    item_id = _queue(_config_dir)
    result = _run("loop", "finish", item_id, *extra)
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"
    assert sent == []


def test_finishing_an_unknown_item_is_not_found() -> None:
    result = _run("loop", "finish", "0" * 16, "--doc", _DOC)
    assert result.exit_code == EXIT_NOT_FOUND
    assert _json(result)["error"] == "not_found"


def test_a_reply_that_fails_is_reported_and_can_be_repeated(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = _queue(_config_dir)

    def boom(profile: str | None, draft_id: str) -> None:
        raise OSError("graph down")

    monkeypatch.setattr("blumkin.loop.lesson.prepare_reply", lambda *a, **k: "draft-1")
    monkeypatch.setattr("blumkin.loop.lesson.send_draft", boom)
    result = _run("loop", "finish", item_id, "--doc", _DOC)
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "reply_failed"
    assert len(_json(_run("loop", "pending"))["items"]) == 1


def test_handed_back_items_are_listed_only_when_asked(
    _config_dir: Path, sent: list[tuple[str | None, str, str, tuple[str, ...]]]
) -> None:
    item_id = _queue(_config_dir)
    _run("loop", "finish", item_id, "--needs-user", "the photo is blurry")
    assert _json(_run("loop", "pending"))["items"] == []
    listed = _json(_run("loop", "pending", "--handed-back"))["items"]
    assert [(i["id"], i["state"], i["note"]) for i in listed] == [
        (item_id, "needs_user", "the photo is blurry")
    ]
    text = CliRunner().invoke(main, ["loop", "pending", "--handed-back"])
    assert "handed back: the photo is blurry" in text.output
