"""Regression tests for complete-sentence Meet caption buffering."""

from __future__ import annotations

from ws_collab.meet_bridge.tracker import CaptionTracker


def _tracker() -> CaptionTracker:
    return CaptionTracker(settle=0)


def _row(key: str, text: str, speaker: str = "Alice") -> dict[str, str]:
    return {"key": key, "speaker": speaker, "text": text}


def _recorder() -> tuple[list[tuple[str, str, str, bool, str | None]], object]:
    """`emit(key, speaker, text, final=..., replaces=...)` -- record as a
    tuple including `final` and the key this sentence continues from."""
    emits: list[tuple[str, str, str, bool, str | None]] = []

    def emit(key: str, speaker: str, text: str, final: bool = False, replaces: str | None = None) -> None:
        emits.append((key, speaker, text, final, replaces))

    return emits, emit


def test_cold_start_baselines_silently() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "already on screen when we attached")], [], emit)
    assert emits == []
    assert tracker.baselined is True


def test_empty_poll_does_not_consume_the_cold_start_baseline() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([], [], emit)
    assert tracker.baselined is False

    tracker.update([_row("row1", "old caption history")], ["row1"], emit)
    assert tracker.baselined is True
    assert emits == []


def test_cold_start_waits_for_incremental_caption_history_to_finish_loading() -> None:
    now = 0.0

    def clock() -> float:
        return now

    tracker = CaptionTracker(settle=1.2, clock=clock)
    emits, emit = _recorder()
    old = [
        _row("row1", "Old sentence one."),
        _row("row2", "Old sentence two."),
        _row("row3", "Old sentence three."),
    ]

    tracker.update(old[:1], ["row1"], emit)
    now = 0.2
    tracker.update(old[:2], ["row1", "row2"], emit)
    now = 0.4
    tracker.update(old, ["row1", "row2", "row3"], emit)
    now = 1.7
    tracker.update(old, ["row1", "row2", "row3"], emit)
    assert emits == []

    now = 1.8
    current = [*old, _row("row4", "A new task.")]
    tracker.update(current, ["row1", "row2", "row3", "row4"], emit)
    now = 3.1
    tracker.update(current, ["row1", "row2", "row3", "row4"], emit)
    assert emits == [("row4", "Alice", "A new task.", True, None)]


def test_growth_waits_for_a_complete_sentence() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], [], emit)
    tracker.update([_row("row1", "Hello")], [], emit)
    tracker.update([_row("row1", "Hello there")], [], emit)
    assert emits == []


def test_sentence_boundary_freezes_the_old_key_and_starts_a_new_one() -> None:
    """The core "break this single line after a full sentence" behavior:
    once growth crosses a `.`/`!`/`?`, the completed sentence is emitted
    (final=True) under the key that had been growing, and everything
    after it moves to a brand-new key -- the old key never receives
    another update. The new key's `replaces` points back at the old one,
    making the chain explicit for a consumer."""
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], [], emit)
    for text in ("Hello", "Hello there", "Hello there.", "Hello there. How", "Hello there. How are you?"):
        tracker.update([_row("row1", text)], ["row1"], emit)

    assert emits == []

    tracker.update(
        [_row("row1", "Hello there. How are you?"), _row("row2", "Next")],
        ["row1", "row2"],
        emit,
    )
    assert [event[0] for event in emits] == ["row1", "row1#1"]
    assert emits[0] == ("row1", "Alice", "Hello there.", True, None)
    assert emits[-1] == ("row1#1", "Alice", "How are you?", True, "row1")

    # The old key must never be touched again even as the row keeps growing.
    tracker.update(
        [_row("row1", "Hello there. How are you? I am fine."), _row("row2", "Next")],
        ["row1", "row2"],
        emit,
    )
    assert emits[-1] == ("row1#2", "Alice", "I am fine.", True, "row1#1")


def test_multiple_sentences_completed_in_one_poll_are_dished_out_in_order() -> None:
    """Meet can jump by more than one sentence between two polls (e.g. a
    slow poll interval) -- every completed sentence must still get its own
    key, dispatched in order, not merged into a single event, and each
    chained to the one before it via `replaces`."""
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], [], emit)
    tracker.update(
        [_row("row1", "First one. Second one. Third starts")],
        ["row1"],
        emit,
    )
    tracker.update(
        [_row("row1", "First one. Second one. Third starts")],
        ["row1"],
        emit,
    )

    assert emits == [
        ("row1", "Alice", "First one.", True, None),
        ("row1#1", "Alice", "Second one.", True, "row1"),
    ]


def test_transient_terminal_punctuation_is_revised_before_emit() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], ["row1"], emit)
    tracker.update([_row("row1", "he.")], ["row1"], emit)
    tracker.update([_row("row1", "Here is a screensh.")], ["row1"], emit)
    tracker.update([_row("row1", "Here is a screenshot of what I mean.")], ["row1"], emit)

    assert emits == []

    tracker.update(
        [_row("row1", "Here is a screenshot of what I mean."), _row("row2", "Next")],
        ["row1", "row2"],
        emit,
    )
    assert emits == [
        ("row1", "Alice", "Here is a screenshot of what I mean.", True, None),
    ]


def test_active_row_must_remain_unchanged_for_settle_interval() -> None:
    now = 0.0

    def clock() -> float:
        return now

    tracker = CaptionTracker(settle=1.2, clock=clock)
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], ["row1"], emit)
    tracker.update([_row("row1", "A complete sentence.")], ["row1"], emit)

    now = 1.1
    tracker.update([_row("row1", "A complete sentence.")], ["row1"], emit)
    assert emits == []

    now = 1.3
    tracker.update([_row("row1", "A complete sentence.")], ["row1"], emit)
    assert emits == [
        ("row1", "Alice", "A complete sentence.", True, None),
    ]


def test_reused_dom_row_resets_the_old_sentence_offset() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], ["row1"], emit)
    tracker.update([_row("row1", "Um.")], ["row1"], emit)
    tracker.update([_row("row1", "Um.")], ["row1"], emit)
    tracker.update([_row("row1", "Okay, this is the replacement.")], ["row1"], emit)
    tracker.update([_row("row1", "Okay, this is the replacement.")], ["row1"], emit)

    assert emits == [
        ("row1", "Alice", "Um.", True, None),
        (
            "row1#1",
            "Alice",
            "Okay, this is the replacement.",
            True,
            "row1",
        ),
    ]


def test_forgotten_row_cleans_up_all_internal_state() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], [], emit)
    tracker.update([_row("row1", "Hello there.")], ["row1"], emit)
    assert "row1" in tracker.raw

    # The row disappears from the live DOM entirely (scrolled away).
    tracker.update([], [], emit)
    assert emits == [("row1", "Alice", "Hello there.", True, None)]
    assert "row1" not in tracker.raw
    assert "row1" not in tracker.settled_len
    assert "row1" not in tracker.active_key
    assert "row1" not in tracker.clone_seq


def test_no_op_when_text_is_unchanged() -> None:
    tracker = _tracker()
    emits, emit = _recorder()
    tracker.update([_row("row1", "")], [], emit)
    tracker.update([_row("row1", "Hello there")], [], emit)
    tracker.update([_row("row1", "Hello there")], [], emit)  # repeat, unchanged
    assert emits == []
