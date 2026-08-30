from __future__ import annotations

from pathlib import Path


ADMIN = Path(__file__).resolve().parents[1] / "src" / "ws_collab" / "admin"
APP_JS = ADMIN / "app.js"
INDEX_HTML = ADMIN / "index.html"


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


def test_meet_page_links_to_silences_as_the_only_backchannel_editor() -> None:
    source = _source()
    index = INDEX_HTML.read_text(encoding="utf-8")
    meet_page = index.split('<section class="page" data-page="meet">', 1)[1].split(
        '<section class="page" data-page="silences">', 1
    )[0]

    assert "Automatic companion backchannels are configured and monitored on" in meet_page
    assert '<a href="#silences">Silences</a>' in meet_page
    assert 'id="meet-companion-form"' not in meet_page
    assert '<label for="meet-companion-meeting">Exact meeting</label>' in index
    assert "companionMeetingKey(state.meetCompanion.meetingUrl)" in source
    assert "Select a specific meeting before saving." in source


def test_sso_browser_page_loads_and_saves_accessible_consent_toggle() -> None:
    source = _source()

    assert 'requireConsent.id = "br-require-sso-consent";' in source
    assert "requireConsentLabel.htmlFor = requireConsent.id;" in source
    assert "Require confirmation before opening identity-provider sign-in pages" in source
    assert "requireConsent.checked = data.require_sso_consent === true;" in source
    assert "require_sso_consent: requireConsent.checked" in source
    assert "result.textContent = `error: ${error.message}`;" in source
    assert "Typed SSO intent is still required" in source
    assert "every navigation is still logged" in source


def test_companion_interjector_uses_existing_routes_and_exact_payload_keys() -> None:
    source = _source()

    assert 'api(`${V1}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`)' in source
    assert 'api(`${V1}/meet/companion-click`, { method: "POST", body })' in source
    assert 'method: "DELETE"' in source
    assert "Remove the companion interjector override for ${room}" in source
    for key in (
        "meeting_url",
        "enabled",
        "interval_seconds",
        "mode",
        "trigger",
        "after_seconds",
        "silence_ms",
        "min_gap_seconds",
        "max_wait_seconds",
        "audio_rms_threshold",
        "click_ms",
        "gain",
        "sound",
        "phrase",
        "f0_hz",
        "f1_hz",
        "f2_hz",
    ):
        assert f"{key}:" in source


def test_companion_interjector_renders_source_and_absence_safe_metrics() -> None:
    source = _source()

    assert '"Inherited global defaults"' in source
    assert '"Persisted meeting override"' in source
    assert 'health.companionClick && typeof health.companionClick === "object"' in source
    assert '"Runtime metrics unavailable (older worker)"' in source
    assert 'silencesMetric("Backchannels sent"' in source
    assert 'silencesMetric("Suppressed / skipped"' in source
    assert 'silencesMetric("Row breaks observed"' in source
    assert 'silencesMetric("Last trigger reason"' in source
    assert 'silencesMetric("Status / eligibility"' in source
    assert "runtime && runtime.clicksSent" in source
