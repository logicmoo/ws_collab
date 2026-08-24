"""Cursor semantics: advance, rewind, replay, skip authorization, and audit."""

from __future__ import annotations

import pytest

from ws_collab.cursors import CursorManager
from ws_collab.errors import AuthorizationError, NotFoundError
from ws_collab.events import Event, streams_for_role

STREAM = streams_for_role("conversation")[0]
CONSUMER = "worker-1"


def _seed(store, count: int) -> list[str]:
    """Append events and return a cursor token after each one."""

    tokens = []
    for i in range(count):
        result = store.append(Event(stream=STREAM, type="CONVERSATION_MESSAGE", data={"i": i}))
        tokens.append(result.cursor)
    return tokens


@pytest.fixture
def cursors(config):
    audited: list[dict] = []
    manager = CursorManager(config.cursors_dir, audit_sink=audited.append)
    manager.audited = audited  # type: ignore[attr-defined]
    return manager


def test_commit_advances_and_persists(cursors, store, config) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[1])
    assert cursors.get(STREAM, CONSUMER).seq == 2

    reloaded = CursorManager(config.cursors_dir)
    assert reloaded.get(STREAM, CONSUMER).seq == 2, "cursors must survive a restart"


def test_commit_refuses_to_move_backwards(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[2])
    with pytest.raises(AuthorizationError):
        cursors.commit(STREAM, CONSUMER, tokens[0])


def test_recommitting_the_same_position_is_allowed(cursors, store) -> None:
    tokens = _seed(store, 2)
    cursors.commit(STREAM, CONSUMER, tokens[1])
    cursors.commit(STREAM, CONSUMER, tokens[1])
    assert cursors.get(STREAM, CONSUMER).seq == 2


def test_rewind_requires_explicit_replay_authorization(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[2])
    with pytest.raises(AuthorizationError):
        cursors.reposition(STREAM, CONSUMER, tokens[0], reason="verify", operator="op")
    moved = cursors.reposition(STREAM, CONSUMER, tokens[0], reason="verify", operator="op", allow_replay=True)
    assert moved.seq == 1


def test_forward_skip_requires_explicit_skip_authorization(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[0])
    with pytest.raises(AuthorizationError):
        cursors.reposition(STREAM, CONSUMER, tokens[2], reason="drain", operator="op")
    moved = cursors.reposition(STREAM, CONSUMER, tokens[2], reason="drain", operator="op", allow_skip=True)
    assert moved.seq == 3


def test_history_records_who_moved_what_and_the_risk(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[2], reason="processed")
    cursors.reposition(STREAM, CONSUMER, tokens[0], reason="incident replay", operator="alice", allow_replay=True)
    history = cursors.history(STREAM, CONSUMER)
    latest = history[-1]
    assert latest["operator"] == "alice"
    assert latest["reason"] == "incident replay"
    assert latest["risk"] == "replay"
    assert latest["old_seq"] > latest["new_seq"]
    assert latest["at"], "every move must be timestamped"


def test_moves_are_audited(cursors, store) -> None:
    tokens = _seed(store, 2)
    cursors.commit(STREAM, CONSUMER, tokens[0])
    assert cursors.audited, "cursor movement must reach the audit sink"


def test_reset_is_allowed_and_flagged_as_replay(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, CONSUMER, tokens[2])
    reset = cursors.reset(STREAM, CONSUMER, store.stream(STREAM).cursor_at_start(), reason="stream repaired", operator="op")
    assert reset.seq == 0
    assert cursors.history(STREAM, CONSUMER)[-1]["risk"] == "replay"


def test_history_for_unknown_consumer_is_an_error(cursors) -> None:
    with pytest.raises(NotFoundError):
        cursors.history(STREAM, "never-seen")


def test_cursors_are_independent_per_consumer(cursors, store) -> None:
    tokens = _seed(store, 3)
    cursors.commit(STREAM, "a", tokens[2])
    cursors.commit(STREAM, "b", tokens[0])
    assert cursors.get(STREAM, "a").seq != cursors.get(STREAM, "b").seq


def test_rewound_cursor_actually_replays_events(cursors, store) -> None:
    tokens = _seed(store, 4)
    cursors.commit(STREAM, CONSUMER, tokens[3])
    assert store.read(STREAM, cursors.get(STREAM, CONSUMER).token, 10).events == []
    cursors.reposition(STREAM, CONSUMER, tokens[1], reason="replay", operator="op", allow_replay=True)
    replayed = store.read(STREAM, cursors.get(STREAM, CONSUMER).token, 10).events
    assert [e.data["i"] for e in replayed] == [2, 3]
