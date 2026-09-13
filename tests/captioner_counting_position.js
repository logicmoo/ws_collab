"use strict";

// Offline experiment only. Strategies mutate test-owned association instances, never production defaults.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const replay = require("./captioner_counting_replay.js");
const vad = require("./captioner_counting_vad.js");
const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];
const keyOf = pause => `${pause.startAt ?? pause.start_at}|${pause.endAt ?? pause.end_at}`;

function countingTokens(text) {
  const pieces = String(text).toLowerCase().match(/[a-z]+|[0-9]+/g) || [];
  return pieces.flatMap(piece => /^[0-9]+$/.test(piece) ? [...piece]
    : WORDS.includes(piece) ? [String(WORDS.indexOf(piece))] : [`unknown:${piece}`]);
}

function wordBoundary(prefixText, text, afterChar, reference) {
  const characters = [...text];
  if (!Number.isInteger(afterChar) || afterChar < 0 || afterChar > characters.length) {
    return {status: "invalid_character_boundary"};
  }
  const context = countingTokens(`${prefixText} ${text}`);
  if (context.some((word, index) => word !== reference[index])) {
    return {status: "unscorable_lexical_replacement", context};
  }
  const before = countingTokens(`${prefixText} ${characters.slice(0, afterChar).join("")}`);
  if (before.some((word, index) => word !== reference[index])) {
    return {status: "inside_word_or_unscorable_prefix", context};
  }
  return {status: "scorable", boundary: before.length, represented_words: context.length};
}

function makeStrategy({refineOpen = false, deferInitialEmpty = false} = {}) {
  const candidates = new Set(), actions = [];
  return {
    actions,
    onRecord({association, event, finals}) {
      if (!deferInitialEmpty || finals.length) return;
      for (const entry of association.pending) {
        if (!entry.prefix && keyOf(entry) === keyOf(event)) candidates.add(entry);
      }
    },
    onTranscript({association, text, utterance, observation}) {
      const open = association.openSilence;
      if (refineOpen && text && open && (open.utteranceId === null || open.utteranceId === utterance)) {
        if (open.prefix !== text) actions.push({kind: "refine_while_open", elapsed_ms: observation.elapsed_ms,
          silence_start_at: open.at, previous_prefix: open.prefix, prefix: text});
        open.prefix = text;
        open.utteranceId = utterance;
      }
      if (!deferInitialEmpty || !text) return;
      for (const entry of candidates) {
        if (!association.pending.includes(entry) || entry.prefix) continue;
        entry.prefix = text;
        entry.after_char = [...text].length;
        entry.alignment = "interim_prefix";
        entry.utteranceId = utterance;
        actions.push({kind: "defer_initial_empty_to_first_nonempty", elapsed_ms: observation.elapsed_ms,
          pause_key: keyOf(entry), prefix: text,
          uncertainty: "Several already-closed gaps can receive the same prefix; no timestamps identify distinct words."});
      }
    },
  };
}

function scorePlacement(test, manifestCase, run, acousticComparison) {
  const reference = manifestCase.speech_spans.flatMap(span => countingTokens(span.text));
  const nativePauses = test.observations.filter(o => o.type === "vad_event" && o.event.type === "pause")
    .map(o => o.event);
  const initial = test.observations.find(o => o.type === "asr_full_result_event"
    && o.results.some(row => row.alternatives.some(item => String(item.transcript).trim())));
  const targets = manifestCase.gaps.map((gap, index) => {
    const match = acousticComparison.matches[index];
    const expectedBoundary = manifestCase.speech_spans
      .filter(span => span.end_sample_exclusive <= gap.start_sample)
      .reduce((sum, span) => sum + countingTokens(span.text).length, 0);
    if (match.missed) return {gap_index: index, expected_boundary: expectedBoundary, status: "vad_missed"};
    const measured = nativePauses[match.pause_index], key = keyOf(measured);
    const appearances = [];
    for (const step of run.steps) {
      for (const pause of step.displayed_pauses.filter(p => keyOf(p) === key)) {
        const placement = wordBoundary(step.final_prefix_text, step.text, pause.afterChar, reference);
        const leftContextReady = placement.status === "scorable" && placement.represented_words >= expectedBoundary;
        appearances.push({
          elapsed_ms: step.elapsed_ms, text: step.text, final_prefix_text: step.final_prefix_text,
          is_final: step.is_final, after_char: pause.afterChar, displayed_duration_ms: pause.durationMs,
          ...placement, left_context_ready: leftContextReady,
          boundary_error_words: leftContextReady ? placement.boundary - expectedBoundary : null,
          correct_boundary: leftContextReady ? placement.boundary === expectedBoundary : null,
        });
      }
    }
    const finals = appearances.filter(item => item.is_final);
    const scorableFinals = finals.filter(item => item.left_context_ready);
    return {
      gap_index: index, after_source: gap.after, before_source: gap.before,
      expected_boundary: expectedBoundary, exact_inserted_ms: gap.duration_ms,
      measured_duration_ms: measured.duration_ms, pause_key: key,
      first_asr_receipt_after_gap_end_ms: initial ? initial.wall_ms - Date.parse(measured.end_at) : null,
      duration_retained: finals.length > 0 && finals.every(item => item.displayed_duration_ms === measured.duration_ms),
      final_occurrences: finals.length,
      final_placement_status: !finals.length ? "not_retained"
        : finals.length > 1 ? "duplicated"
        : !scorableFinals.length ? "unscorable"
        : scorableFinals[0].correct_boundary ? "correct_boundary" : "wrong_boundary",
      first_visible: appearances[0] || null,
      final_positions: finals.map(item => ({...item})),
      appearances,
    };
  });
  const statuses = status => targets.filter(item => item.final_placement_status === status).length;
  const finals = targets.flatMap(item => item.final_positions || []).filter(item => item.left_context_ready);
  const revisionPlacements = targets.flatMap(item => item.appearances || []).filter(item => item.left_context_ready);
  return {
    case_id: test.case_id, known_counting_reference: reference, targets,
    metrics: {
      known_insertions: targets.length,
      durations_retained_unchanged: targets.filter(item => item.duration_retained).length,
      final_correct_boundaries: statuses("correct_boundary"),
      final_wrong_boundaries: statuses("wrong_boundary"),
      final_unscorable_boundaries: statuses("unscorable"),
      final_duplicate_intervals: statuses("duplicated"),
      final_missing_intervals: statuses("not_retained"),
      acoustic_misses: targets.filter(item => item.status === "vad_missed").length,
      final_mean_absolute_boundary_error_words: finals.length
        ? finals.reduce((sum, item) => sum + Math.abs(item.boundary_error_words), 0) / finals.length : null,
      scorable_revision_appearances: revisionPlacements.length,
      correct_revision_appearances: revisionPlacements.filter(item => item.correct_boundary).length,
    },
  };
}

function runPositionExperiment(manifest, native, baselineR, baselineT, currentR, currentT) {
  const acoustic = vad.compareNative(manifest, native);
  const strategies = [
    {id: "baseline_frozen", R: baselineR, T: baselineT, settings: null},
    {id: "current_frozen", R: currentR, T: currentT, settings: null},
    {id: "current_open_refinement", R: currentR, T: currentT, settings: {refineOpen: true}},
    {id: "current_deferred_initial_empty", R: currentR, T: currentT, settings: {deferInitialEmpty: true}},
    {id: "current_open_and_deferred_initial", R: currentR, T: currentT,
      settings: {refineOpen: true, deferInitialEmpty: true}},
  ];
  return {
    scope: "Offline replay of actual recorded native events. Known inserted source-utterance boundaries score text placement independently from measured-duration retention.",
    policy_definitions: {
      frozen: "Actual production PauseAssociation prefix at silenceStarted, mapper/retention as supplied. Current uses recognitionUpdates for complete draft aggregation.",
      open_refinement: "Only while openSilence exists, replace its prefix with the newest nonempty ASR draft for the same or unassigned utterance. Closed gaps stay frozen.",
      deferred_initial_empty: "For closed empty-prefix gaps recorded before any accepted final, freeze the first subsequent nonempty ASR prefix. Later true between-final boundaries remain unchanged.",
      combined: "Apply both open refinement and the limited deferred-initial rule.",
    },
    caveats: [
      "Correct measured duration does NOT establish correct word placement.",
      "Counting word/digit equivalence is used only to score these known counting fixtures, not to position any gap or infer word times.",
      "Numeric strings such as123 are scored as the known single-digit counting sequence; this is not a general-purpose tokenizer.",
      "Reference positions come from separately recorded source text on either side of exact inserted zeros, not ASR receipt times.",
      "Revision positions are scored only when the left reference context is represented; lexical mismatches remain unscorable.",
      "Native extra short pauses are not placement ground truth and are not scored here.",
      "Deferred empty prefixes are heuristic: multiple completed gaps may share one late prefix and collapse at one boundary.",
      "No strategy changes VAD frames, measured duration, production code, or production defaults. No cloud recognition rerun.",
    ],
    experiments: strategies.map(strategy => {
      const cases = native.cases.map(test => {
        const manifestCase = manifest.cases.find(item => item.id === test.case_id);
        const hooks = strategy.settings ? makeStrategy(strategy.settings) : null;
        const run = replay.replayCase(test, strategy.R, strategy.T, {associationStrategy: hooks});
        return {
          ...scorePlacement(test, manifestCase, run, acoustic.find(item => item.id === test.case_id)),
          recognition_updates_used: run.recognition_updates_used,
          actions: hooks?.actions || [],
        };
      });
      const metrics = {};
      for (const key of Object.keys(cases[0].metrics).filter(key => key !== "final_mean_absolute_boundary_error_words")) {
        metrics[key] = cases.reduce((sum, test) => sum + test.metrics[key], 0);
      }
      return {strategy: strategy.id, metrics, cases};
    }),
  };
}

module.exports = {countingTokens, wordBoundary, makeStrategy, scorePlacement, runPositionExperiment};
if (require.main === module) {
  const [manifest, native, baselineRuntime, baselineTranscript, currentRuntime, currentTranscript, output] = process.argv.slice(2);
  if (!output) throw new Error("Usage: node tests\\captioner_counting_position.js MANIFEST NATIVE BASE_R BASE_T CURRENT_R CURRENT_T OUTPUT");
  const read = file => JSON.parse(fs.readFileSync(file, "utf8"));
  const report = runPositionExperiment(read(manifest), read(native),
    require(path.resolve(baselineRuntime)), require(path.resolve(baselineTranscript)),
    require(path.resolve(currentRuntime)), require(path.resolve(currentTranscript)));
  report.inputs = Object.fromEntries(Object.entries({manifest, native, baselineRuntime, baselineTranscript,
    currentRuntime, currentTranscript}).map(([name, file]) => [name, {path: path.resolve(file),
      sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex")}]));
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
  for (const experiment of report.experiments) console.log(experiment.strategy, JSON.stringify(experiment.metrics));
}
