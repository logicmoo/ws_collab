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
    assert '<label for="meet-companion-target">Configuration target</label>' in index
    assert "companionMeetingKey(state.meetCompanion.meetingUrl)" in source
    assert "companionTargetChannelKey()" in source


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


def test_companion_interjector_uses_existing_scoped_routes_and_payload() -> None:
    source = _source()

    assert 'api(`${API_BASE}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`)' in source
    assert "await api(`${API_BASE}/meet/companion-click`, { method: \"POST\", body })" in source
    assert 'method: "DELETE"' in source
    for key in (
        "enabled",
        "action",
        "intervalSeconds",
        "mode",
        "trigger",
        "afterSeconds",
        "silenceMs",
        "minGapSeconds",
        "maxWaitSeconds",
        "audioRmsThreshold",
        "clickMs",
        "gain",
        "f0Hz",
        "f1Hz",
        "f2Hz",
    ):
        assert f"{key}:" in source


def test_meet_admin_has_delete_action_for_driver_and_client_cards() -> None:
    source = _source()

    assert 'actions.setAttribute("aria-label", "Meeting actions")' in source
    assert 'actionButton("Delete", "mini danger"' in source
    assert "if (stableChannelUrl)" in source
    assert 'kind: clientUrls.has(meetAssignmentKey(url)) ? "client" : "driver"' in source
    assert "Leave or switch meetings before deleting" in source
    assert "deleteBtn.disabled = isCurrent" in source


def test_meet_admin_confirms_and_calls_durable_channel_delete() -> None:
    source = _source()

    assert "Delete meeting ${code} (${key})?" in source
    assert "removes it from the Driver/Client lists and deletes its per-channel role and Silence overrides" in source
    assert "Transcript, caption, and event history is preserved." in source
    assert "/meet/channels/forget" in source
    assert "body: { meeting_url: key }" in source
    assert "error.status === 405" in source
    assert "Restart/update the server" in source
    assert "MEET_BRIDGE_BASE}/command" not in source.split("async function forgetMeetChannel", 1)[1].split(
        "function meetRoleOverride", 1
    )[0]
    assert 'actionButton("Clear displayed data", "mini", clearAllMeetSections)' in source


def test_meet_media_controls_are_explicit_and_not_capture_controls() -> None:
    source = _source()
    rows = source.split("function meetUsRows", 1)[1].split(
        "function readOnlyCheck", 1
    )[0]

    assert 'muted ? `Unmute ${noun}` : `Mute ${noun}`' in rows
    assert '"micMuted"' in rows
    assert '"speakersMuted"' in rows
    assert 'target === "mic" ? "mic" : "speakers"' in rows
    assert 'button.setAttribute("aria-pressed"' in rows
    assert 'mediaCell(host, "host", "mic")' in rows
    assert 'mediaCell(companion, "companion", "speakers")' in rows
    actions = rows.split("const actionsFor", 1)[1].split("const ssoLink", 1)[0]
    assert "Mute" not in actions
    assert "Sync devices" in actions
    assert "Foreground" in actions
    assert "Disconnect" in actions
    assert "toggleMeetCapture" not in source
    media_request = source.split("async function postMeetMediaMute", 1)[1].split(
        "async function postMeetSso", 1
    )[0]
    assert "/audio/capture/" not in media_request
    assert "postMeetCommand(" not in media_request
    assert "body: { meeting_url: meetingUrl, role, target, muted }" in source


def test_meet_media_controls_are_absent_for_unavailable_rows() -> None:
    source = _source()
    rows = source.split("function meetUsRows", 1)[1].split(
        "function readOnlyCheck", 1
    )[0]

    assert 'if (!client || client.state !== "in-call") return wrap;' in rows
    assert 'unavailable.disabled = true' in rows
    assert '["GUEST_CLIENT", ssoLink(ssoVal)' in rows
    assert '"Guest/client connectors are not implemented."' in rows


def test_meet_routing_controls_are_scoped_accessible_and_truthful() -> None:
    source = _source()

    assert 'table(["Who", "SSO", "State", "Mic", "Speakers", "Actions"]' in source
    assert 'table(["Who", "SSO", "State", "Meeting"' not in source
    assert '"HOST I/O room"' in source
    assert '"Autostart"' in source
    assert '"Reconnect after unexpected disconnect"' in source
    assert "adapter.available" in source
    assert '" — not configured"' in source
    assert 'select.setAttribute("aria-label", `${role} ${target' in source
    assert '"Pending — Sync required"' in source
    assert '`${API_BASE}/meet/routing/sync`' in source
    assert 'actionButton("Copy meeting"' in source


def test_async_routing_sync_preserves_newer_same_meeting_edits() -> None:
    source = _source()
    sync = source.split("async function syncMeetingDevices", 1)[1].split(
        "async function postMeetSso", 1
    )[0]

    assert "roleRevisions" in source
    assert "routeState.roleRevisions[role] =" in source
    assert "const submittedRevision =" in sync
    assert "const submittedSnapshot =" in sync
    assert "(routeState.roleRevisions[role] || 0) === submittedRevision" in sync
    assert "JSON.stringify(routeState.drafts[role] || {}) === submittedSnapshot" in sync
    assert "Saved/synced previous selection; newer edits pending" in sync


def test_meet_admin_reconciles_delete_without_resurrecting_defaults() -> None:
    source = _source()
    delete_source = source.split("async function forgetMeetChannel", 1)[1].split(
        "function meetRoleOverride", 1
    )[0]
    offline_source = source.split(
        "// Before discovery completes, the in-memory defaults are placeholders.", 1
    )[1].split("return;", 1)[0]

    assert "removeMeetChannelFromRenderedList(key)" in delete_source
    assert "state.meetKnownUrls = kept" in delete_source
    assert "syncCompanionTargetOptions(" in delete_source
    assert "Discard unsaved Silence configuration changes and switch targets?" in delete_source
    assert "Could not delete ${code}" in delete_source
    assert 'resultEl.className = "mono hint error"' in delete_source
    assert "never append hardcoded defaults here" in offline_source
    assert "state.meetKnownUrls.map" in offline_source


def test_companion_interjector_renders_source_and_absence_safe_metrics() -> None:
    source = _source()

    assert '"Default config"' in source
    assert '"override active"' in source
    assert 'health.companionClick && typeof health.companionClick === "object"' in source
    assert '"Runtime metrics unavailable (older worker)"' in source
    assert 'silencesMetric("Backchannels sent"' in source
    assert 'silencesMetric("Suppressed / skipped"' in source
    assert 'silencesMetric("Row breaks observed"' in source
    assert 'silencesMetric("Last trigger reason"' in source
    assert 'silencesMetric("Status / eligibility"' in source
    assert "runtime && runtime.clicksSent" in source


def test_companion_wiring_target_switch_invalidates_and_reloads_safely() -> None:
    source = _source()
    handler = source.split(
        '$("meet-companion-target").onchange = () => {', 1
    )[1].split("syncCompanionTargetOptions();", 1)[0]
    loader = source.split(
        "async function loadCompanionCableWiring()", 1
    )[1].split("async function saveCompanionCableWiring()", 1)[0]
    saver = source.split(
        "async function saveCompanionCableWiring()", 1
    )[1].split("async function applyCompanionCableWiring()", 1)[0]

    assert "companionFormIsDirty() || wiringDirty" in handler
    assert "Discard unsaved Silence and cable wiring changes" in handler
    assert "invalidateCompanionCableWiring();" in handler
    assert "loadCompanionCableWiring();" in handler
    assert "payload = null" in loader
    assert "status.replaceChildren()" in loader
    assert "requestGeneration += 1" in loader
    assert "generation !== state.companionCableWiring.requestGeneration" in loader
    assert "meetingUrl !== companionMeetingKey(state.meetCompanion.meetingUrl)" in loader
    assert "generation !== state.companionCableWiring.requestGeneration" in saver
    assert "const wiringDirty = state.companionCableWiring.dirty;" in source
    assert "Discard unsaved cable wiring changes and switch to the bridge's current room?" in source


def test_async_cable_save_preserves_newer_same_target_edits() -> None:
    source = _source()
    saver = source.split(
        "async function saveCompanionCableWiring()", 1
    )[1].split("async function applyCompanionCableWiring()", 1)[0]

    assert "draftRevision" in source
    assert "state.companionCableWiring.draftRevision += 1" in source
    assert "const submittedRevision =" in saver
    assert "const submittedSnapshot =" in saver
    assert "submittedRevision === state.companionCableWiring.draftRevision" in saver
    assert "submittedSnapshot === JSON.stringify(companionCableDraftSnapshot())" in saver
    assert "Saved previous selection; newer edits pending." in saver
