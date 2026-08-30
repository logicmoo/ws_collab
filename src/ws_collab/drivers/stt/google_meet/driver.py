"""Google Meet caption bridge STT driver (drop-in, real-hardware-free network source).

Rather than decoding raw audio itself, this driver reads text Google's own
captioning engine already produced for a live Meet call, via the server-managed
:mod:`ws_collab.meet_bridge` worker. The worker exposes an internal local API:

* ``GET {base_url}/health`` -> ``{"ok": true, "meetingUrl": ..., ...}``
* ``GET {base_url}/captions?since=<epoch>`` ->
  ``{"captions": [{"at": epoch, "iso": ..., "speaker": ..., "text": ...}, ...], "now": epoch}``

Correlation strategy: each :class:`AudioSegment` carries ``started_at`` (ISO8601)
and ``duration_ms``, describing the wall-clock window the segment covers. This
driver converts that window to an epoch range (with a small settle buffer, since
the bridge finalizes a caption line ~1.2s after it stops changing) and asks the
bridge for every caption line whose timestamp falls inside it, concatenating any
matches in order. No audio is ever sent to the bridge or over any network by
this driver — it only reads already-public caption text over localhost.

Configure as ``google_meet`` (default bridge URL) or ``google_meet:<base_url>``
to point at a non-default bridge address/port. The default base URL can also be
set once via the ``WS_COLLAB_GOOGLE_MEET_URL`` environment variable.

If the bridge is not running (connection refused/timeout), this reports an
honest failure rather than inventing text — matching the project's "no acoustic
model, no pretending" policy used by the deterministic driver for real audio.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import SttDriverSpec
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text

DEFAULT_BASE_URL = "http://127.0.0.1:48699"
# The bridge finalizes a caption line only after it holds still for --settle
# seconds (1.2s by default) plus one poll cycle; pad the correlation window on
# both sides so a segment's own captions aren't missed due to that lag.
_SETTLE_BUFFER_S = 2.0
# The bridge holds the terminal live-caption row until Meet moves on, avoiding
# transient punctuation such as "he." being mistaken for a complete sentence.
# Poll repeatedly so multiple completed sentences can enter the audio window.
_POLL_INTERVAL_S = 0.5
_STABLE_GRACE_S = 1.5
_POLL_BUDGET_S = 6.0


def _parse_iso(value: str) -> float:
    """Convert an RFC3339/ISO-8601 timestamp (optionally trailing 'Z') to epoch."""

    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fetch_captions(base_url: str, since: float, timeout: float = 5.0) -> dict:
    url = f"{base_url.rstrip('/')}/captions?since={since}"
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local trusted bridge
        return json.loads(response.read().decode("utf-8"))


def assemble_complete_captions(
    rows_by_key: dict[str, dict],
    window_lo: float,
    window_hi: float,
) -> tuple[str, list[str]]:
    matched = [
        row
        for row in rows_by_key.values()
        if row.get("final") is True
        and window_lo <= float(row.get("at", 0)) <= window_hi
    ]
    matched.sort(key=lambda row: row.get("at", 0))
    text = " ".join(
        str(row.get("text", "")).strip()
        for row in matched
        if row.get("text")
    ).strip()
    speakers = sorted(
        {str(row.get("speaker", "")) for row in matched if row.get("speaker")}
    )
    return text, speakers


class GoogleMeetAdapter(SttAdapter):
    is_remote = False

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.model = "google-meet-captions"
        self.base_url = base_url

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        try:
            window_start = _parse_iso(segment.started_at)
        except (ValueError, TypeError) as error:
            return Hypothesis.failed(self.name, self.model, f"cannot parse segment.started_at: {error}")
        window_end = window_start + max(0, segment.duration_ms) / 1000.0
        window_lo = window_start - _SETTLE_BUFFER_S
        window_hi = window_end + _SETTLE_BUFFER_S

        loop = asyncio.get_running_loop()

        rows_by_key: dict[str, dict] = {}
        last_text = ""
        speakers: list[str] = []
        since = window_lo
        stable_since: float | None = None
        deadline = time.perf_counter() + _POLL_BUDGET_S
        while True:
            try:
                payload = await loop.run_in_executor(None, _fetch_captions, self.base_url, since)
            except urllib.error.URLError as error:
                return Hypothesis.failed(
                    self.name, self.model,
                    f"google meet bridge unreachable at {self.base_url}: {error.reason if hasattr(error, 'reason') else error}",
                    (time.perf_counter() - start) * 1000,
                )
            except Exception as error:  # noqa: BLE001
                return Hypothesis.failed(self.name, self.model, f"google meet bridge error: {error}", (time.perf_counter() - start) * 1000)

            # `/captions?since=` returns only rows whose `updated_at` moved
            # past our cursor (an ADD or an in-place EDIT of an existing
            # row) — accumulate by `key` rather than replace, so an earlier
            # row that isn't in THIS batch (nothing changed about it) isn't
            # lost from the assembled window text.
            for row in payload.get("captions") or []:
                key = row.get("key") or f"at:{row.get('at')}"
                rows_by_key[key] = row
            since = float(payload.get("now") or since)

            text, speakers = assemble_complete_captions(
                rows_by_key,
                window_lo,
                window_hi,
            )
            now_perf = time.perf_counter()
            if text != last_text:
                last_text = text
                stable_since = now_perf
                # These are complete sentences, but the aggregate hypothesis
                # can still grow while this segment's polling window is open.
                if on_partial and text:
                    on_partial(Hypothesis(
                        engine=self.name, model=self.model, raw_text=text,
                        normalized_text=normalize_text(text), confidence=0.6, language="en",
                        latency_ms=(now_perf - start) * 1000, is_final=False, alternatives=speakers,
                    ))
            if stable_since is not None and (now_perf - stable_since) >= _STABLE_GRACE_S:
                break
            if now_perf >= deadline:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)

        return Hypothesis(
            engine=self.name,
            model=self.model,
            raw_text=last_text,
            normalized_text=normalize_text(last_text),
            # Google's own caption confidence isn't exposed by the bridge; report
            # a fixed, honestly-labeled confidence rather than inventing precision.
            confidence=0.75 if last_text else 0.0,
            language="en",
            latency_ms=(time.perf_counter() - start) * 1000,
            is_final=True,
            alternatives=speakers,
        )


def _build(name: str, config) -> SttAdapter:
    base_url = name.split(":", 1)[1] if ":" in name else ""
    if not base_url:
        base_url = os.environ.get("WS_COLLAB_GOOGLE_MEET_URL", DEFAULT_BASE_URL)
    return GoogleMeetAdapter(name, base_url=base_url)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="google_meet",
        aliases=["google_meet", "meet", "gmeet"],
        build=_build,
        description=(
            "Google Meet live-caption bridge (reads scripts/meet_caption_bridge.py's "
            "/captions HTTP API). Configure as 'google_meet' (default bridge at "
            f"{DEFAULT_BASE_URL}) or 'google_meet:http://host:port' to override."
        ),
        is_remote=False,
    )
