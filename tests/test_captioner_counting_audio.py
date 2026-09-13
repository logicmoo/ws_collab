"""Production VAD against exact PCM gaps; optional preserved Windows TTS recordings."""
import json
from pathlib import Path

import pytest

from test_captioner_silence_display import run_node


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "collab_state" / "captioner_counting_tests"
RUNNER = ROOT / "tests" / "captioner_counting_vad.js"


@pytest.mark.parametrize("gaps", [[2000] * 4, [80, 240, 1000, 2000, 4000], [4000], []])
def test_exact_counting_fixture_through_production_rms_accumulator(gaps: list[int]) -> None:
    run_node("""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
const test = require(%s);
const durations = %s;
const samples = Array(8000).fill(0), gaps = [];
for (let i = 0; i <= durations.length; i++) {
  for (let j = 0; j < 6400; j++) samples.push(Math.round(12000 * Math.sin(2 * Math.PI * 180 * j / 16000)));
  if (i < durations.length) {
    const start = samples.length;
    for (let j = 0; j < durations[i] * 16; j++) samples.push(0);
    gaps.push({duration_ms: durations[i], start_ms: start / 16, end_ms: samples.length / 16});
  }
}
samples.push(...Array(16000).fill(0));
const pcm = Buffer.alloc(samples.length * 2);
samples.forEach((sample, i) => pcm.writeInt16LE(sample, i * 2));
const results = [128, 317, 960].map(block => test.detect(R, pcm, 16000, block));
assert.deepEqual(results[0].pauses, results[1].pauses);
assert.deepEqual(results[0].pauses, results[2].pauses);
const comparison = test.compareGaps(gaps, results[0].pauses);
assert.equal(comparison.missed_count, 0);
assert.equal(comparison.merged.length, 0);
assert.equal(comparison.extra_pauses.length, 0);
assert.equal(comparison.out_of_tolerance_count, 0);
assert.deepEqual(comparison.matches.map(m => m.detected_ms), gaps.map(g => g.duration_ms));
""" % (json.dumps(str(RUNNER)), json.dumps(gaps)))


def test_recorded_tts_has_exact_zeros_and_production_detects_all_insertions() -> None:
    manifest_file = ARTIFACTS / "manifest.json"
    if not manifest_file.exists():
        pytest.skip("Run tests\\captioner_counting_harness.py generate for real Windows TTS recordings")
    report = json.loads(run_node("""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const test = require(%s);
const manifestPath = %s;
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
for (const c of manifest.cases) {
  const file = path.join(path.dirname(manifestPath), c.wav);
  const {rate, pcm} = test.readPcm16(file);
  assert.equal(crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex"), c.sha256);
  assert.equal(rate, manifest.sample_rate);
  assert.equal(pcm.length / 2, c.sample_count);
  for (const span of c.speech_spans) {
    const source = manifest.sources[span.source_id];
    const original = test.readPcm16(path.join(path.dirname(manifestPath), source.wav)).pcm;
    assert.deepEqual(pcm.subarray(span.start_sample * 2, span.end_sample_exclusive * 2),
      original.subarray(span.source_crop_start_sample * 2, span.source_crop_end_sample_exclusive * 2));
  }
  for (const gap of c.gaps) {
    assert.equal(gap.end_sample_exclusive - gap.start_sample, gap.sample_count);
    assert.equal(gap.sample_count * 1000 / rate, gap.duration_ms);
    for (let i = gap.start_sample; i < gap.end_sample_exclusive; i++) assert.equal(pcm.readInt16LE(i * 2), 0);
  }
}
process.stdout.write(JSON.stringify(test.runManifest(manifestPath, process.argv[2])));
""" % (json.dumps(str(RUNNER)), json.dumps(str(manifest_file)))))
    for case in report["cases"]:
        compare = case["comparison"]
        assert compare["missed_count"] == 0, case["id"]
        assert compare["merged"] == [], case["id"]
        assert compare["out_of_tolerance_count"] == 0, compare


def test_counting_comparator_reports_missed_merged_and_extra_gaps() -> None:
    run_node("""
const assert = require("node:assert/strict");
const test = require(%s);
const gaps = [{start_ms: 100, end_ms: 200, duration_ms: 100},
  {start_ms: 300, end_ms: 400, duration_ms: 100},
  {start_ms: 600, end_ms: 700, duration_ms: 100}];
const pauses = [{start_ms: 90, end_ms: 410, duration_ms: 320},
  {start_ms: 800, end_ms: 850, duration_ms: 50}];
const result = test.compareGaps(gaps, pauses);
assert.equal(result.missed_count, 1);
assert.deepEqual(result.merged, [{pause_index: 0, gap_indices: [0, 1]}]);
assert.equal(result.extra_pauses.length, 1);
assert.equal(result.out_of_tolerance_count, 2);
""" % json.dumps(str(RUNNER)))


def test_preserved_native_counting_used_muted_shared_track_and_kept_pause_durations() -> None:
    latest = ARTIFACTS / "native_latest.json"
    if not latest.exists():
        pytest.skip("Run tests\\captioner_counting_harness.py native for isolated native ASR")
    report = json.loads(Path(json.loads(latest.read_text(encoding="utf-8"))["report"]).read_text(encoding="utf-8"))
    if not report["cases"]:
        pytest.skip("Native browser unavailable: " + report.get("harness_error", "no cases"))
    assert report["cleanup"]["created_context_disposed"]
    assert report["cleanup"]["test_http_server_closed"]
    assert all(request["method"] == "GET" for request in report["requests"])
    for case in report["cases"]:
        assert case["audio_graph"]["identical_recognition_vad_track"]
        assert case["audio_graph"]["capture_output_gain"] == 0
        assert case["audio_graph"]["source_connected_only_to_media_stream_destination"]
        assert case["safeguards"]["microphone_api_called"] is False
        assert case["frames"], case["case_id"]
    for compare in report["comparisons"]:
        assert compare["missed_count"] == 0, compare
        assert compare["merged"] == [], compare
        assert not any(p["duration_changed"] for p in compare["pause_preservation"]), compare


def test_counting_variant_schema_scoring_and_silence_validation() -> None:
    run_node("""
const assert = require("node:assert/strict");
const compare = require(%s);
compare.validateConfig(compare.DEFAULT_CONFIG);
assert.equal(compare.DEFAULT_CONFIG.variants.length, 8);
assert.throws(() => compare.validateConfig({...compare.DEFAULT_CONFIG, gains: [-1]}));
assert.throws(() => compare.validateConfig({...compare.DEFAULT_CONFIG, variants: [
  {id:"bad", vad_options:{onsetFrames:0}}]}));
assert.throws(() => compare.validateConfig({...compare.DEFAULT_CONFIG, variants: [
  {id:"bad", vad_options:{nonexistent:1}}]}));
const zeros = Buffer.alloc(48000 * 2);
const gaps = compare.normalizeGaps([{start_sample:4800, end_sample_exclusive:8640}], zeros, 48000);
assert.equal(gaps[0].duration_ms, 80);
assert.equal(gaps[0].start_ms, 100);
assert.equal(gaps[0].end_ms, 180);
zeros.writeInt16LE(1, 5000 * 2);
assert.throws(() => compare.normalizeGaps(gaps, zeros, 48000), /nonzero/);
const score = compare.score([{expected_count:3,matched_count:2,missed_count:1,
  detected_pause_count:2,out_of_tolerance_count:2,merged:[{pause_index:0,gap_indices:[0,1]}],
  extra_pauses:[{}],matches:[
    {missed:false,pause_index:0,within_tolerance:false,duration_error_ms:20,
      quiet_proxy_start_error_ms:-10,quiet_proxy_end_error_ms:10},
    {missed:false,pause_index:0,within_tolerance:false,duration_error_ms:-20,
      quiet_proxy_start_error_ms:-10,quiet_proxy_end_error_ms:10},
    {missed:true},
  ]}]);
assert.equal(score.missed_gaps,1);
assert.equal(score.merged_ground_truth_gaps,2);
assert.equal(score.qualified_unmerged_matches,0);
assert.equal(score.insertion_duration_error.mean_absolute_ms,20);
assert.equal(score.reference_endpoint_error.mean_absolute_ms,10);
""" % json.dumps(str(ROOT / "tests" / "captioner_counting_compare.js")))


def test_counting_parameter_variants_and_gain_use_identical_pcm_without_changing_defaults() -> None:
    run_node("""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
const runner = require(%s);
const compare = require(%s);
const pcm = Buffer.alloc(16000 * 2 * 2);
for (let i = 0; i < 32000; i++) {
  if ((i >= 4000 && i < 8000) || (i >= 11200 && i < 15200)) {
    pcm.writeInt16LE(Math.round(12000*Math.sin(2*Math.PI*180*i/16000)),i*2);
  }
}
const original = Buffer.from(pcm);
const defaults = new R.BrowserRmsVad();
for (const variant of compare.DEFAULT_CONFIG.variants) {
  for (const gain of compare.DEFAULT_CONFIG.gains) {
    const result = runner.detect(R,pcm,16000,128,{vadOptions:variant.vad_options,gain,retainFrames:false});
    assert.equal(result.frames.length,0);
    assert.equal(result.frame_interval_ms,variant.vad_options.frameIntervalMs || 20);
    assert.equal(result.frame_count,2000/result.frame_interval_ms);
    assert.equal(result.clipped_samples,0);
    assert.equal(result.pauses.length,1,variant.id);
    assert.equal(result.pauses[0].duration_ms,200,variant.id);
  }
}
const silent = runner.detect(R,pcm,16000,128,{gain:0});
assert.equal(silent.pauses.length,0);
assert.deepEqual(pcm,original);
assert.deepEqual(new R.BrowserRmsVad(),defaults);
""" % (json.dumps(str(RUNNER)), json.dumps(str(ROOT / "tests" / "captioner_counting_compare.js"))))


def test_offline_replay_injects_mapper_and_retains_numeric_replacement_anchors() -> None:
    run_node("""
const assert = require("node:assert/strict");
const R = require(process.argv[2]), T = require(process.argv[1]);
const replay = require(%s);
const origin = Date.parse("2026-09-11T00:00:00Z");
const iso = ms => new Date(origin+ms).toISOString();
const item = (type,ms,data) => ({type,elapsed_ms:ms,wall_ms:origin+ms,...data});
const asr = (ms,text,final=false) => item("asr_full_result_event",ms,{
  result_index:0,results:[{index:0,is_final:final,alternatives:[{transcript:text,confidence:0.9}]}],
});
const end = ms => item("vad_event",ms,{event:{type:"speech_end",at:iso(ms)}});
const pause = (start,end) => item("vad_event",end,{event:{type:"pause",
  start_at:iso(start),end_at:iso(end),duration_ms:end-start,source:"browser_rms_vad"}});
const fixture = {case_id:"authored_numeric_compaction_not_native_recording",observations:[
  asr(500,"1"),end(1000),pause(1000,3000),asr(3300,"1 2"),
  end(3500),pause(3500,5500),asr(5800,"1 2 3"),asr(6000,"123",true),
]};
const original = JSON.stringify(fixture);
const result = replay.replayCase(fixture,R,T);
assert.equal(result.final_pause_loss_count,0);
assert.equal(result.duration_change_count,0);
assert.deepEqual(result.final_rows[0].pauses.map(p => p.durationMs),[2000,2000]);
if (typeof T.silencePosition === "function") {
  assert.equal(result.mapper_injected,true);
  assert.deepEqual(result.final_rows[0].pauses.map(p => p.afterChar),[1,2]);
}
assert.equal(JSON.stringify(fixture),original,"replay must not mutate recorded source data");
""" % json.dumps(str(ROOT / "tests" / "captioner_counting_replay.js")))


def test_recorded_native_sequences_replay_offline_without_duration_loss() -> None:
    latest = ARTIFACTS / "native_latest.json"
    if not latest.exists():
        pytest.skip("No preserved native recording available for offline replay")
    native_report = Path(json.loads(latest.read_text(encoding="utf-8"))["report"])
    run_node("""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const R = require(process.argv[2]), T = require(process.argv[1]);
const replay = require(%s), report = JSON.parse(fs.readFileSync(%s,"utf8"));
const original = JSON.stringify(report);
for (const test of report.cases) {
  const result = replay.replayCase(test,R,T);
  assert.equal(result.final_pause_loss_count,0,test.case_id);
  assert.equal(result.duration_change_count,0,test.case_id);
  if (typeof R.recognitionUpdates === "function") assert.equal(result.duplicated_final_pause_count,0,test.case_id);
  const updates = test.observations.filter(o => o.type === "asr_full_result_event")
    .flatMap(o => replay.recognitionRows(o,R));
  assert.equal(result.steps.length,updates.length,test.case_id);
}
assert.equal(JSON.stringify(report),original);
""" % (json.dumps(str(ROOT / "tests" / "captioner_counting_replay.js")), json.dumps(str(native_report))))


def test_recorded_reference_wer_and_unlabeled_silence_controls_are_separate() -> None:
    run_node("""
const assert = require("node:assert/strict");
const runner = require(%s), compare = require(%s);
assert.equal(runner.wordErrorRate("Hello, WORLD!", "hello world").wer,0);
const errors = runner.wordErrorRate("one two three", "one four");
assert.equal(errors.errors,2);
assert.equal(errors.substitutions,1);
assert.equal(errors.deletions,1);
assert.equal(errors.wer,2/3);
assert.equal(runner.wordErrorRate("one", "one more").insertions,1);
assert.equal(runner.wordErrorRate("", "").wer,null);
assert.equal(runner.wordErrorRate("one two", "1 2").wer,1,"numeric rewriting must not silently change WER policy");
const report = {cases:[{case_id:"human",observations:[
  {type:"asr_revision",text:"hello world",is_final:true,metadata:{pauses:[]}},
],playback:{source_start_performance_ms_estimate:100,wall_minus_performance_ms:0}}]};
const manifest = {cases:[{id:"human",source_kind:"recorded_human",expected_text:"Hello world.",
  silence_ground_truth:"unavailable",gaps_kind:"unlabeled_not_empty_ground_truth",gaps:[]}]};
const native = runner.compareNative(manifest,report)[0];
assert.equal(native.source_kind,"recorded_human");
assert.equal(native.silence_ground_truth,"unavailable");
assert.equal(native.word_error_rate.wer,0);
const control = runner.compareGaps([], [{start_ms:100,end_ms:200,duration_ms:100}]);
const score = compare.score([{...control,gaps_kind:"unlabeled_not_empty_ground_truth",silence_ground_truth:"unavailable"}]);
assert.equal(score.unlabeled_control_case_count,1);
assert.equal(score.unscored_control_detections,1);
assert.equal(score.expected_gaps,0);
assert.equal(score.insertion_duration_error.mean_absolute_ms,null);
""" % (json.dumps(str(RUNNER)), json.dumps(str(ROOT / "tests" / "captioner_counting_compare.js"))))


def test_replay_uses_complete_current_interim_draft() -> None:
    run_node("""
const assert = require("node:assert/strict");
const R = require(process.argv[2]), replay = require(%s);
const event = {type:"asr_full_result_event",result_index:1,results:[
  {index:0,is_final:false,alternatives:[{transcript:"one two"}]},
  {index:1,is_final:false,alternatives:[{transcript:" three"}]},
]};
assert.deepEqual(replay.recognitionRows(event,{}).map(row => row.alternatives[0].transcript),[" three"]);
if (typeof R.recognitionUpdates === "function") {
  const rows = replay.recognitionRows(event,R);
  assert.equal(rows.length,1);
  assert.equal(rows[0].index,0);
  assert.equal(rows[0].alternatives[0].transcript,"one two three");
}
""" % json.dumps(str(ROOT / "tests" / "captioner_counting_replay.js")))


def test_position_experiments_refine_only_allowed_prefixes_and_score_placement_separately() -> None:
    run_node("""
const assert = require("node:assert/strict");
const R = require(process.argv[2]), position = require(%s);
const at = ms => new Date(ms).toISOString();
const association = new R.PauseAssociation();
association.updateTranscript("one","u");
association.silenceStarted(at(1000));
const open = position.makeStrategy({refineOpen:true});
open.onTranscript({association,text:"one two",utterance:"u",observation:{elapsed_ms:1100}});
assert.equal(association.openSilence.prefix,"one two");
association.record({start_at:at(1000),end_at:at(3000),duration_ms:2000,source:"browser_rms_vad"});
open.onTranscript({association,text:"one two three",utterance:"u",observation:{elapsed_ms:3100}});
assert.equal(association.pending[0].prefix,"one two","closed prefixes stay frozen");
const blank = new R.PauseAssociation(), deferred = position.makeStrategy({deferInitialEmpty:true});
const pause = {type:"pause",start_at:at(1000),end_at:at(3060),duration_ms:2060,source:"browser_rms_vad"};
blank.record(pause);
deferred.onRecord({association:blank,event:pause,finals:[]});
deferred.onTranscript({association:blank,text:"one",utterance:"u",observation:{elapsed_ms:4000}});
deferred.onTranscript({association:blank,text:"one two",utterance:"u",observation:{elapsed_ms:4200}});
assert.equal(blank.pending[0].prefix,"one","deferred prefix freezes after first nonempty result");
const test = {case_id:"placement",observations:[{type:"vad_event",event:pause}]};
const manifest = {speech_spans:[{text:"one",end_sample_exclusive:100},{text:"two",end_sample_exclusive:33000}],
  gaps:[{start_sample:100,duration_ms:2000,after:"one",before:"two"}]};
const run = {steps:[{elapsed_ms:4000,text:"one two",final_prefix_text:"",is_final:true,
  displayed_pauses:[{startAt:pause.start_at,endAt:pause.end_at,durationMs:2060,afterChar:0}]}]};
const scored = position.scorePlacement(test,manifest,run,{matches:[{missed:false,pause_index:0}]});
assert.equal(scored.metrics.durations_retained_unchanged,1);
assert.equal(scored.metrics.final_correct_boundaries,0);
assert.equal(scored.metrics.final_wrong_boundaries,1,"a preserved duration before the wrong word is not placement success");
assert.deepEqual(position.countingTokens("one 2 three"),["1","2","3"]);
assert.equal(position.wordBoundary("","one two",2,["1","2"]).status,"inside_word_or_unscorable_prefix");
""" % json.dumps(str(ROOT / "tests" / "captioner_counting_position.js")))


def test_recorded_human_derivative_preserves_exact_insertions_without_equating_them_to_quiet_tails() -> None:
    manifest = ROOT / "collab_state" / "captioner_recording_baseline" / "inserted_gap_cases" / "manifest.json"
    if not manifest.exists():
        pytest.skip("Licensed human-audio derivative has not been prepared locally")
    run_node("""
const assert = require("node:assert/strict");
const compare = require(%s);
const config = {...compare.DEFAULT_CONFIG,variants:[compare.DEFAULT_CONFIG.variants[0]]};
const report = compare.runComparison(%s,process.argv[2],config);
for (const experiment of report.experiments) {
  assert.equal(experiment.score.expected_gaps,2);
  assert.equal(experiment.score.missed_gaps,0);
  assert.equal(experiment.score.merged_detections,0);
  assert.equal(experiment.cases[0].clipped_samples,0);
  for (const match of experiment.cases[0].comparison.matches) {
    assert.ok(match.detected_ms > match.expected_ms,
      "untrimmed source quiet tails are not part of the inserted-zero duration");
  }
}
assert.deepEqual(report.fixtures[0].gaps.map(g => g.duration_ms),[240,2000]);
""" % (json.dumps(str(ROOT / "tests" / "captioner_counting_compare.js")), json.dumps(str(manifest))))
