"""Caption buffering: trap every row-text CHANGE and split it into sentences.

Ported faithfully from the original ``meet_caption_bridge.py``'s
``CaptionTracker`` (workbench monorepo) -- this is the most valuable, most
thoroughly live-tested piece of the whole bridge, so it is reproduced here
unchanged in behavior. See ``tests/test_meet_bridge_tracker.py`` for the
regression suite ported alongside it.
"""

from __future__ import annotations

import re

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
    """Trap every row-text CHANGE, keyed by DOM element identity, and relay
    it immediately -- zero latency, no settle timers on the bridge side at
    all. Google's live captions get revised unpredictably (confirmed live:
    identical audio produced a "first version", then a few seconds later a
    completely different "corrected version" of the same stretch of
    speech) -- every attempted "decide what's final on the bridge side"
    heuristic ended up either hiding updates the listener needed to see,
    or replaying huge duplicate blobs. So the bridge still never WAITS
    before relaying a change.

    BUT: Meet frequently keeps the SAME DOM row growing for an entire
    monologue (one speaker, one row, thousands of characters) rather than
    starting a new row per utterance -- confirmed live (a single row grew
    past 3000 chars covering many minutes of speech). Relaying that as one
    ever-growing "line" makes the raw emit stream useless as a log of
    distinct speech events. So the moment the growing text crosses a
    completed sentence (`.`/`!`/`?`), that finished sentence is FROZEN
    under the key it was already growing under (it will never be updated
    again -- "give the last line the old key") and a brand-new key is
    minted for whatever comes next in the same DOM row -- the still-growing
    line keeps extending in FRONT of what's already been dished out, never
    behind it.

    Three explicit per-DOM-row buffers:
      1. `raw`      -- an exact mirror of Meet's own row text, untouched.
      2. `pending`  -- the still-unsettled tail of `raw` (tracked as an
                        offset, not copied) that hasn't crossed a sentence
                        boundary yet; every CHANGE to it is still relayed
                        immediately (in place, same key) so the consumer
                        sees the line growing in real time, same as before.
      3. `ready`    -- completed sentences peeled off of `pending` the
                        instant they cross a boundary, queued here and then
                        dished out to the real consumer (mailbox emit(), one
                        call per sentence) in the SAME poll, in order -- a
                        real queue rather than an inline emit so multiple
                        sentences completing between two polls are still
                        dished out as distinct, separately-ordered items.
    """

    def __init__(self, settle: float) -> None:
        self.settle = settle  # kept for CLI/call-site compatibility; unused
        # Buffer 1 -- RAW: last-seen full text of each DOM row, unmodified.
        self.raw: dict[str, str] = {}
        # Buffer 2 -- PENDING: how much of `raw` has already been settled
        # into `ready`/dished-out sentences (an offset into `raw`, not a
        # copy) + which key is currently receiving updates for what's left.
        self.settled_len: dict[str, int] = {}
        self.active_key: dict[str, str] = {}
        self.clone_seq: dict[str, int] = {}
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
        # `final`: True for a completed sentence ("phrase") that will never
        # be updated again once dished out; False for the still-growing
        # live remainder -- lets a consumer distinguish settled phrases from
        # in-progress speech without guessing from the text itself.
        # `replaces`: the key this one continues from (None if it's the
        # row's original key).
        self.ready: list[tuple[str, str, str, bool, str | None]] = []  # (key, speaker, text, final, replaces)
        # On the very first poll after a (re)start, whatever's already
        # visible could be minutes of accumulated on-screen history (Meet's
        # captions region keeps a long scroll-back) rather than something
        # newly said -- baseline it silently instead of relaying it as a
        # wall of "new" updates every single time the bridge restarts.
        self.baselined = False

    def update(self, rows: list[dict[str, str]], live_keys: list[str], emit) -> None:
        if not self.baselined:
            self.baselined = True
            for row in rows:
                self.raw[row["key"]] = row["text"]
                self.settled_len[row["key"]] = len(row["text"])
            return
        seen_keys = set()
        for row in rows:
            dom_key, speaker, text = row["key"], row["speaker"], row["text"]
            text = text.strip()
            seen_keys.add(dom_key)
            if len(text) < 2 or self.raw.get(dom_key) == text:
                continue
            self.raw[dom_key] = text  # buffer 1: mirror updated first
            settled_len = self.settled_len.get(dom_key, 0)
            active_key = self.active_key.get(dom_key, dom_key)
            replaces = self.replaces.get(dom_key)
            # Consume buffer 2 (the still-unsettled tail): peel off every
            # COMPLETED sentence, queuing each into buffer 3 (`ready`) --
            # the still-growing part always stays IN FRONT of (after) the
            # already-settled offset, never overlapping it.
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
                replaces = active_key  # the NEXT key continues from this one
                active_key = f"{dom_key}#{self.clone_seq[dom_key]}"
            self.settled_len[dom_key] = settled_len
            self.active_key[dom_key] = active_key
            self.replaces[dom_key] = replaces
            # Whatever hasn't crossed a boundary yet is still relayed live,
            # in place, under the (still-open) active key -- the consumer
            # keeps seeing the growing line in real time, it just no longer
            # carries the already-dished-out sentences in front of it.
            live_pending = text[settled_len:].strip()
            if live_pending:
                self.ready.append((active_key, speaker, live_pending, False, replaces))
        # Dispatch buffer 3: dish out every queued item to the real
        # consumer, one at a time, in order, then clear the queue.
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
