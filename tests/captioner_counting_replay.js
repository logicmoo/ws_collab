"use strict";

// Offline: replays saved VAD events and complete native ASR result arrays. No browser/network.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

class Node {
  constructor(tag = "span") {
    this.tag = tag;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.style = {setProperty() {}};
    this._text = "";
    this.hidden = false;
  }
  set textContent(text) { this.replaceChildren(); this._text = String(text); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) {
    if (node.tag === "fragment") {
      [...node.children].forEach(child => this.appendChild(child));
    } else {
      node.remove();
      this.children.push(node);
      node.parentNode = this;
    }
    return node;
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  replaceChildren() {
    [...this.children].forEach(child => child.remove());
    this._text = "";
  }
  querySelector() { return null; }
}

const documentRef = {
  createElement: tag => new Node(tag),
  createElementNS: (_, tag) => new Node(tag),
  createDocumentFragment: () => new Node("fragment"),
};
const clone = value => JSON.parse(JSON.stringify(value));
const flatten = root => [root, ...root.children.flatMap(flatten)];
const visibleText = root => root.hidden ? "" : root._text + root.children.map(visibleText).join("");
const keyOf = pause => `${pause.startAt ?? pause.start_at}|${pause.endAt ?? pause.end_at}`;

function recognitionRows(observation, R, useRuntimeUpdates = true) {
  if (useRuntimeUpdates && typeof R.recognitionUpdates === "function") {
    const event = {resultIndex: observation.result_index, results: observation.results.map(row => {
      const result = row.alternatives.map(alternative => ({...alternative}));
      result.isFinal = row.is_final;
      return result;
    })};
    return R.recognitionUpdates(event).map(update => ({
      index: update.index, is_final: update.isFinal,
      alternatives: [{transcript: update.text, confidence: update.confidence}],
    }));
  }
  return observation.results.filter(row => row.index >= observation.result_index);
}

function replayCase(test, R, T, {
  injectMapper = true, retainRevisions = true, useRuntimeUpdates = true, associationStrategy = null,
} = {}) {
  const mapped = injectMapper && typeof T.silencePosition === "function";
  const association = new R.PauseAssociation(mapped ? {positionMapper: T.silencePosition} : {});
  const finals = [], finalized = new Set(), steps = [], originalPauses = new Map();
  const render = new Node("div");
  let wall = 0, monotonic = 0;
  let tail = new T.LiveTranscriptTail(documentRef, render, {
    wallNow: () => wall, monotonicNow: () => monotonic,
  });
  let currentStatus = null, statusObservedAt = 0;
  for (const [eventIndex, observation] of test.observations.entries()) {
    wall = observation.wall_ms || 0;
    monotonic = observation.elapsed_ms;
    if (observation.type === "vad_status") {
      currentStatus = observation.status;
      statusObservedAt = monotonic;
      tail.observe(currentStatus);
    }
    if (observation.type === "vad_event") {
      const event = observation.event;
      if (event.type === "speech_end") association.silenceStarted(event.at);
      if (event.type === "pause") {
        association.record(event);
        associationStrategy?.onRecord?.({association, event, finals, observation});
        originalPauses.set(keyOf(event), event);
      }
    }
    if (observation.type !== "asr_full_result_event") continue;
    const fullText = observation.results.map(row => row.alternatives[0]?.transcript.trim() || "").join(" ");
    const eventSteps = [];
    for (const row of recognitionRows(observation, R, useRuntimeUpdates)) {
      if (finalized.has(row.index)) continue;
      const text = String(row.alternatives[0]?.transcript || "").trim();
      if (!text) continue;
      const utterance = `${test.case_id}:${row.index}`;
      association.updateTranscript(text, utterance);
      associationStrategy?.onTranscript?.({association, text, utterance, finals, observation});
      const snapshot = association.snapshot(text, {reserve: row.is_final});
      const event = {
        type: row.is_final ? "STT_FINAL_RESULT" : "STT_PARTIAL_RESULT",
        source_id: "browser_captioner", ts: new Date(wall).toISOString(),
        data: {engine: "browser_captioner", raw_text: text, is_final: row.is_final,
          session_id: "isolated-synthetic-test", utterance_id: utterance, ...snapshot.metadata},
      };
      if (!retainRevisions) {
        tail = new T.LiveTranscriptTail(documentRef, render, {
          wallNow: () => wall, monotonicNow: () => monotonic,
        });
        tail.observe(currentStatus, {ageMs: Math.max(0, monotonic - statusObservedAt)});
      }
      let displayed;
      const finalPrefixText = finals.map(previous => previous.text).join(" ");
      if (row.is_final) {
        displayed = tail.finish(event) || T.finalCaption(event);
        finals.push(displayed);
      }
      T.renderTranscript(documentRef, render, finals);
      if (!row.is_final) {
        tail.setInterim(event);
        displayed = tail.interim;
      }
      tail.update(finals.at(-1) || null);
      const step = {
        event_index: eventIndex, elapsed_ms: monotonic, wall_ms: wall, result_index: row.index,
        text, is_final: row.is_final, full_result_text: fullText,
        final_prefix_text: finalPrefixText,
        metadata: clone(snapshot.metadata),
        displayed_pauses: clone(displayed?.pauses || []),
        displayed_caption_text: displayed?.text || null,
        visible_text: visibleText(render),
        markers: flatten(render).filter(node => !node.hidden && node.className?.includes("silence-marker"))
          .map(node => ({text: node.textContent, aria_label: node.attributes["aria-label"]})),
      };
      steps.push(step);
      eventSteps.push(step);
      if (row.is_final) {
        association.commit(snapshot);
        finalized.add(row.index);
      }
    }
    if (eventSteps.length) eventSteps.at(-1).last_step_in_native_event = true;
  }
  const allFinalPauses = finals.flatMap(row => row.pauses);
  const coverage = [...originalPauses].map(([key, pause]) => {
    const displayed = steps.flatMap(step => step.displayed_pauses.filter(item => keyOf(item) === key)
      .map(item => ({text: step.text, final: step.is_final, after_char: item.afterChar,
        duration_ms: item.durationMs, elapsed_ms: step.elapsed_ms})));
    return {key, measured_ms: pause.duration_ms,
      reached_final: allFinalPauses.some(item => keyOf(item) === key),
      final_occurrences: allFinalPauses.filter(item => keyOf(item) === key).length,
      changed_duration: displayed.some(item => item.duration_ms !== pause.duration_ms),
      displayed_positions: [...new Set(displayed.map(item => item.after_char))],
      observations: displayed};
  });
  return {case_id: test.case_id, mapper_injected: mapped,
    recognition_updates_used: useRuntimeUpdates && typeof R.recognitionUpdates === "function",
    revision_retention_exported: typeof T.reviseCaption === "function", steps, pause_coverage: coverage,
    final_texts: finals.map(row => row.text),
    final_rows: finals.map(row => ({text: row.text, utteranceId: row.utteranceId, pauses: row.pauses})),
    measured_pause_count: coverage.length,
    final_pause_loss_count: coverage.filter(item => !item.reached_final).length,
    duration_change_count: coverage.filter(item => item.changed_duration).length,
    duplicated_final_pause_count: coverage.filter(item => item.final_occurrences > 1).length,
    duplicated_final_pauses: coverage.filter(item => item.final_occurrences > 1)
      .map(item => ({key: item.key, measured_ms: item.measured_ms, final_occurrences: item.final_occurrences}))};
}

function compareReplay(nativeReport, baselineR, baselineT, currentR, currentT) {
  const baseline = nativeReport.cases.map(test => replayCase(test, baselineR, baselineT));
  const current = nativeReport.cases.map(test => replayCase(test, currentR, currentT));
  const changes = [];
  baseline.forEach((test, caseIndex) => {
    const updated = current[caseIndex];
    test.steps.filter(step => step.last_step_in_native_event).forEach(step => {
      const newer = updated.steps.find(item => item.event_index === step.event_index && item.last_step_in_native_event);
      if (!newer) return;
      const positions = item => item.displayed_pauses.map(pause => [keyOf(pause), pause.afterChar, pause.durationMs]);
      if (step.text !== newer.text || JSON.stringify(positions(step)) !== JSON.stringify(positions(newer))) {
        changes.push({case_id: test.case_id, elapsed_ms: step.elapsed_ms,
          result_index: step.result_index, text: step.text, current_text: newer.text, is_final: step.is_final,
          baseline_positions: positions(step), current_positions: positions(newer),
          baseline_visible_text: step.visible_text, current_visible_text: newer.visible_text});
      }
    });
  });
  return {
    replay_scope: "Replays captured event order and receipt times through actual PauseAssociation and persistent LiveTranscriptTail; no recognition rerun and no invented word timing.",
    limitations: [
      "VAD transitions are replayed as observed, not recomputed. Native full-result arrays are unchanged.",
      "The native record may not contain numeric compaction (1 2 3 -> 123); lack of differences does not disprove that separate regression.",
      "Current replay uses Runtime.recognitionUpdates when exported; baseline fallback processes each result index separately.",
      "Final acceptance/commit is immediate and local; backend delivery races are outside this replay.",
      "Live-tail timers extrapolate sparse captured VAD status transitions; only completed-pause metadata/positions are compared.",
    ],
    baseline, current, changes,
    summary: {case_count: baseline.length, changed_display_metadata_steps: changes.length,
      baseline_final_pause_losses: baseline.reduce((n, test) => n + test.final_pause_loss_count, 0),
      current_final_pause_losses: current.reduce((n, test) => n + test.final_pause_loss_count, 0),
      baseline_duration_changes: baseline.reduce((n, test) => n + test.duration_change_count, 0),
      current_duration_changes: current.reduce((n, test) => n + test.duration_change_count, 0),
      baseline_duplicated_final_pauses: baseline.reduce((n, test) => n + test.duplicated_final_pause_count, 0),
      current_duplicated_final_pauses: current.reduce((n, test) => n + test.duplicated_final_pause_count, 0)},
  };
}

module.exports = {Node, documentRef, recognitionRows, replayCase, compareReplay};
if (require.main === module) {
  const [input, baselineRuntime, baselineTranscript, currentRuntime, currentTranscript, output] = process.argv.slice(2);
  if (!output) throw new Error("Usage: node tests\\captioner_counting_replay.js NATIVE_REPORT BASELINE_RUNTIME BASELINE_TRANSCRIPT CURRENT_RUNTIME CURRENT_TRANSCRIPT OUTPUT");
  const native = JSON.parse(fs.readFileSync(input, "utf8"));
  const report = compareReplay(native, require(path.resolve(baselineRuntime)), require(path.resolve(baselineTranscript)),
    require(path.resolve(currentRuntime)), require(path.resolve(currentTranscript)));
  report.inputs = Object.fromEntries(Object.entries({
    native_report: input, baseline_runtime: baselineRuntime, baseline_transcript: baselineTranscript,
    current_runtime: currentRuntime, current_transcript: currentTranscript,
  }).map(([name, file]) => [name, {path: path.resolve(file),
    sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex")}]));
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report.summary));
}
