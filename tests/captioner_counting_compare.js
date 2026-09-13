"use strict";

// node tests\captioner_counting_compare.js MANIFEST RUNTIME OUTPUT [CONFIG_JSON]
// node tests\captioner_counting_compare.js --config-template
// node tests\captioner_counting_compare.js --schema
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const runner = require("./captioner_counting_vad.js");

const DEFAULT_CONFIG = {
  schema_version: 1,
  endpoint_tolerance_ms: 60,
  gains: [1, 0.25],
  variants: [
    {id: "production_defaults", description: "Unmodified production constructor defaults", vad_options: {}},
    {id: "sensitive_floor", description: "Lower RMS/noise floor and margin",
      vad_options: {rmsFloor: 0.005, initialNoiseFloor: 0.0015, thresholdMargin: 0.003}},
    {id: "conservative_floor", description: "Higher RMS floor and margin",
      vad_options: {rmsFloor: 0.012, thresholdMargin: 0.008}},
    {id: "lower_noise_margin", description: "Less adaptive noise margin; unchanged absolute floor",
      vad_options: {thresholdMargin: 0.003, thresholdMultiplier: 1.6}},
    {id: "fast_hysteresis", description: "One frame onset and silence confirmation",
      vad_options: {onsetFrames: 1, silenceFrames: 1}},
    {id: "stable_hysteresis", description: "Three frame onset and silence confirmation",
      vad_options: {onsetFrames: 3, silenceFrames: 3}},
    {id: "10ms_fast", description: "10ms frames; two-frame confirmation; report >=10ms pauses",
      vad_options: {frameIntervalMs: 10, minPauseMs: 10}},
    {id: "10ms_same_confirmation", description: "10ms frames; 40ms confirmation like 20ms production",
      vad_options: {frameIntervalMs: 10, onsetFrames: 4, silenceFrames: 4}},
  ],
};

const OPTION_NAMES = [
  "frameIntervalMs", "minPauseMs", "onsetFrames", "silenceFrames", "initialNoiseFloor",
  "rmsFloor", "thresholdMargin", "thresholdMultiplier", "noiseAlpha", "maxPauseMs",
];
const SCHEMA = {
  schema_version: 1,
  invocation: "node tests\\captioner_counting_compare.js MANIFEST RUNTIME OUTPUT [CONFIG_JSON]",
  manifest: {
    required: ["cases"],
    cases: [{id: "unique-case-id", wav: "relative\\mono-pcm16.wav",
      gaps: [{start_sample: "integer inclusive", end_sample_exclusive: "integer exclusive",
        sample_count: "optional integer consistency check", duration_ms: "optional number consistency check",
        quiet_proxy: "optional {start_ms,end_ms}; independent acoustic-quiet envelope, NOT a word timestamp"}]}],
    notes: [
      "Sample rate is read from each WAV; arbitrary PCM16 mono rates work. Optional manifest.sample_rate is checked.",
      "Sample indices are authoritative. Alternatively provide {start_ms,end_ms,duration_ms} that map exactly to integer samples.",
      "Every declared inserted gap must contain only zero PCM samples. Existing natural word silence is not automatically labelled ground truth.",
      "Other provenance, transcript and licensing metadata may remain in the manifest and are not interpreted.",
      "Recorded controls may set source_kind:'recorded_human', silence_ground_truth:'unavailable', gaps_kind:'unlabeled_not_empty_ground_truth'; these labels are preserved. Empty gaps is not a zero-pause assertion.",
      "No SRT, ASR receipt time, or transcript word is treated as exact acoustic timing.",
    ],
  },
  config: {
    schema_version: 1,
    endpoint_tolerance_ms: "number >=0, default60; same tolerance for all variants",
    gains: "array of 1..8 unique gain multipliers in [0,4]; clipping reported",
    variants: "array of 1..16 {id,description?,vad_options}; permitted keys: " + OPTION_NAMES.join(","),
    default: DEFAULT_CONFIG,
  },
  interpretation: {
    matched: "A detected pause overlaps an inserted interval; overlap alone does NOT imply accurate boundaries.",
    merged: "One detected pause overlaps/matches more than one inserted interval.",
    qualified_match: "Unique, unmerged matched interval whose two endpoints are within the fixed tolerance.",
    extra: "Detected pause not matched to a declared inserted gap. May be real intra-speech quiet; not necessarily a false positive.",
    unscored_control_detections: "All detections in cases without inserted-gap labels; cannot be scored as acoustic false positives.",
    quiet_proxy: "Optional fixed-reference acoustic-quiet bounds; kept unchanged across gains for fair comparison.",
    timing_error: "Signed/absolute errors are against both exact insertions and optional quiet proxy; no word timestamps.",
  },
};

function validateConfig(config) {
  if (config.schema_version !== 1) throw new Error("Configuration schema_version must be 1");
  if (!Array.isArray(config.variants) || !config.variants.length || config.variants.length > 16) {
    throw new Error("Specify 1..16 bounded variants");
  }
  if (!Array.isArray(config.gains) || !config.gains.length || config.gains.length > 8
      || config.gains.some(g => !Number.isFinite(g) || g < 0 || g > 4)
      || new Set(config.gains).size !== config.gains.length) throw new Error("Specify 1..8 unique gains in [0,4]");
  if (!Number.isFinite(config.endpoint_tolerance_ms) || config.endpoint_tolerance_ms < 0) {
    throw new Error("endpoint_tolerance_ms must be a finite nonnegative number");
  }
  const ids = new Set();
  for (const variant of config.variants) {
    if (typeof variant.id !== "string" || !variant.id || ids.has(variant.id)) throw new Error("Unique variant IDs required");
    ids.add(variant.id);
    if (!variant.vad_options || typeof variant.vad_options !== "object" || Array.isArray(variant.vad_options)) {
      throw new Error("Every variant must contain a vad_options object");
    }
    for (const [name, value] of Object.entries(variant.vad_options)) {
      if (!OPTION_NAMES.includes(name) || !Number.isFinite(value) || value < 0) throw new Error(`Invalid VAD option ${name}`);
      if (["onsetFrames", "silenceFrames"].includes(name) && (!Number.isInteger(value) || value < 1)) {
        throw new Error(`${name} must be a positive integer`);
      }
      if (name === "frameIntervalMs" && ![10, 20].includes(value)) throw new Error("Bounded comparison permits 10 or 20ms frames");
      if (name === "noiseAlpha" && value > 1) throw new Error("noiseAlpha must be <=1");
    }
  }
}

function normalizeGaps(gaps, pcm, rate) {
  if (!Array.isArray(gaps)) throw new Error("Case gaps must be an array");
  let previousEnd = -1;
  return gaps.map(gap => {
    const start = gap.start_sample === undefined ? gap.start_ms * rate / 1000 : gap.start_sample;
    const end = gap.end_sample_exclusive === undefined ? gap.end_ms * rate / 1000 : gap.end_sample_exclusive;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start
        || end * 2 > pcm.length || start < previousEnd) throw new Error("Invalid or overlapping exact gap sample bounds");
    previousEnd = end;
    const duration = (end - start) * 1000 / rate;
    if (gap.sample_count !== undefined && gap.sample_count !== end - start) throw new Error("Gap sample count mismatch");
    if (gap.duration_ms !== undefined && Math.abs(gap.duration_ms - duration) > 1e-7) throw new Error("Gap duration/sample mismatch");
    for (let i = start; i < end; i++) {
      if (pcm.readInt16LE(i * 2) !== 0) throw new Error("Declared exact inserted silence contains nonzero PCM");
    }
    const normalized = {...gap, start_sample: start, end_sample_exclusive: end,
      start_ms: start * 1000 / rate, end_ms: end * 1000 / rate, duration_ms: duration};
    if (gap.quiet_proxy && (!Number.isFinite(gap.quiet_proxy.start_ms)
        || !Number.isFinite(gap.quiet_proxy.end_ms)
        || gap.quiet_proxy.start_ms > normalized.start_ms || gap.quiet_proxy.end_ms < normalized.end_ms)) {
      throw new Error("Quiet proxy must be a finite envelope containing the exact inserted gap");
    }
    return normalized;
  });
}

function mean(values) {
  return values.length ? values.reduce((sum, x) => sum + x, 0) / values.length : null;
}

function score(comparisons) {
  const matches = comparisons.flatMap(c => c.matches.filter(m => !m.missed));
  const unmergedQualified = comparisons.flatMap(c => {
    const merged = new Set(c.merged.map(m => m.pause_index));
    return c.matches.filter(m => !m.missed && m.within_tolerance && !merged.has(m.pause_index));
  });
  const total = key => comparisons.reduce((sum, c) => sum + c[key], 0);
  const signed = matches.map(m => m.duration_error_ms);
  const endpoint = matches.flatMap(m => [m.quiet_proxy_start_error_ms, m.quiet_proxy_end_error_ms]);
  const errorStats = values => ({
    signed_mean_ms: mean(values), mean_absolute_ms: mean(values.map(Math.abs)),
    max_absolute_ms: values.length ? Math.max(...values.map(Math.abs)) : null,
  });
  return {
    expected_gaps: total("expected_count"), matched_gaps: total("matched_count"),
    missed_gaps: total("missed_count"), qualified_unmerged_matches: unmergedQualified.length,
    merged_detections: comparisons.reduce((sum, c) => sum + c.merged.length, 0),
    merged_ground_truth_gaps: comparisons.reduce((sum, c) => sum + c.merged.reduce((n, m) => n + m.gap_indices.length, 0), 0),
    extra_detections: comparisons.reduce((sum, c) => sum + c.extra_pauses.length, 0),
    extra_detections_in_insertion_cases: comparisons.filter(c => c.expected_count > 0)
      .reduce((sum, c) => sum + c.extra_pauses.length, 0),
    detected_pauses: total("detected_pause_count"), out_of_tolerance_gaps: total("out_of_tolerance_count"),
    unscored_control_detections: comparisons.filter(c => c.expected_count === 0)
      .reduce((sum, c) => sum + c.detected_pause_count, 0),
    exact_insertion_case_count: comparisons.filter(c => c.expected_count > 0).length,
    unlabeled_control_case_count: comparisons.filter(c => c.silence_ground_truth === "unavailable"
      || c.gaps_kind === "unlabeled_not_empty_ground_truth").length,
    insertion_duration_error: errorStats(signed), reference_endpoint_error: errorStats(endpoint),
    error_population_note: "Timing statistics include matched gaps only; inspect misses/merges alongside errors. Extras are not labelled false positives.",
  };
}

function runComparison(manifestFile, runtimeFile, config = DEFAULT_CONFIG) {
  validateConfig(config);
  const R = require(path.resolve(runtimeFile));
  const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8").replace(/^\uFEFF/, ""));
  if (!Array.isArray(manifest.cases) || !manifest.cases.length) throw new Error("Manifest requires nonempty cases");
  const ids = new Set();
  const cases = manifest.cases.map(test => {
    if (typeof test.id !== "string" || ids.has(test.id)) throw new Error("Unique case IDs required");
    ids.add(test.id);
    const file = path.resolve(path.dirname(manifestFile), test.wav);
    const {rate, pcm} = runner.readPcm16(file);
    if (manifest.sample_rate !== undefined && manifest.sample_rate !== rate) throw new Error("Manifest and WAV sample rates differ");
    const gaps = normalizeGaps(test.gaps, pcm, rate);
    return {id: test.id, file, rate, pcm, gaps,
      source_kind: test.source_kind || manifest.source_kind || "unspecified",
      gaps_kind: test.gaps_kind || (gaps.length ? "exact_inserted_gaps" : "no_insertions_control"),
      silence_ground_truth: test.silence_ground_truth || (gaps.length ? "insertions_only" : "unavailable"),
      expected_text: test.expected_text || test.reference_text || null,
      sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex")};
  });
  const experiments = [];
  for (const variant of config.variants) {
    for (const gain of config.gains) {
      const measured = cases.map(test => {
        const detection = runner.detect(R, test.pcm, test.rate, 128, {
          vadOptions: variant.vad_options, gain, retainFrames: false,
        });
        const durations = detection.pauses.map(p => p.duration_ms).sort((a, b) => a - b);
        return {id: test.id, frame_count: detection.frame_count,
          clipped_samples: detection.clipped_samples, pauses: detection.pauses,
          pause_distribution: {
            completed_count: durations.length,
            shorter_than_100ms: durations.filter(ms => ms < 100).length,
            shorter_than_200ms: durations.filter(ms => ms < 200).length,
            at_least_200ms: durations.filter(ms => ms >= 200).length,
            median_ms: durations.length ? (durations[Math.floor((durations.length - 1) / 2)]
              + durations[Math.floor(durations.length / 2)]) / 2 : null,
            total_completed_pause_ms: durations.reduce((sum, ms) => sum + ms, 0),
            note: "Completed RMS-quiet intervals only; not annotated speech/silence truth and not full recording quiet time.",
          },
          final_status: detection.final_status,
          comparison: {...runner.compareGaps(test.gaps, detection.pauses, {
            endpointToleranceMs: config.endpoint_tolerance_ms,
          }), gaps_kind: test.gaps_kind, silence_ground_truth: test.silence_ground_truth}};
      });
      const vad = new R.BrowserRmsVad(variant.vad_options);
      experiments.push({variant_id: variant.id, description: variant.description,
        gain, gain_db: gain > 0 ? 20 * Math.log10(gain) : null,
        resolved_vad_options: Object.fromEntries(OPTION_NAMES.map(key => [key, vad[key]])),
        score: score(measured.map(m => m.comparison)), cases: measured});
    }
  }
  const hash = file => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  return {schema_version: 1, runtime_file: path.resolve(runtimeFile), runtime_sha256: hash(runtimeFile),
    runner_sha256: hash(__filename), manifest_file: path.resolve(manifestFile), manifest_sha256: hash(manifestFile),
    config, assumptions: SCHEMA.interpretation,
    caveats: [
      "Parameter ablation of the production RMS detector, not a comparison with independent ML VAD algorithms.",
      "Exact inserted zero spans are certain; reference quiet proxies are optional fixed estimates, not words.",
      "The same 60ms default endpoint tolerance applies to both 10ms and 20ms variants; raw errors are retained.",
      "Existing pauses inside speech/control recordings are extra detections, not necessarily detector errors.",
      "Gain applies only inside the offline runner; no audio output, no live captions, and no production-default changes.",
      "TTS-only outcomes cannot establish the best setting for ordinary human speech, noise or distant microphones.",
    ],
    fixtures: cases.map(({pcm, ...test}) => ({...test, sample_count: pcm.length / 2})),
    experiments, by_gain: config.gains.map(gain => ({gain,
      variants: experiments.filter(e => e.gain === gain).map(e => ({variant_id: e.variant_id, ...e.score}))}))};
}

module.exports = {DEFAULT_CONFIG, SCHEMA, validateConfig, normalizeGaps, score, runComparison};
if (require.main === module) {
  if (process.argv[2] === "--config-template") {
    process.stdout.write(JSON.stringify(DEFAULT_CONFIG, null, 2) + "\n");
  } else if (process.argv[2] === "--schema") {
    process.stdout.write(JSON.stringify(SCHEMA, null, 2) + "\n");
  } else {
    if (!process.argv[4]) throw new Error(SCHEMA.invocation);
    const config = process.argv[5]
      ? JSON.parse(fs.readFileSync(process.argv[5], "utf8").replace(/^\uFEFF/, "")) : DEFAULT_CONFIG;
    const result = runComparison(process.argv[2], process.argv[3], config);
    fs.writeFileSync(process.argv[4], JSON.stringify(result, null, 2) + "\n");
    for (const experiment of result.experiments) {
      const s = experiment.score;
      console.log(experiment.variant_id, `gain=${experiment.gain}`,
        s.expected_gaps ? `matched=${s.matched_gaps}/${s.expected_gaps}` : "inserted_labels=unavailable",
        `qualified=${s.qualified_unmerged_matches}`, `missed=${s.missed_gaps}`, `merged=${s.merged_detections}`,
        `extra_in_insertion_cases=${s.extra_detections_in_insertion_cases}`,
        `unscored_control_detections=${s.unscored_control_detections}`,
        `insertion_mae=${s.insertion_duration_error.mean_absolute_ms}`,
        `reference_endpoint_mae=${s.reference_endpoint_error.mean_absolute_ms}`);
    }
  }
}
