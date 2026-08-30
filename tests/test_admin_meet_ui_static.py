from __future__ import annotations

from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "src" / "ws_collab" / "admin" / "app.js"


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_meet_admin_emit_exposes_role_duplicate_and_raw_caption_fields() -> None:
    source = _source()

    assert '["Key", "Time", "Role", "Speaker", "Text", "Final", "Replaces", "Dup"]' in source
    assert "meetCaptionTextCell(c)" in source
    assert "Raw DOM text:" in source
    assert "meetCaptionKeyNode(c.duplicateOf)" in source


def test_meet_admin_phrases_and_transcript_skip_cross_role_duplicates() -> None:
    source = _source()

    assert "const phraseRows = sortedByKey.filter((c) => c.final && !c.duplicateOf);" in source
    assert "const transcriptRows = sortedByKey.filter((c) => c.final && !c.duplicateOf);" in source
    assert "meetTranscriptSpeaker(c, transcriptRows)" in source


def test_meet_admin_normalizes_meeting_buckets_and_shows_transport_health() -> None:
    source = _source()

    assert "const key = c.meetingUrl ? meetAssignmentKey(c.meetingUrl) : \"(unknown meeting)\";" in source
    assert "const currentMeetingKey = health.meetingUrl ? meetAssignmentKey(health.meetingUrl) : \"\";" in source
    assert "captionTransportByRole" in source
    assert "transport     ${meetTransportLine(transportSource)}" in source
