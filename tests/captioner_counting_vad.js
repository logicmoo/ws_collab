"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

function readPcm16(file) {
  const data = fs.readFileSync(file);
  if (data.toString("ascii", 0, 4) !== "RIFF" || data.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`Not a WAV: ${file}`);
  }
  let format, pcm;
  for (let p = 12; p + 8 <= data.length;) {
    const size = data.readUInt32LE(p + 4);
    const body = data.subarray(p + 8, p + 8 + size);
    if (data.toString("ascii", p, p + 4) === "fmt ") {
      format = {encoding: body.readUInt16LE(0), channels: body.readUInt16LE(2),
        rate: body.readUInt32LE(4), bits: body.readUInt16LE(14)};
    }
    if (data.toString("ascii", p, p + 4) === "data") pcm = body;
    p += 8 + size + (size % 2);
  }
  if (!format || !pcm || format.encoding !== 1 || format.channels !== 1 || format.bits !== 16
      || !Number.isInteger(format.rate) || format.rate <= 0 || pcm.length % 2 !== 0) {
    throw new Error("Expected mono PCM16");
  }
  return {rate: format.rate, pcm};
}

function detect(R, pcm, rate, blockSamples = 128, {vadOptions = {}, gain = 1, retainFrames = true} = {}) {
  const vad = new R.BrowserRmsVad(vadOptions);
  const epoch = "synthetic-counting";
  const wallOrigin = Date.parse("2026-09-11T00:00:00.000Z");
  vad.start(epoch);
  const frames = [], events = [];
  let frameCount = 0, clippedSamples = 0;
  const accumulator = new R.RmsFrameAccumulator(rate, vad.frameIntervalMs, frame => {
    frameCount += 1;
    if (retainFrames) frames.push(frame);
    events.push(...vad.processFrame(frame.rms, frame.audio_time_ms,
      wallOrigin + frame.audio_time_ms, epoch, frame.audio_time_ms));
  });
  for (let start = 0; start < pcm.length / 2; start += blockSamples) {
    const count = Math.min(blockSamples, pcm.length / 2 - start);
    const floats = Float32Array.from({length: count}, (_, i) => {
      const scaled = pcm.readInt16LE((start + i) * 2) / 32768 * gain;
      if (Math.abs(scaled) > 1) clippedSamples += 1;
      return Math.max(-1, Math.min(1, scaled));
    });
    accumulator.push(floats, start);
  }
  return {frames, events, frame_count: frameCount, frame_interval_ms: vad.frameIntervalMs,
    gain, clipped_samples: clippedSamples, final_status: vad.status(), pauses: events.filter(e => e.type === "pause")
    .map(e => ({...e, start_ms: Date.parse(e.start_at) - wallOrigin,
      end_ms: Date.parse(e.end_at) - wallOrigin}))};
}

function compareGaps(gaps, pauses, {endpointToleranceMs = 60} = {}) {
  const matches = gaps.map((gap, gapIndex) => {
    const candidates = pauses.map((pause, index) => ({pause, index}))
      .filter(({pause}) => pause.start_ms < gap.end_ms && pause.end_ms > gap.start_ms)
      .sort((a, b) => {
        const overlap = p => Math.min(p.end_ms, gap.end_ms) - Math.max(p.start_ms, gap.start_ms);
        return overlap(b.pause) - overlap(a.pause);
      });
    const best = candidates[0];
    if (!best) return {gap_index: gapIndex, expected_ms: gap.duration_ms, missed: true};
    const startError = best.pause.start_ms - gap.start_ms;
    const endError = best.pause.end_ms - gap.end_ms;
    const quiet = gap.quiet_proxy || {start_ms: gap.start_ms, end_ms: gap.end_ms};
    // Two VAD frames plus the independent 10ms quiet-proxy bin, rounded up.
    const tolerance = endpointToleranceMs;
    return {gap_index: gapIndex, expected_ms: gap.duration_ms, missed: false,
      pause_index: best.index, detected_ms: best.pause.duration_ms,
      duration_error_ms: best.pause.duration_ms - gap.duration_ms,
      inserted_start_error_ms: startError, inserted_end_error_ms: endError,
      quiet_proxy_start_error_ms: best.pause.start_ms - quiet.start_ms,
      quiet_proxy_end_error_ms: best.pause.end_ms - quiet.end_ms,
      endpoint_tolerance_ms: tolerance,
      within_tolerance: Math.abs(best.pause.start_ms - quiet.start_ms) <= tolerance
        && Math.abs(best.pause.end_ms - quiet.end_ms) <= tolerance};
  });
  const used = new Set(matches.filter(m => !m.missed).map(m => m.pause_index));
  const merged = [...used].map(index => ({
    pause_index: index,
    gap_indices: matches.filter(m => !m.missed && m.pause_index === index).map(m => m.gap_index),
  })).filter(m => m.gap_indices.length > 1);
  return {matches, expected_count: gaps.length, detected_pause_count: pauses.length,
    matched_count: matches.filter(m => !m.missed).length,
    missed_count: matches.filter(m => m.missed).length, merged,
    out_of_tolerance_count: matches.filter(m => !m.missed && !m.within_tolerance).length,
    extra_pauses: pauses.map((p, index) => ({...p, pause_index: index})).filter(p => !used.has(p.pause_index))};
}

function runManifest(manifestFile, runtimeFile) {
  const R = require(path.resolve(runtimeFile));
  const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8").replace(/^\uFEFF/, ""));
  const base = path.dirname(manifestFile);
  return {runtime_file: runtimeFile,
    runtime_sha256: crypto.createHash("sha256").update(fs.readFileSync(runtimeFile)).digest("hex"),
    frame_interval_ms: 20, clock: "PCM sample clock; frame timestamps mark frame ends",
    tolerance_policy: "60ms per quiet-proxy endpoint (20ms VAD frames, hysteresis and 10ms proxy bins); inserted zeros remain exact",
    cases: manifest.cases.map(test => {
      const {rate, pcm} = readPcm16(path.join(base, test.wav));
      const detection = detect(R, pcm, rate);
      return {id: test.id, ...detection, comparison: compareGaps(test.gaps, detection.pauses)};
    })};
}

function wordErrorRate(expected, recognized) {
  const tokens = text => String(text).replace(/\u2019/g, "'").toLowerCase()
    .match(/[\p{L}\p{N}]+(?:'[\p{L}\p{N}]+)*/gu) || [];
  const reference = tokens(expected), hypothesis = tokens(recognized);
  const matrix = Array.from({length: reference.length + 1}, () => Array(hypothesis.length + 1).fill(0));
  for (let i = 0; i <= reference.length; i++) matrix[i][0] = i;
  for (let j = 0; j <= hypothesis.length; j++) matrix[0][j] = j;
  for (let i = 1; i <= reference.length; i++) {
    for (let j = 1; j <= hypothesis.length; j++) {
      matrix[i][j] = Math.min(matrix[i - 1][j] + 1, matrix[i][j - 1] + 1,
        matrix[i - 1][j - 1] + (reference[i - 1] === hypothesis[j - 1] ? 0 : 1));
    }
  }
  let i = reference.length, j = hypothesis.length, substitutions = 0, deletions = 0, insertions = 0;
  while (i || j) {
    const same = i && j && reference[i - 1] === hypothesis[j - 1];
    if (i && j && matrix[i][j] === matrix[i - 1][j - 1] + (same ? 0 : 1)) {
      if (!same) substitutions += 1;
      i -= 1; j -= 1;
    } else if (i && matrix[i][j] === matrix[i - 1][j] + 1) {
      deletions += 1; i -= 1;
    } else {
      insertions += 1; j -= 1;
    }
  }
  const errors = substitutions + deletions + insertions;
  return {expected_text: expected, recognized_text: recognized,
    reference_words: reference.length, hypothesis_words: hypothesis.length,
    substitutions, deletions, insertions, errors,
    wer: reference.length ? errors / reference.length : null,
    normalization: "Lowercase Unicode letter/number words, retain internal apostrophes; punctuation ignored. No digit-to-word or semantic normalization.",
    timing_note: "Transcript-level edit distance only; no word timestamps inferred."};
}

function compareNative(manifest, report) {
  return report.cases.map(test => {
    const expected = manifest.cases.find(c => c.id === test.case_id);
    if (!test.playback) return {id: test.case_id, unavailable: true};
    const origin = test.playback.source_start_performance_ms_estimate
      + test.playback.wall_minus_performance_ms;
    const pauses = test.observations.filter(o => o.type === "vad_event" && o.event.type === "pause")
      .map(({event}) => ({...event, start_ms: Date.parse(event.start_at) - origin,
        end_ms: Date.parse(event.end_at) - origin}));
    const revisions = test.observations.filter(o => o.type === "asr_revision");
    const expectedText = expected.expected_text || expected.reference_text
      || (expected.speech_spans || []).map(span => span.text || "").join(" ").trim();
    const recognizedText = revisions.filter(revision => revision.is_final).map(revision => revision.text).join(" ");
    const observed = new Map();
    for (const revision of revisions) {
      for (const pause of revision.metadata.pauses) {
        const key = `${pause.start_at}|${pause.end_at}`;
        if (!observed.has(key)) observed.set(key, []);
        observed.get(key).push({text: revision.text, final: revision.is_final,
          duration_ms: pause.duration_ms, after_char: pause.after_char, alignment: pause.alignment});
      }
    }
    return {id: test.case_id,
      source_kind: expected.source_kind || manifest.source_kind || test.source_kind || "unspecified",
      gaps_kind: expected.gaps_kind || (expected.gaps.length ? "exact_inserted_gaps" : "no_insertions_control"),
      silence_ground_truth: expected.silence_ground_truth || (expected.gaps.length ? "insertions_only" : "unavailable"),
      word_error_rate: expectedText ? wordErrorRate(expectedText, recognizedText) : null,
      timing_note: "Native endpoints use producer output-clock correlation; MediaStream buffering/resampling may shift them. Durations are directly measured, not ASR word timestamps.",
      ...compareGaps(expected.gaps, pauses),
      pause_preservation: pauses.map(pause => {
        const observations = observed.get(`${pause.start_at}|${pause.end_at}`) || [];
        return {start_at: pause.start_at, end_at: pause.end_at, measured_ms: pause.duration_ms,
          observations, never_attached_to_transcript: !observations.length,
          reached_final: observations.some(o => o.final),
          duration_changed: observations.some(o => o.duration_ms !== pause.duration_ms),
          distinct_text_positions: [...new Set(observations.map(o => o.after_char))]};
      })};
  });
}

module.exports = {readPcm16, detect, compareGaps, runManifest, compareNative, wordErrorRate};
if (require.main === module) {
  const result = runManifest(process.argv[2], process.argv[3]);
  if (process.argv[4]) fs.writeFileSync(process.argv[4], JSON.stringify(result, null, 2));
  else process.stdout.write(JSON.stringify(result));
}
