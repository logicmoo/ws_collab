"""Retired Google Meet STT compatibility helpers.

Google Meet captions are meeting/chat context, not an ``AudioSegment`` STT
driver.  This module intentionally exposes no ``SttDriverSpec``. The assembly
helper remains temporarily for callers that process stored Meet caption rows.
"""

from __future__ import annotations


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
