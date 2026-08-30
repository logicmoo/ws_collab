"""Caption buffering: hold Meet's active tail until it is a complete sentence."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

# A sentence boundary: one or more .!? immediately followed by whitespace or
# end-of-string. Deliberately simple (this is ASR captions, not copy-edited
# prose).
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")


def _first_sentence_boundary(text: str) -> int | None:
    """Index just past the FIRST sentence-ending punctuation in `text`, or
    None if it doesn't contain one yet."""
    m = _SENTENCE_END_RE.search(text)
    return m.end() if m else None


class CaptionTracker:
    """Split growing Meet rows without freezing the active, revisable tail.

    Meet frequently keeps the SAME DOM row growing for an entire
    monologue (one speaker, one row, thousands of characters) rather than
    starting a new row per utterance. Internal sentences can be frozen as soon
    as soon as Meet advances to a newer row. The active row stays buffered
    because Meet often shows transient punctuation while it is still revising
    that text. Its completed sentences are emitted only after the whole row
    remains unchanged for the settle interval.

    Three explicit per-DOM-row buffers:
      1. `raw`      -- an exact mirror of Meet's own row text, untouched.
      2. `pending`  -- the still-unsettled tail of `raw` (tracked as an
                        offset, not copied), including the terminal sentence
                        in the last live row.
      3. `ready`    -- completed sentences peeled off of `pending` the
                        instant they cross a boundary, queued here and then
                        dished out to the real consumer (mailbox emit(), one
                        call per sentence) in the SAME poll, in order -- a
                        real queue rather than an inline emit so multiple
                        sentences completing between two polls are still
                        dished out as distinct, separately-ordered items.
    """

    def __init__(
        self,
        settle: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settle = max(0.0, settle)
        self._clock = clock
        # Buffer 1 -- RAW: last-seen full text of each DOM row, unmodified.
        self.raw: dict[str, str] = {}
        # Buffer 2 -- PENDING: how much of `raw` has already been settled
        # into `ready`/dished-out sentences (an offset into `raw`, not a
        # copy) + which key is currently receiving updates for what's left.
        self.settled_len: dict[str, int] = {}
        self.active_key: dict[str, str] = {}
        self.clone_seq: dict[str, int] = {}
        self.speakers: dict[str, str] = {}
        self.changed_at: dict[str, float] = {}
        # Per DOM row: what key the CURRENT active_key replaces (the key
        # that was just frozen when this active_key was minted) -- makes the
        # chain explicit for a consumer (row1 -> row1#1 -> row1#2 is really
        # one continuous utterance stream Meet never split into separate
        # rows itself) instead of leaving it to be inferred from the key
        # naming convention. None for a row's very first/original key.
        self.replaces: dict[str, str | None] = {}
        # Buffer 3 -- READY: completed sentences waiting to be dished out,
        # one at a time, to the real consumer. Populated then fully drained
        # within the same update() call (no added latency) but kept as an
        # explicit queue so the dispatch step is its own, separate stage.
        # Every dispatched item is final: incomplete active text never leaves
        # the pending buffer.
        # `replaces`: the key this one continues from (None if it's the
        # row's original key).
        self.ready: list[tuple[str, str, str, bool, str | None]] = []
        # On the very first poll after a (re)start, whatever's already
        # visible could be minutes of accumulated on-screen history (Meet's
        # captions region keeps a long scroll-back) rather than something
        # newly said -- baseline it silently instead of relaying it as a
        # wall of "new" updates every single time the bridge restarts.
        self.baselined = False

    def _queue_completed(
        self,
        dom_key: str,
        speaker: str,
        text: str,
    ) -> None:
        settled_len = self.settled_len.get(dom_key, 0)
        active_key = self.active_key.get(dom_key, dom_key)
        replaces = self.replaces.get(dom_key)
        while True:
            pending = text[settled_len:]
            boundary = _first_sentence_boundary(pending)
            if boundary is None:
                break
            sentence = pending[:boundary].strip()
            if sentence:
                self.ready.append((active_key, speaker, sentence, True, replaces))
            settled_len += boundary
            self.clone_seq[dom_key] = self.clone_seq.get(dom_key, 0) + 1
            replaces = active_key
            active_key = f"{dom_key}#{self.clone_seq[dom_key]}"
        self.settled_len[dom_key] = settled_len
        self.active_key[dom_key] = active_key
        self.replaces[dom_key] = replaces

    def update(self, rows: list[dict[str, str]], live_keys: list[str], emit) -> None:
        if not self.baselined:
            if not rows:
                return
            self.baselined = True
            now = self._clock()
            for row in rows:
                self.raw[row["key"]] = row["text"]
                self.settled_len[row["key"]] = len(row["text"])
                self.speakers[row["key"]] = row["speaker"]
                self.changed_at[row["key"]] = now
            return
        seen_keys = set()
        active_dom_key = live_keys[-1] if live_keys else (rows[-1]["key"] if rows else None)
        now = self._clock()
        for row in rows:
            dom_key, speaker, text = row["key"], row["speaker"], row["text"]
            text = text.strip()
            seen_keys.add(dom_key)
            previous_text = self.raw.get(dom_key, "")
            changed = previous_text != text
            self.speakers[dom_key] = speaker
            if len(text) < 2:
                continue
            settled_len = self.settled_len.get(dom_key, 0)
            settled_prefix = previous_text[:settled_len]
            if settled_prefix and not text.startswith(settled_prefix):
                # Meet reused this DOM node for replacement text rather than
                # extending it. The old offset belongs to the prior wording;
                # applying it to the replacement would turn "Okay." into
                # fragments such as "y.".
                self.settled_len[dom_key] = 0
            self.raw[dom_key] = text  # buffer 1: mirror updated first
            if changed:
                self.changed_at[dom_key] = now
            if dom_key == active_dom_key and (
                changed or now - self.changed_at.get(dom_key, now) < self.settle
            ):
                continue
            self._queue_completed(dom_key, speaker, text)
        # A removed row can no longer be revised, so release any complete
        # terminal sentence it was holding before discarding its state.
        for dom_key in list(self.raw):
            if dom_key not in seen_keys:
                self._queue_completed(
                    dom_key,
                    self.speakers.get(dom_key, "Speaker"),
                    self.raw[dom_key],
                )
        for key, speaker, text, final, replaces in self.ready:
            emit(key, speaker, text, final=final, replaces=replaces)
        self.ready.clear()
        # Forget rows no longer present at all, so these dicts never grow
        # unbounded across a long meeting.
        for dom_key in list(self.raw):
            if dom_key not in seen_keys:
                self.raw.pop(dom_key, None)
                self.settled_len.pop(dom_key, None)
                self.active_key.pop(dom_key, None)
                self.clone_seq.pop(dom_key, None)
                self.replaces.pop(dom_key, None)
                self.speakers.pop(dom_key, None)
                self.changed_at.pop(dom_key, None)
