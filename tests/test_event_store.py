"""Behaviour of :mod:`ws_collab.jsonl_store`.

Tests assert the store's *contract* -- ordering, idempotency, bounded cursor
reads, tolerance of a crashed writer, rotation/retention, restart recovery --
rather than how bytes are laid out on disk. When a test must simulate real file
corruption it resolves paths through ``describe_files()`` so renaming or
restructuring the storage layout does not break the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ws_collab.errors import ConflictError, CursorError
from ws_collab.events import STREAM_CONVERSATION, Event, streams_for_role
from ws_collab.ids import encode_cursor

from conftest import make_event_store

STREAM = STREAM_CONVERSATION
OTHER_STREAM = streams_for_role("alerts")[0]


def _event(index: int, key: str | None = None) -> Event:
    return Event(
        stream=STREAM,
        type="CONVERSATION_MESSAGE",
        data={"i": index},
        source_id="op",
        source_kind="operator",
        idempotency_key=key,
    )


def _read_all(store, page_size: int = 50) -> list[int]:
    """Drain a stream through the public cursor API and return payload order."""

    collected: list[int] = []
    cursor = None
    for _ in range(200):
        page = store.read(STREAM, cursor, page_size)
        collected.extend(event.data["i"] for event in page.events)
        cursor = page.next_cursor
        if not page.has_more:
            break
    return collected


def _active_path(store) -> Path:
    return Path(store.stream(STREAM).describe_files()["active_path"])


# --------------------------------------------------------------------- writes
def test_appends_are_ordered_and_uniquely_identified(store) -> None:
    results = [store.append(_event(i)) for i in range(5)]
    sequences = [r.event.seq for r in results]
    assert sequences == sorted(sequences), "positions must increase monotonically"
    assert len(set(sequences)) == len(sequences)
    assert len({r.event.id for r in results}) == len(results)
    assert all(r.event.ts.endswith("Z") for r in results), "timestamps must be UTC"


def test_append_returns_a_cursor_positioned_after_the_new_event(store) -> None:
    store.append(_event(0))
    result = store.append(_event(1))
    assert store.read(STREAM, result.cursor, 10).events == []


def test_idempotency_key_suppresses_a_duplicate_write(store) -> None:
    first = store.append(_event(1, key="k1"))
    second = store.append(_event(1, key="k1"))
    assert first.duplicate is False and second.duplicate is True
    assert (second.event.id, second.event.seq) == (first.event.id, first.event.seq)
    assert _read_all(store) == [1], "the duplicate must not be stored twice"


# ---------------------------------------------------------------- cursor reads
def test_cursor_pagination_is_bounded_and_resumable(store) -> None:
    for i in range(10):
        store.append(_event(i))
    first = store.read(STREAM, None, 4)
    assert len(first.events) == 4 and first.has_more is True
    second = store.read(STREAM, first.next_cursor, 4)
    assert [e.data["i"] for e in second.events] == [4, 5, 6, 7]
    third = store.read(STREAM, second.next_cursor, 100)
    assert [e.data["i"] for e in third.events] == [8, 9] and third.has_more is False


def test_reading_from_the_same_cursor_twice_is_stable(store) -> None:
    for i in range(4):
        store.append(_event(i))
    cursor = store.read(STREAM, None, 2).next_cursor
    assert [e.data["i"] for e in store.read(STREAM, cursor, 10).events] == [2, 3]
    assert [e.data["i"] for e in store.read(STREAM, cursor, 10).events] == [2, 3]


def test_tail_returns_the_most_recent_events(store) -> None:
    for i in range(10):
        store.append(_event(i))
    assert [e.data["i"] for e in store.tail(STREAM, 3)] == [7, 8, 9]


def test_filters_apply_to_reads(store) -> None:
    store.append(_event(0))
    store.append(Event(stream=STREAM, type="OTHER", data={"i": 1}))
    page = store.read(STREAM, None, 10, lambda e: e.type == "OTHER")
    assert [e.data["i"] for e in page.events] == [1]


# ------------------------------------------------------------ cursor recovery
def test_cursor_beyond_end_of_stream_offers_a_usable_recovery_position(store) -> None:
    store.append(_event(0))
    beyond = encode_cursor({"s": STREAM, "seq": 999, "off": 0, "gen": 0})
    with pytest.raises(CursorError) as excinfo:
        store.read(STREAM, beyond, 10)
    recovery = excinfo.value.details.get("recovery")
    assert recovery, "an unusable cursor must come with a recovery position"
    store.read(STREAM, recovery, 10)


def test_malformed_cursor_is_rejected_cleanly(store) -> None:
    with pytest.raises(CursorError):
        store.read(STREAM, "not-a-real-cursor", 10)


def test_cursor_from_another_stream_is_rejected(store) -> None:
    store.append(_event(0))
    cursor = store.read(STREAM, None, 1).next_cursor
    with pytest.raises(CursorError):
        store.read(OTHER_STREAM, cursor, 10)


# ------------------------------------------------------- crashed-writer safety
def test_unterminated_final_record_is_skipped_by_readers(store) -> None:
    for i in range(3):
        store.append(_event(i))
    with open(_active_path(store), "a", encoding="utf-8") as handle:
        handle.write('{"id": "torn", "seq": 99, "incomplete": tru')  # crashed mid-write
    assert _read_all(store) == [0, 1, 2], "a partial trailing record must not surface"


def test_append_after_a_crashed_write_keeps_the_new_record_readable(store) -> None:
    for i in range(3):
        store.append(_event(i))
    with open(_active_path(store), "a", encoding="utf-8") as handle:
        handle.write('{"id": "torn", "seq": 99, "incomplete": tru')
    store.append(_event(3))
    assert _read_all(store) == [0, 1, 2, 3], "the new event must not be swallowed"
    assert store.read(STREAM, None, 100).malformed >= 1, "the torn record must be reported"


def test_corrupt_record_is_reported_but_does_not_stop_the_stream(store) -> None:
    store.append(_event(0))
    with open(_active_path(store), "a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    store.append(_event(1))
    page = store.read(STREAM, None, 100)
    assert [e.data["i"] for e in page.events] == [0, 1]
    assert page.malformed == 1


def test_unknown_fields_on_older_records_are_preserved(config) -> None:
    store = make_event_store(config)
    try:
        active = Path(store.stream(STREAM).describe_files()["active_path"])
        record = {
            "id": "01AAA", "stream": STREAM, "seq": 1, "type": "LEGACY",
            "ts": "2024-01-01T00:00:00.000Z", "schema_version": 0,
            "source_id": "old", "source_kind": "system", "data": {"x": 1},
            "future_field": {"kept": True},
        }
        active.write_text(json.dumps(record) + "\n", encoding="utf-8")
        event = store.read(STREAM, None, 10).events[0]
        assert event.to_dict()["future_field"] == {"kept": True}
    finally:
        store.close()


# --------------------------------------------------------- rotation/retention
def test_reads_span_rotation_without_losing_events(config) -> None:
    store = make_event_store(config, rotate_max_bytes=400, retention_max_files=50)
    try:
        for i in range(30):
            store.append(_event(i))
        assert _read_all(store, page_size=5) == list(range(30))
    finally:
        store.close()


def test_retention_bounds_history_while_keeping_recent_events(config) -> None:
    store = make_event_store(config, rotate_max_bytes=300, retention_max_files=2)
    try:
        for i in range(40):
            store.append(_event(i))
        readable = _read_all(store, page_size=10)
        assert readable, "recent history must still be readable"
        assert readable[-1] == 39, "the newest event must always survive retention"
        assert len(readable) < 40, "retention should discard the oldest history"
    finally:
        store.close()


# ------------------------------------------------------------ restart recovery
def test_restart_continues_the_stream_without_reusing_positions(config) -> None:
    store = make_event_store(config)
    for i in range(4):
        store.append(_event(i))
    last_seq = store.stream(STREAM).stats()["seq"]
    store.close()

    reopened = make_event_store(config)
    try:
        assert reopened.append(_event(4)).event.seq > last_seq, "a restart must never reuse a position"
        assert [e.data["i"] for e in reopened.tail(STREAM, 2)] == [3, 4]
    finally:
        reopened.close()


def test_recovers_when_recovery_metadata_is_lost(config) -> None:
    store = make_event_store(config)
    for i in range(3):
        store.append(_event(i))
    recovery_path = Path(store.stream(STREAM).describe_files()["recovery_path"])
    last_seq = store.stream(STREAM).stats()["seq"]
    store.close()
    recovery_path.unlink()

    reopened = make_event_store(config)
    try:
        assert reopened.append(_event(3)).event.seq > last_seq
        assert _read_all(reopened) == [0, 1, 2, 3]
    finally:
        reopened.close()


def test_a_second_writer_cannot_take_over_the_directory(config) -> None:
    store = make_event_store(config)
    try:
        with pytest.raises(ConflictError):
            make_event_store(config)
    finally:
        store.close()
