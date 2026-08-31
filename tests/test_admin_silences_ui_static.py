from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "src" / "ws_collab" / "admin"
APP_JS = ADMIN / "app.js"
INDEX_HTML = ADMIN / "index.html"
SILENCES_JS = ADMIN / "silences.js"


def _admin_source() -> tuple[str, str, str]:
    return (
        INDEX_HTML.read_text(encoding="utf-8"),
        APP_JS.read_text(encoding="utf-8"),
        SILENCES_JS.read_text(encoding="utf-8"),
    )


def test_silences_page_owns_backchannel_editor_and_observation_harness() -> None:
    index, app, logic = _admin_source()
    silences_page = index.split('<section class="page" data-page="silences">', 1)[1].split(
        '<section class="page" data-page="browser">', 1
    )[0]

    assert 'data-page="silences"' in index
    assert '<span class="nav-label">Silences</span>' in index
    assert "Observation-first turn-taking harness" in index
    assert "Continue mode releases one queued companion turn" in index
    assert "silences: loadSilencesWithPolling" in app
    assert "WsCollabSilencesLogic" in logic
    assert 'id="meet-companion-form"' in silences_page
    assert "Every action uses the same trigger, detector, and scoped inheritance settings." in silences_page
    assert silences_page.count('id="meet-companion-action"') == 1
    assert '<option value="continue">Continue</option>' in silences_page
    assert '<option value="nothing">Say nothing</option>' in silences_page
    assert '<option value="say:uh">Say “uh”</option>' in silences_page
    assert '<option value="say:uhuh">Say “uhuh”</option>' in silences_page
    assert '<option value="say:hmm">Say “hmm”</option>' in silences_page
    assert '<option value="reactive">On silence</option>' in silences_page
    assert '<option value="fixed">Every N seconds</option>' in silences_page
    assert "Continue opens the conversational floor" in silences_page
    assert 'id="meet-companion-enabled"' in silences_page
    assert 'id="meet-companion-reset"' in silences_page
    assert 'id="meet-companion-source"' in silences_page
    assert 'id="meet-companion-result"' in silences_page
    assert "Continue settings" not in silences_page
    assert "used only by Say" in silences_page


def test_silences_backchannel_routes_conditionals_and_absence_safe_metrics() -> None:
    _index, app, _logic = _admin_source()

    assert 'api(`${V1}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`)' in app
    assert 'api(`${V1}/meet/companion-click`, { method: "POST", body })' in app
    assert 'method: "DELETE"' in app
    assert 'action: $("meet-companion-action").value' in app
    assert 'document.querySelectorAll(".companion-silence-field")' in app
    assert 'document.querySelectorAll(".companion-interval-field")' in app
    assert '"Runtime metrics unavailable (older worker)"' in app
    for label in (
        "Backchannels sent",
        "Actions evaluated",
        "Say nothing no-ops",
        "Floor grants",
        "Floor open",
        "Floor granted to",
        "Floor deferred",
        "Last deferred reason",
        "Last action",
        "Suppressed / skipped",
        "Row breaks observed",
        "Phrase last sent",
        "Last trigger mode",
        "Last trigger reason",
        "Last trigger time",
        "Current silence",
        "Companion readiness",
        "Output queue",
    ):
        assert f'silencesMetric("{label}"' in app


def test_continue_ui_disables_interval_and_queues_test_agent_turns() -> None:
    index, app, _logic = _admin_source()

    assert "Every N seconds is unavailable for Continue" in index
    assert "intervalOption.disabled = isContinue" in app
    assert 'if (isContinue && mode.value === "fixed") mode.value = "reactive"' in app
    assert 'field.hidden = isContinue' not in app
    assert 'control.disabled = isContinue' not in app
    assert 'api(`${V1}/meet/floor/queue`' in app
    assert 'agent_id: "companion"' in app
    assert 'if (role !== "companion"' in app


def test_silences_caption_feed_filters_final_non_duplicates() -> None:
    _index, app, logic = _admin_source()

    assert 'api(`${MEET_BRIDGE_BASE}/captions?${query}`)' in app
    assert 'new URLSearchParams({ since: String(run.startedAtSec), fromEnd: "100" })' in app
    assert "if (!run || !caption || !caption.final || caption.duplicateOf) return null;" in logic
    assert "logic.scoreCaption(run, caption)" in app


def test_silences_turn_list_preserves_existing_pause_behaviour() -> None:
    _index, app, _logic = _admin_source()

    assert 'box.matches(":hover") || hasSelectionIn("sil-turns") || silencesScrolledUp()' in app
    assert "Turn list paused while hovered, selected, or scrolled up." in app
    assert "renderSilencesTurns(silencesRun(), { force: true })" in app


def test_silences_scoped_config_controls_sources_and_test_lease() -> None:
    index, app, _logic = _admin_source()
    silences_page = index.split('<section class="page" data-page="silences">', 1)[1].split(
        '<section class="page" data-page="browser">', 1
    )[0]

    assert silences_page.count('id="meet-companion-target"') == 1
    assert '<label for="meet-companion-target">Configuration target</label>' in silences_page
    assert 'aria-describedby="meet-companion-target-hint"' in silences_page
    assert 'id="meet-companion-scope"' not in silences_page
    assert 'id="meet-companion-meeting"' not in silences_page
    default_at = silences_page.index('<option value="global">Default Config</option>')
    test_at = silences_page.index('<option value="test">Tests here</option>')
    channels_at = silences_page.index('<optgroup label="Channels"')
    assert default_at < test_at < channels_at
    assert ">Save override</button>" in silences_page
    assert ">Reset from defaults</button>" in silences_page
    assert ">Save to defaults</button>" in silences_page
    assert ">Restore built-in defaults</button>" in silences_page
    assert '"Default config"' in app
    assert '"Test" : "Channel"' in app
    assert '"override active"' in app
    assert "paintCompanionFieldSources(config)" in app
    assert "replace_override: true" in app
    assert "override: companionDelta(values, defaults)" in app
    assert 'body: { scope: "global", replace_override: true, override: values }' in app
    assert "Reset from defaults deletes this ${scope} override" in app
    assert "Reloaded saved global defaults; unsaved edits were discarded." in app
    assert "Discard unsaved Silence configuration changes and switch targets?" in app
    assert "Discard unsaved Tests here changes and switch test scenarios?" in app
    assert 'params.set("channel_key", companionTestChannelKey())' in app
    assert 'test_profile: scope === "test" ? state.meetCompanion.testProfile' in app
    assert 'scope === "test"' in app
    assert "?${params}`" in app
    assert "Discard unsaved Tests here changes and switch to the bridge's current room?" in app
    assert "state.meetCompanion.meetingUrl = current" in app
    assert "`test:${state.meetCompanion.testProfile}:${companionTestChannelKey()}`" in app
    assert "/meet/companion-click/test-session" in app
    assert "await endSilencesTestSession()" in app


def test_silences_normalization_and_scoring_logic() -> None:
    script = r"""
const logic = require(process.argv[1]);
const run = logic.createRun({ testId: "count20", firstRole: "host", startedAtSec: 0 });
const rows = [];
rows.push(logic.scoreCaption(run, { key: "one", at: 1, role: "host", text: "ONE!", final: true }));
rows.push(logic.scoreCaption(run, { key: "two", at: 1.25, role: "host", text: "2", final: true }));
rows.push(logic.scoreCaption(run, { key: "eight", at: 2, role: "host", text: "eight", final: true }));
rows.push(logic.scoreCaption(run, { key: "ignored-partial", at: 3, role: "companion", text: "nine", final: false }));
rows.push(logic.scoreCaption(run, { key: "ignored-dup", at: 4, role: "companion", text: "nine", final: true, duplicateOf: "host:nine" }));
const abc = logic.createRun({ testId: "abcs", firstRole: "companion", startedAtSec: 0 });
rows.push(logic.scoreCaption(abc, { key: "sea", at: 1, role: "companion", text: "Sea.", final: true }));
const payload = {
  statuses: rows.filter(Boolean).map((row) => [row.status, row.errorClass, row.observedToken, row.latencyMs]),
  countSummary: logic.summarizeRun(run),
  normalized: [logic.canonicalToken("count20", "7."), logic.canonicalToken("abcs", "you"), logic.canonicalToken("abcs", "double you")],
};
console.log(JSON.stringify(payload));
"""
    result = subprocess.run(
        ["node", "-e", script, str(SILENCES_JS)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)

    assert payload["statuses"][0][:3] == ["correct", "", "one"]
    assert payload["statuses"][1][:3] == ["mis-attributed", "mis-attributed", "two"]
    assert payload["statuses"][2][:3] == ["sequence-error", "dropped", "eight"]
    assert payload["statuses"][3][:3] == ["sequence-error", "dropped", "C"]
    assert payload["countSummary"]["completed"] == 8
    assert payload["countSummary"]["errors"] == 2
    assert payload["normalized"] == ["seven", "U", "W"]
