"""Work queue entries carry mail context (issue #504)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.loop.queue import (
    MAX_SUBJECT_CHARS,
    LoopQueueError,
    QueueEntry,
    drain_queue,
    enqueue,
    peek_queue,
    requeue_front,
)


def test_entries_round_trip_in_fifo_order(tmp_path: Path) -> None:
    first = QueueEntry(trigger="receipts", message_id="m1", profile="work", subject="[trip] a")
    second = QueueEntry(trigger="eod")
    enqueue(tmp_path, first)
    enqueue(tmp_path, second)
    assert peek_queue(tmp_path) == [first, second]
    assert drain_queue(tmp_path) == [first, second]
    assert drain_queue(tmp_path) == []


def test_requeue_front_keeps_context_and_order(tmp_path: Path) -> None:
    kept = QueueEntry(trigger="b", message_id="m2")
    enqueue(tmp_path, QueueEntry(trigger="c"))
    requeue_front(tmp_path, [QueueEntry(trigger="a", message_id="m1"), kept])
    assert [e.trigger for e in peek_queue(tmp_path)] == ["a", "b", "c"]
    assert peek_queue(tmp_path)[1] == kept


def test_optional_fields_are_left_out_of_the_file(tmp_path: Path) -> None:
    enqueue(tmp_path, QueueEntry(trigger="eod"))
    text = (tmp_path / "loop" / "queue.jsonl").read_text(encoding="utf-8")
    assert text.strip() == '{"trigger": "eod"}'


def test_a_name_only_line_from_an_older_queue_still_reads(tmp_path: Path) -> None:
    directory = tmp_path / "loop"
    directory.mkdir(mode=0o700)
    (directory / "queue.jsonl").write_text('{"trigger": "old"}\n', encoding="utf-8")
    assert drain_queue(tmp_path) == [QueueEntry(trigger="old")]


def test_a_long_subject_is_cut(tmp_path: Path) -> None:
    enqueue(tmp_path, QueueEntry(trigger="t", subject="x" * (MAX_SUBJECT_CHARS + 50)))
    (entry,) = peek_queue(tmp_path)
    assert entry.subject == "x" * MAX_SUBJECT_CHARS


def test_context_is_handed_to_the_runner(tmp_path: Path) -> None:
    context = QueueEntry(trigger="t", message_id="m1", profile="work", subject="s").as_context()
    assert (context.message_id, context.profile, context.subject) == ("m1", "work", "s")
    assert context.config_dir is None


@pytest.mark.parametrize(
    "line",
    [
        '{"trigger": 1}',
        '{"trigger": "t", "message_id": 5}',
        '{"trigger": "t", "subject": ["x"]}',
        "[]",
        "not json",
    ],
)
def test_a_bad_line_is_a_queue_error(tmp_path: Path, line: str) -> None:
    directory = tmp_path / "loop"
    directory.mkdir(mode=0o700)
    (directory / "queue.jsonl").write_text(line + "\n", encoding="utf-8")
    with pytest.raises(LoopQueueError):
        peek_queue(tmp_path)
