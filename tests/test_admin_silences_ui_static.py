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
    assert "Drive mode disabled" in index
    assert "agent voice not yet wired" in index
    assert "silences: loadSilencesWithPolling" in app
    assert "WsCollabSilencesLogic" in logic
    assert 'id="meet-companion-form"' in silences_page
    assert "Silences is the authoritative operator surface." in silences_page
    assert '<option value="uh">uh</option>' in silences_page
    assert '<option value="uhuh">uhuh</option>' in silences_page
    assert '<option value="hmm">hmm</option>' in silences_page
    assert '<option value="reactive">On silence</option>' in silences_page
    assert '<option value="fixed">Every N seconds</option>' in silences_page
    assert "one backchannel per continuous silence" in silences_page
    assert 'id="meet-companion-enabled"' in silences_page
    assert 'id="meet-companion-reset"' in silences_page
    assert 'id="meet-companion-source"' in silences_page
    assert 'id="meet-companion-result"' in silences_page


def test_silences_backchannel_routes_conditionals_and_absence_safe_metrics() -> None:
    _index, app, _logic = _admin_source()

    assert 'api(`${V1}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`)' in app
    assert 'api(`${V1}/meet/companion-click`, { method: "POST", body })' in app
    assert 'method: "DELETE"' in app
    assert 'phrase: $("meet-companion-sound").value' in app
    assert 'sound: "uh"' in app
    assert 'document.querySelectorAll(".companion-silence-field")' in app
    assert 'document.querySelectorAll(".companion-interval-field")' in app
    assert '"Runtime metrics unavailable (older worker)"' in app
    for label in (
        "Backchannels sent",
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
