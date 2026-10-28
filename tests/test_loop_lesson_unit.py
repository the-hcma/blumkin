"""The `lesson` loop task: queue a page photo for the operator's agent, then reply (issue #506)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from blumkin.loop.journal import list_runs
from blumkin.loop.lesson import (
    LessonError,
    LessonNotFoundError,
    LessonReplyError,
    finish_lesson,
    hand_back_lesson,
    lesson,
    pending_lessons,
    pick_template,
)
from blumkin.loop.mail_intake import intake_dir
from blumkin.loop.types import LESSON_TASK, RunContext, RunOutcome, TriggerKind, TriggerSpec

_MESSAGE = "AAMkAGI2-message-one"
_TEMPLATES = (
    ("ana", "aj-lesson"),
    ("bia", "eb-lesson"),
    ("elena", "eb-lesson"),
)
_DOC_ONE = "https://docs.google.com/document/d/1AbC_def-123/edit"
_DOC_TWO = "https://drive.google.com/file/d/1XyZ_987/view?usp=sharing"


def _trigger() -> TriggerSpec:
    return TriggerSpec(
        allowlist=("me@example.com",),
        attachment_types=(".jpg", ".png"),
        instance="lessons",
        kind=TriggerKind.LOOP,
        name="lessons",
        task=LESSON_TASK,
        templates=_TEMPLATES,
    )


def _context(config: Path, *, subject: str | None = "Bia's lesson", intake_ok: bool = True):
    return RunContext(
        config_dir=config,
        instance="lessons",
        intake_ok=intake_ok,
        message_id=_MESSAGE,
        profile="personal",
        subject=subject,
    )


def _save_page(config: Path, name: str = "page.jpg") -> Path:
    directory = intake_dir(config, trigger="lessons", message_id=_MESSAGE)
    path = directory / name
    path.write_bytes(b"not really a jpeg")
    return path


class _Replies:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, str, str]] = []
        self.fail = False

    def __call__(self, profile: str | None, message_id: str, body: str) -> None:
        if self.fail:
            raise OSError("graph down")
        self.sent.append((profile, message_id, body))


def _queue(config: Path) -> str:
    _save_page(config)
    result = lesson(_trigger(), context=_context(config))
    assert result.outcome is RunOutcome.NEEDS_USER
    (item,) = pending_lessons(config)
    return item.id


@pytest.mark.parametrize(
    ("subject", "template"),
    [
        ("Bia's lesson", "eb-lesson"),
        ("ELENA lesson page 26", "eb-lesson"),
        ("Ana Júlia's lesson", "aj-lesson"),
        ("lesson for ana", "aj-lesson"),
        ("Bia and Elena", "eb-lesson"),
    ],
)
def test_the_subject_picks_the_template(subject: str, template: str) -> None:
    pick = pick_template(_trigger(), subject)
    assert pick.template == template


@pytest.mark.parametrize(
    ("subject", "fragment"),
    [
        ("Bia and Ana lesson", "more than one"),
        ("Weekend plans", "no lesson keyword"),
        ("", "no lesson keyword"),
        (None, "no lesson keyword"),
    ],
)
def test_a_subject_that_does_not_name_exactly_one_lesson_picks_nothing(
    subject: str | None, fragment: str
) -> None:
    pick = pick_template(_trigger(), subject)
    assert pick.template is None
    assert fragment in pick.reason


def test_a_lesson_mail_is_queued_for_the_agent(tmp_path: Path) -> None:
    page = _save_page(tmp_path)
    result = lesson(_trigger(), context=_context(tmp_path))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert result.reason.startswith("lesson waiting for an agent")
    assert "eb-lesson" in result.reason
    assert _MESSAGE not in result.reason
    assert "Bia" not in result.reason
    (item,) = pending_lessons(tmp_path)
    assert item.template == "eb-lesson"
    assert item.files == (str(page),)
    assert item.message_id == _MESSAGE
    assert item.profile == "personal"
    assert item.trigger == "lessons"
    assert item.state == "pending"


def test_the_same_mail_is_not_queued_twice(tmp_path: Path) -> None:
    _save_page(tmp_path)
    lesson(_trigger(), context=_context(tmp_path))
    first = pending_lessons(tmp_path)
    lesson(_trigger(), context=_context(tmp_path))
    assert pending_lessons(tmp_path) == first


def test_queued_items_are_private(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    store = tmp_path / "loop" / "lessons"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / f"{item_id}.json").stat().st_mode) == 0o600


def test_a_mail_without_a_page_image_is_not_queued(tmp_path: Path) -> None:
    _save_page(tmp_path, "notes.txt")
    result = lesson(_trigger(), context=_context(tmp_path))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "page image" in result.reason
    assert pending_lessons(tmp_path) == []


def test_a_symlink_in_the_intake_folder_is_not_a_page(tmp_path: Path) -> None:
    directory = intake_dir(tmp_path, trigger="lessons", message_id=_MESSAGE)
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(b"x")
    (directory / "page.jpg").symlink_to(target)
    result = lesson(_trigger(), context=_context(tmp_path))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert pending_lessons(tmp_path) == []


def test_a_failed_intake_is_not_queued(tmp_path: Path) -> None:
    _save_page(tmp_path)
    result = lesson(_trigger(), context=_context(tmp_path, intake_ok=False))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "not saved" in result.reason
    assert pending_lessons(tmp_path) == []


def test_a_subject_with_no_lesson_keyword_is_not_queued(tmp_path: Path) -> None:
    _save_page(tmp_path)
    result = lesson(_trigger(), context=_context(tmp_path, subject="Weekend plans"))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "no lesson keyword" in result.reason
    assert "Weekend" not in result.reason
    assert pending_lessons(tmp_path) == []


def test_finishing_replies_with_the_links_and_closes_the_item(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    done = finish_lesson(tmp_path, item_id, [_DOC_ONE, _DOC_TWO], reply=replies)
    assert done.state == "done"
    assert done.docs == (_DOC_ONE, _DOC_TWO)
    ((profile, message_id, body),) = replies.sent
    assert (profile, message_id) == ("personal", _MESSAGE)
    assert _DOC_ONE in body
    assert _DOC_TWO in body
    assert "Sent by: blumkin loop" in body
    assert "—" not in body
    assert "–" not in body
    assert pending_lessons(tmp_path) == []
    last = list_runs(tmp_path)[-1]
    assert last.outcome is RunOutcome.DONE
    assert (last.task, last.trigger, last.instance) == (LESSON_TASK, "lessons", "lessons")


def test_finishing_twice_replies_once(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    finish_lesson(tmp_path, item_id, [_DOC_ONE], reply=replies)
    again = finish_lesson(tmp_path, item_id, [_DOC_ONE], reply=replies)
    assert again.state == "done"
    assert len(replies.sent) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "http://docs.google.com/document/d/1/edit",
        "https://docs.google.com.evil.example/document/d/1/edit",
        "https://docs.google.com@evil.example/document/d/1/edit",
        "https://evil.example/https://docs.google.com/document/d/1",
        "https://user:pw@docs.google.com/document/d/1",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://docs.google.com/document/d/1\nhttps://evil.example",
        "https://docs.google.com/document/d/1 extra",
        "https://docs.google.com",
        "",
    ],
)
def test_only_google_document_links_are_accepted(tmp_path: Path, bad: str) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    with pytest.raises(LessonError, match="Expected"):
        finish_lesson(tmp_path, item_id, [bad], reply=replies)
    assert replies.sent == []
    assert [i.id for i in pending_lessons(tmp_path)] == [item_id]


def test_finishing_needs_at_least_one_and_at_most_ten_links(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    with pytest.raises(LessonError, match="Expected"):
        finish_lesson(tmp_path, item_id, [], reply=replies)
    many = [f"https://docs.google.com/document/d/{n}/edit" for n in range(11)]
    with pytest.raises(LessonError, match="Expected"):
        finish_lesson(tmp_path, item_id, many, reply=replies)
    assert replies.sent == []


def test_a_reply_that_fails_keeps_the_item_so_finish_can_be_repeated(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    replies.fail = True
    with pytest.raises(LessonReplyError, match="finish"):
        finish_lesson(tmp_path, item_id, [_DOC_ONE], reply=replies)
    (item,) = pending_lessons(tmp_path)
    assert item.docs == (_DOC_ONE,)
    assert list_runs(tmp_path) == []
    replies.fail = False
    done = finish_lesson(tmp_path, item_id, [_DOC_ONE], reply=replies)
    assert done.state == "done"
    assert len(replies.sent) == 1


def test_an_unknown_or_malformed_item_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LessonNotFoundError):
        finish_lesson(tmp_path, "0" * 16, [_DOC_ONE], reply=_Replies())
    with pytest.raises(LessonError, match="Expected"):
        finish_lesson(tmp_path, "../x", [_DOC_ONE], reply=_Replies())


def test_handing_an_item_back_records_why_and_sends_nothing(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    replies = _Replies()
    handed = hand_back_lesson(tmp_path, item_id, "the photo is too blurry to read")
    assert handed.state == "needs_user"
    assert handed.note == "the photo is too blurry to read"
    assert pending_lessons(tmp_path) == []
    assert replies.sent == []
    last = list_runs(tmp_path)[-1]
    assert last.outcome is RunOutcome.NEEDS_USER


def test_a_handed_back_item_can_still_be_finished_later(tmp_path: Path) -> None:
    item_id = _queue(tmp_path)
    hand_back_lesson(tmp_path, item_id, "blurry")
    replies = _Replies()
    done = finish_lesson(tmp_path, item_id, [_DOC_ONE], reply=replies)
    assert done.state == "done"
    assert len(replies.sent) == 1
