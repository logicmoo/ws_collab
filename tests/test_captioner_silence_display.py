from html.parser import HTMLParser
from pathlib import Path
import json
import subprocess

from ws_collab.captioner import BrowserCaptioner


ROOT = Path(__file__).parents[1] / "src" / "ws_collab"


def run_node(script: str) -> str:
    completed = subprocess.run(
        [
            "node", "-e", script,
            str(ROOT / "admin" / "transcript_runtime.js"),
            str(ROOT / "captioner" / "captioner_runtime.js"),
        ],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_live_silence_tail_timing_state_and_incremental_rendering() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const T = require(process.argv[1]);
class Node {
  constructor() {
    this.children = []; this.parentNode = null; this.attributes = {};
    this.style = {setProperty() {}};
    this._text = ""; this.writes = 0;
  }
  set textContent(value) { this.replaceChildren(); this._text = value; this.writes++; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(""); }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) { node.remove(); this.children.push(node); node.parentNode = this; }
  remove() {
    if (this.parentNode) {
      const p = this.parentNode; p.children = p.children.filter(n => n !== this);
      this.parentNode = null;
    }
  }
  replaceChildren() { [...this.children].forEach(node => node.remove()); this._text = ""; }
}
const doc = {
  createElement: () => new Node(), createElementNS: () => new Node(),
  createDocumentFragment: () => new Node(),
};
const container = new Node();
const caption = new Node(); caption.textContent = "Hello";
container.appendChild(caption);
let mono = 1000;
let wall = 5000;
const row = {eventAtMs: 4000};
const tail = new T.LiveTranscriptTail(doc, container, {
  monotonicNow: () => mono, wallNow: () => wall,
});
const vad = {source: "browser_rms_vad", available: true, state: "silence", current_silence_ms: 80};
tail.observe(vad);
tail.update(row);
assert.equal(tail.silence.textContent, "80ms");
assert.match(tail.node.textContent, /Last caption 1s ago/);
assert.equal(tail.node.attributes["aria-live"], "off");
assert.equal(container.children.at(-1), tail.node);
assert.match(tail.silence.attributes["aria-label"], /still listening/);
const writes = tail.silence.writes;
tail.update(row);
assert.equal(tail.silence.writes, writes, "unchanged timers must not churn the DOM");
mono += 100; wall += 100;
tail.update(row);
assert.equal(tail.silence.textContent, "180ms");
assert.equal(container.children[0], caption, "timer must not rebuild earlier captions");
assert.equal(caption.writes, 1);
tail.observe({...vad, state: "speech", current_silence_ms: null});
tail.update(row);
assert.match(tail.node.textContent, /Speech detected/);
assert.doesNotMatch(tail.node.textContent, /ongoing/);
tail.observe({...vad, current_silence_ms: 20});
tail.update(row);
assert.equal(tail.silence.textContent, "20ms", "new silence must start over");

// Repeated polling of the same old heartbeat does not refresh its freshness.
tail.observe(vad, {ageMs: 14900});
mono += 200;
tail.update(row);
assert.match(tail.node.textContent, /stale update/);
assert.doesNotMatch(tail.node.textContent, /ongoing/);
tail.observe(vad, {ageMs: 15100});
tail.update(row);
assert.match(tail.node.textContent, /stale update/);
tail.observe(vad, {reason: "Listening paused by the user"});
wall += 10000;
tail.update(row);
assert.match(tail.node.textContent, /Listening paused by the user/);
assert.match(tail.node.textContent, /Last caption 11\.1s ago/);
assert.doesNotMatch(tail.node.textContent, /ongoing/);
tail.observe(null);
tail.update(row);
assert.match(tail.node.textContent, /detector unavailable/);
tail.observe({...vad, state: "idle", current_silence_ms: null});
tail.update(row);
assert.match(tail.node.textContent, /Listening for speech/);

container.replaceChildren();
tail.update(row);
assert.equal(container.children.length, 1, "tail reattaches after new finals or history reload");
tail.update(null);
assert.equal(container.children.length, 0, "Clear view must hide the old tail");
tail.update({eventAtMs: null});
assert.doesNotMatch(tail.node.textContent, /Last caption/);
tail.update({eventAtMs: wall + 10000});
assert.match(tail.node.textContent, /Last caption 0ms ago/);
tail.observe({...vad, state: "speech", current_silence_ms: null});
tail.update(null);
assert.match(tail.node.textContent, /Recognizing speech/, "show speech before any recognized words or final");
const interim = {type: "STT_PARTIAL_RESULT", source_id: "browser_captioner",
  data: {engine: "browser_captioner", is_final: false, session_id: "s", utterance_id: "u", raw_text: "show me pictures"}};
tail.setInterim(interim);
tail.update(null);
assert.match(tail.node.textContent, /show me pictures/);
assert.doesNotMatch(tail.node.textContent, /Recognizing speech/);
assert.equal(tail.draft.attributes["aria-live"], "polite");
tail.setInterim({...interim, data: {...interim.data, raw_text: "show me pictures how long ago", pauses: [{
  duration_ms: 4000, start_at: "2026-09-11T00:00:00Z", end_at: "2026-09-11T00:00:04Z",
  source: "browser_rms_vad", alignment: "interim_prefix", after_char: 16,
}]}});
tail.update(null);
assert.match(tail.draft.textContent, /show me pictures4s how long ago/);
tail.finish({...interim, type: "STT_FINAL_RESULT", data: {...interim.data, is_final: true, utterance_id: "old"}});
assert.ok(tail.interim, "an earlier final must not erase the current draft");
tail.finish({...interim, type: "STT_FINAL_RESULT", data: {...interim.data, is_final: true}});
assert.equal(tail.interim, null, "final replaces the matching draft without duplication");
assert.equal(T.finalCaption(interim), null, "drafts must not be counted as durable final captions");
for (const duration of [1, 20, 40, 80, 240, 299]) {
  const first = T.finalCaption({data: {engine: "browser_captioner", raw_text: "first"}});
  const last = T.finalCaption({data: {engine: "browser_captioner", raw_text: "last", silence_before_ms: duration}});
  assert.equal(T.markerBetween(first, last).text, `${duration}ms`);
}
const first = T.finalCaption({ts: "2026-09-11T00:00:00.000Z", data: {engine: "browser_captioner", raw_text: "a"}});
const last = T.finalCaption({ts: "2026-09-11T00:00:00.299Z", data: {engine: "browser_captioner", raw_text: "b"}});
assert.equal(T.markerBetween(first, last), null, "legacy approximate gaps remain conservative");
assert.equal(T.liveSilenceState({...vad, source: "speech_recognition"}).active, false);
assert.equal(T.liveSilenceState(vad, {ageMs: NaN}).active, false);
""")


def test_adjacent_silence_durations_merge_across_final_draft_and_live_boundaries() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const T = require(process.argv[1]);
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null; this.attributes = {};
    this._text = ""; this.style = {setProperty() {}};
  }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(""); }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  appendChild(node) { node.remove(); this.children.push(node); node.parentNode = this; }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this);
    this.parentNode = null;
  }
  replaceChildren() { [...this.children].forEach(n => n.remove()); this._text = ""; }
}
const doc = {
  createElement: tag => new Node(tag), createElementNS: (_, tag) => new Node(tag),
  createDocumentFragment: () => new Node("fragment"),
};
const box = new Node("div");
const pause = (after_char, duration_ms, start) => ({
  after_char, duration_ms, source:"browser_rms_vad", alignment:"interim_prefix",
  start_at:new Date(start).toISOString(), end_at:new Date(start+duration_ms).toISOString(),
});
const event = (text, pauses, id="one") => ({
  type:"STT_FINAL_RESULT", ts:new Date(10000).toISOString(), source_id:"browser_captioner",
  data:{raw_text:text, engine:"browser_captioner", is_final:true, pauses, session_id:"s", utterance_id:id},
});
const nodes = root => [root, ...root.children.flatMap(nodes)];
const markers = () => nodes(box).filter(n => !n.hidden && n.className?.includes("silence-marker"));
const render = events => {
  const rows = events.map(T.finalCaption);
  const original = JSON.stringify(rows);
  T.renderTranscript(doc, box, rows);
  assert.equal(JSON.stringify(rows), original, "display merging must not alter saved timing");
  return rows;
};
render([event("one two", [pause(3,4000,0), pause(3,2000,6000)])]);
assert.deepEqual(markers().map(n => n.textContent), ["6s"]);
assert.ok(!nodes(box).some(n => n.tag === "svg"));
assert.equal(markers()[0].attributes.role, "note");
assert.match(markers()[0].attributes["aria-label"], /silence 6 seconds/);
render([event("one  two", [pause(3,4000,0), pause(5,2000,6000)])]);
assert.deepEqual(markers().map(n => n.textContent), ["6s"], "whitespace does not separate durations");
render([event("one word two", [pause(3,4000,0), pause(8,2000,6000)])]);
assert.deepEqual(markers().map(n => n.textContent), ["4s","2s"], "words must separate markers");
render([event("one two", [pause(3,4000,0), pause(3,4000,0), pause(3,4000,2000)])]);
assert.deepEqual(markers().map(n => n.textContent), ["6s"], "overlapping/repeated intervals count once");
render([event("one", [pause(3,2000,0)]), event("two", [pause(0,3000,2000)], "two")]);
assert.deepEqual(markers().map(n => n.textContent), ["5s"], "merge across sentence wrappers");

const [last] = render([event("done", [pause(4,2000,0)])]);
const tail = new T.LiveTranscriptTail(doc, box, {wallNow:()=>10000, monotonicNow:()=>0});
const vad = {source:"browser_rms_vad", available:true, state:"silence", current_silence_ms:3000};
tail.observe(vad); tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["5s"], "merge finalized trailing silence with ongoing silence");
tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["5s"], "repainting must not add durations again");
tail.observe({...vad, current_silence_ms:10000}); tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["10s"], "live overlap must not be added twice");
tail.observe({...vad, state:"speech", current_silence_ms:null}); tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["2s"], "completed duration must reappear when speech resumes");
const draft = event("next words", [pause(0,1000,2000), pause(10,4000,4000)], "draft");
tail.setInterim({...draft, type:"STT_PARTIAL_RESULT", data:{...draft.data,is_final:false}});
tail.observe(vad); tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["3s","6s"], "merge at both draft boundaries, deduplicating live overlap");
tail.setInterim(null); tail.observe({...vad, state:"speech", current_silence_ms:null}); tail.update(last);
assert.deepEqual(markers().map(n => n.textContent), ["2s"], "draft revisions must restore the saved marker");
const approximate = T.combineSilenceMarkers([
  T.measuredMarker(80,"between_utterances"),
  {kind:"approximate",durationMs:300},
]);
assert.equal(approximate.text, "~380ms");
assert.equal(approximate.kind, "approximate");
assert.equal(T.formatDuration(68000), "1m 08s");
""")


def test_pcm_four_second_silence_is_preserved_between_words_and_finalized(tmp_path: Path) -> None:
    output = run_node(r"""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
let result;
for (const rate of [16000, 44100, 48000]) {
  const vad = new R.BrowserRmsVad();
  const association = new R.PauseAssociation();
  vad.start(1);
  const frames = new R.RmsFrameAccumulator(rate, 20, frame => {
    if (frame.audio_time_ms >= 600 && frame.audio_time_ms < 620) association.updateTranscript("show me pictures");
    for (const event of vad.processFrame(frame.rms, frame.audio_time_ms, 100000 + frame.audio_time_ms)) {
      if (event.type === "speech_end") association.silenceStarted(event.at);
      if (event.type === "pause") association.record(event);
    }
  });
  const end = Math.round(rate * 5.2);
  for (let first = 0; first < end; first += 128) {
    const block = new Float32Array(Math.min(128, end - first));
    for (let i = 0; i < block.length; i++) {
      const second = (first + i) / rate;
      block[i] = second < .6 || second >= 4.6 ? .1 : 0;
    }
    frames.push(block, first);
  }
  result = association.finalize("show me pictures how long ago");
  assert.equal(result.pauses.length, 1);
  assert.equal(result.pauses[0].duration_ms, 4000);
  assert.equal(result.pauses[0].after_char, 16);
  assert.equal(result.pauses[0].alignment, "interim_prefix");
  assert.equal("type" in result.pauses[0], false, "internal VAD event type must not leak into the strict envelope");
}
const between = new R.PauseAssociation();
between.updateTranscript("first three words");
between.silenceStarted("2026-09-11T00:00:00Z");
between.commit(); // First sentence finalizes while the silence is still ongoing.
between.record({type:"pause", source:"browser_rms_vad", start_at:"2026-09-11T00:00:00Z", end_at:"2026-09-11T00:00:04Z", duration_ms:4000});
const next = between.finalize("last three words");
assert.equal(next.pauses[0].after_char, 0);
assert.equal(next.silence_before_ms, 4000);
console.log(JSON.stringify(result));
""")
    metadata = json.loads(output)
    published = []
    captioner = BrowserCaptioner(tmp_path, boot_id="boot", publish_item=published.append)
    captioner.heartbeat({
        "session_id": "session", "instance_id": "window", "owns_lease": True,
        "state": "listening", "queue_depth": 0,
    })
    envelope = {
        "session_id": "session", "instance_id": "window", "utterance_id": "utterance",
        "seq": 1, "revision": 1, "text": "show me pictures how long ago",
        "is_final": True, "language": "en-US", "confidence": 0.9,
        "started_at": "2026-09-11T00:00:00Z", "result_at": "2026-09-11T00:00:06Z",
        **metadata,
    }
    captioner.ingest(envelope)
    captioner.ingest(envelope)
    assert len(published) == 1
    assert published[0]["pauses"] == metadata["pauses"]

def test_only_user_pause_is_paused_and_missing_audio_is_not_silence() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
const base = {paused: false, enabled: true, supported: true, authorized: true,
  owned: true, listening: true, micPermission: "granted"};
assert.equal(R.captionerState(base).state, "listening");
assert.equal(R.captionerState({...base, paused: true}).state, "paused");
for (const other of [
  {enabled: false}, {supported: false}, {authorized: false}, {owned: false},
  {micPermission: "denied"}, {queueBlocked: true}, {restarting: true},
  {terminal: true, terminalState: "error", lastError: "network"},
]) {
  assert.notEqual(R.captionerState({...base, ...other}).state, "paused");
}
assert.match(R.captionerState({...base, authorized: false}).detail, /backend selection/);
const vad = new R.BrowserRmsVad();
vad.start(1);
vad.processFrame(.1, 0, 1000);
vad.processFrame(.1, 20, 1020);
vad.processFrame(0, 40, 1040);
vad.processFrame(0, 60, 1060);
assert.equal(vad.status(80).state, "silence");
assert.equal(vad.status(80).current_silence_ms, 40);
assert.deepEqual(vad.processFrame(0, 4000, 5000), []);
assert.equal(vad.status().state, "idle");
assert.equal(vad.status().current_silence_ms, null);
const events = [...vad.processFrame(.1, 4020, 5020), ...vad.processFrame(.1, 4040, 5040)];
assert.ok(events.some(e => e.type === "speech_start"));
assert.ok(!events.some(e => e.type === "pause"), "missing samples must never manufacture silence");
""")


def test_stale_tab_pause_flag_does_not_override_server_pause_setting(tmp_path: Path) -> None:
    captioner = BrowserCaptioner(tmp_path, boot_id="boot", publish_item=lambda _: None)
    captioner.heartbeat({
        "session_id": "session", "instance_id": "window",
        "owns_lease": False, "state": "paused", "queue_depth": 0,
    })
    assert captioner.status()["state"] == "standby"
    assert captioner.settings.get()["paused"] is False
    captioner.settings.update({"paused": True})
    assert captioner.status()["state"] == "paused"


def test_silences_survive_async_final_saves_and_sentence_revisions() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
const pause = (start, duration=4000) => ({
  type:"pause", duration_ms:duration, start_at:new Date(start).toISOString(),
  end_at:new Date(start+duration).toISOString(), source:"browser_rms_vad",
});
const a = new R.PauseAssociation();
a.updateTranscript("first three words", "u1");
const snapshot = a.snapshot("first three words");
a.silenceStarted(new Date(0).toISOString());
a.record(pause(0)); // Audio continues while IndexedDB is saving the final.
a.commit(snapshot);
let next = a.finalize("last three words", {consume:false});
assert.equal(next.pauses.length, 1, "must not erase newly detected silence");
assert.equal(next.pauses[0].duration_ms, 4000);
assert.equal(next.pauses[0].after_char, 0);
assert.equal(next.silence_before_ms, 4000);
a.updateTranscript("last three words", "u2");
const later = a.snapshot("last three words");
a.record(pause(6000,80));
a.updateTranscript("a new unfinished phrase", "u3");
a.commit(later);
assert.equal(a.transcript, "a new unfinished phrase", "old final must not clear a newer interim");
assert.equal(a.pending.length, 1);
assert.equal(a.pending[0].duration_ms, 80);
const stale = a.snapshot(a.transcript);
a.reset();
a.record(pause(8000));
a.commit(stale);
assert.equal(a.pending.length, 1, "old capture generation cannot consume a new gap");
const reserved = new R.PauseAssociation();
reserved.updateTranscript("first phrase", "first");
reserved.record(pause(0));
const firstTicket = reserved.snapshot("first phrase", {reserve:true});
reserved.updateTranscript("second phrase", "second");
assert.equal(reserved.snapshot("second phrase").metadata.pauses.length, 0,
  "the next caption cannot copy timing already reserved by an in-flight final");
reserved.restore(firstTicket);
assert.equal(reserved.pending.length, 1, "failed queue persistence must return reserved timing");
reserved.restore(firstTicket);
assert.equal(reserved.pending.length, 1, "restoring twice must not duplicate a gap");

const frozen = new R.PauseAssociation();
frozen.updateTranscript("first three words", "u1");
frozen.silenceStarted(new Date(0).toISOString());
frozen.updateTranscript("first three words last three words", "u1");
frozen.record(pause(0));
const corrected = frozen.finalize("first three words last three words");
assert.equal(corrected.pauses[0].after_char, 17, "sentence completion cannot move the silence after later words");
assert.equal(corrected.pauses[0].duration_ms, 4000);
assert.deepEqual(Object.keys(corrected.pauses[0]).sort(), [
  "after_char","alignment","duration_ms","end_at","source","start_at"
], "internal snapshot/utterance bookkeeping must never enter the wire payload");
""")


def test_whole_text_replacements_keep_silences_between_reformatted_numbers() -> None:
    cases = [
        ["1", "123", 1],
        ["1 2", "123", 2],
        ["one two", "1 2 3", 3],
        ["One two", "one, TWO three", 8],
        ["\U0001f642 one", "\U0001f642 ONE two", 5],
    ]
    for prefix, text, position in cases:
        assert BrowserCaptioner._silence_position(prefix, text) == position
    run_node(r"""
const assert = require("node:assert/strict");
const T = require(process.argv[1]);
const R = require(process.argv[2]);
const event = (text, pauses=[]) => ({
  type:"STT_PARTIAL_RESULT",source_id:"browser_captioner",
  data:{engine:"browser_captioner",is_final:false,session_id:"s",utterance_id:"u",raw_text:text,pauses},
});
const pauses = [1,3].map((after_char,index) => ({
  duration_ms:2000,start_at:new Date(index*4000).toISOString(),
  end_at:new Date(index*4000+2000).toISOString(),source:"browser_rms_vad",
  alignment:"interim_prefix",after_char,
}));
let row = T.reviseCaption(null,T.interimCaption(event("1 2 3",pauses)));
row = T.reviseCaption(row,T.interimCaption(event("123")));
assert.deepEqual(row.pauses.map(p=>p.afterChar),[1,2]);
assert.deepEqual(row.pauses.map(p=>p.durationMs),[2000,2000]);
row = T.reviseCaption(row,T.interimCaption(event("1, 2, 3.")));
assert.deepEqual(row.pauses.map(p=>p.afterChar),[1,4]);
assert.deepEqual(row.pauses.map(p=>p.durationMs),[2000,2000]);
row = T.reviseCaption(row,T.finalCaption({...event("123"),type:"STT_FINAL_RESULT",data:{...event("123").data,is_final:true}}));
assert.deepEqual(row.pauses.map(p=>p.afterChar),[1,2],"final full replacement cannot erase interim timing");
assert.equal(T.reviseCaption(row,T.interimCaption({
  ...event("different utterance"),data:{...event("different utterance").data,utterance_id:"other"},
})).pauses.length,0);
const association = new R.PauseAssociation({positionMapper:T.silencePosition});
association.updateTranscript("1","u");
association.record({...pauses[0],type:"pause"});
association.updateTranscript("1 2","u");
association.record({...pauses[1],type:"pause"});
assert.deepEqual(association.finalize("123").pauses.map(p=>p.after_char),[1,2]);
""" + "\n" + "for (const [prefix,text,position] of " + json.dumps(cases) +
        ") assert.equal(T.silencePosition(prefix,text),position);")


def test_google_split_and_recombined_interims_remain_one_caption_draft() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
const result = (text,isFinal=false) => Object.assign([{transcript:text,confidence:.9}],{isFinal});
const updates = R.recognitionUpdates({
  resultIndex:1,results:[result("1 2 3 4"),result("5 6")],
});
assert.equal(updates.length,1);
assert.equal(updates[0].index,0,"the draft identity cannot jump to the changed suffix");
assert.equal(updates[0].text,"1 2 3 4 5 6");
const recombined = R.recognitionUpdates({resultIndex:0,results:[result("1 2 3 4 5 6")]});
assert.equal(recombined[0].index,updates[0].index);
assert.equal(recombined[0].text,updates[0].text);
const mixed = R.recognitionUpdates({resultIndex:1,results:[
  result("already committed",true),result("new final",true),result("one"),result("two"),
]});
assert.deepEqual(mixed.map(u=>[u.index,u.text,u.isFinal]),[[1,"new final",true],[2,"one two",false]]);
""")


def test_backend_sentence_revisions_cannot_erase_recorded_silence(tmp_path: Path) -> None:
    publications = []
    captioner = BrowserCaptioner(tmp_path, boot_id="boot", publish_item=publications.append)
    captioner.heartbeat({
        "session_id": "session", "instance_id": "window", "owns_lease": True,
        "state": "listening", "queue_depth": 0,
    })
    silence = {
        "duration_ms": 4000, "start_at": "2026-09-11T00:00:01Z", "end_at": "2026-09-11T00:00:05Z",
        "source": "browser_rms_vad", "alignment": "interim_prefix", "after_char": 17,
    }
    item = {
        "session_id": "session", "instance_id": "window", "utterance_id": "u1",
        "seq": 1, "revision": 1, "text": "first three words last three words",
        "is_final": False, "language": "en-US", "confidence": 0.9,
        "started_at": "2026-09-11T00:00:00Z", "result_at": "2026-09-11T00:00:06Z",
        "pauses": [silence],
    }
    captioner.ingest(item)
    captioner.ingest({**item, "seq": 2, "revision": 2, "text": "revised words", "pauses": []})
    captioner.ingest({
        **item, "seq": 3, "revision": 3, "text": "revised words",
        "is_final": True, "pauses": [],
    })
    assert len(publications) == 3
    for published in publications:
        assert len(published["pauses"]) == 1
        assert published["pauses"][0]["duration_ms"] == 4000
        assert published["pauses"][0]["start_at"] == silence["start_at"]
    assert publications[-1]["pauses"][0]["after_char"] <= len("revised words")
    assert publications[-1]["pauses"][0]["alignment"] == "approximate_text_position"


def test_backend_keeps_number_gaps_when_google_removes_spaces(tmp_path: Path) -> None:
    published = []
    captioner = BrowserCaptioner(tmp_path, boot_id="boot", publish_item=published.append)
    captioner.heartbeat({
        "session_id": "session", "instance_id": "window", "owns_lease": True,
        "state": "listening", "queue_depth": 0,
    })
    item = {
        "session_id": "session", "instance_id": "window", "utterance_id": "u1",
        "seq": 1, "revision": 1, "text": "1 2 3", "is_final": False,
        "language": "en-US", "confidence": 0.9,
        "started_at": "2026-09-11T00:00:00Z", "result_at": "2026-09-11T00:00:06Z",
        "pauses": [
            {
                "duration_ms": 2000, "start_at": f"2026-09-11T00:00:0{index * 3}Z",
                "end_at": f"2026-09-11T00:00:0{index * 3 + 2}Z",
                "source": "browser_rms_vad", "alignment": "interim_prefix",
                "after_char": position,
            }
            for index, position in enumerate([1, 3])
        ],
    }
    captioner.ingest(item)
    captioner.ingest({**item, "seq": 2, "revision": 2, "text": "123", "is_final": True, "pauses": []})
    assert [pause["after_char"] for pause in published[-1]["pauses"]] == [1, 2]
    assert [pause["duration_ms"] for pause in published[-1]["pauses"]] == [2000, 2000]


def test_browser_vad_uses_audio_clock_without_page_timers() -> None:
    run_node(r"""
"use strict";
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
let stops = 0, portCloses = 0;
global.setInterval = () => { throw new Error("Page timers must not sample microphone audio"); };
const stream = {getTracks: () => [{stop: () => { stops++; }}]};
class Context {
  constructor() {
    this.state = "running"; this.currentTime = 0;
    this.audioWorklet = {addModule: async (url) => assert.equal(url, "captioner_runtime.js")};
  }
  createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
  createGain() { return {gain: {value: 1}, connect() {}, disconnect() {}}; }
  async close() { this.state = "closed"; }
}
let processor;
class Worklet {
  constructor(context, name, options) {
    assert.equal(name, "ws-collab-microphone-rms");
    assert.equal(options.processorOptions.frameIntervalMs, 20);
    processor = this; this.port = {close: () => { portCloses++; }};
  }
  connect(output) { assert.equal(output.gain.value, 0, "microphone must never be played to speakers"); }
  disconnect() {}
}
(async () => {
  let now = 0;
  const events = [];
  const capture = new R.BrowserVadCapture({
    mediaDevices: {getUserMedia: async () => stream},
    AudioContextClass: Context,
    AudioWorkletNodeClass: Worklet,
    monotonicNow: () => now,
    wallNow: () => 10000 + now,
    onEvents: batch => events.push(...batch),
  });
  assert.equal(await capture.start(1), true, capture.error);
  assert.equal(capture.status().available, false, "must receive audio before reporting detection active");
  now = 4800; // Deliver audio-thread messages in a burst as if the UI had been busy.
  for (let at = 20; at <= 4800; at += 20) {
    const rms = at <= 600 || at > 4600 ? .1 : 0;
    processor.port.onmessage({data: {rms, audio_time_ms: at}});
  }
  const silence = events.find(event => event.type === "pause");
  assert.equal(silence.duration_ms, 4000);
  assert.equal(capture.status().available, true);
  now += 1500;
  assert.equal(capture.status().available, false, "stalled audio must not claim to be listening");
  assert.match(capture.status().error, /No microphone audio frames received/);
  const lateMessage = processor.port.onmessage;
  await capture.stop();
  assert.equal(portCloses, 1);
  assert.equal(stops, 1);
  const count = events.length;
  lateMessage({data: {rms: .1, audio_time_ms: 8000}});
  assert.equal(events.length, count, "old capture messages must be ignored after stop");

  now = 0;
  const driftEvents = [];
  class SlowClockContext extends Context {
    getOutputTimestamp() { return {contextTime:this.currentTime,performanceTime:now}; }
  }
  const drift = new R.BrowserVadCapture({
    mediaDevices:{getUserMedia:async()=>stream},
    AudioContextClass:SlowClockContext, AudioWorkletNodeClass:Worklet,
    monotonicNow:()=>now, wallNow:()=>10000+now,
    onEvents:batch=>driftEvents.push(...batch),
  });
  assert.equal(await drift.start(2), true);
  for (let audioMs=20; audioMs<=2800; audioMs+=20) {
    now = audioMs / .68; // Reproduce the real browser's audio/performance clock rate mismatch.
    drift.context.currentTime = audioMs / 1000;
    processor.port.onmessage({data:{rms:audioMs<=40 || audioMs>=2780 ? .1 : 0,audio_time_ms:audioMs}});
    assert.equal(drift.status().available, true, "fresh frames cannot become stale due to audio clock skew");
  }
  assert.equal(driftEvents.find(event=>event.type==="pause").duration_ms, 4000);
  assert.equal(drift.status().error, null);
  drift.context.state = "suspended";
  assert.equal(drift.status().available, false);
  assert.match(drift.status().error, /audio context is suspended/);
  await drift.stop();
})().catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_quiet_recognition_retries_stay_short_without_hiding_real_failures() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const R = require(process.argv[2]);
for (const reason of ["no-speech", "recognizer ended"]) {
  for (const attempt of [1, 2, 8, 1000]) {
    assert.equal(R.recognitionRestartDelay(reason, attempt, () => 0), 500);
    assert.ok(R.recognitionRestartDelay(reason, attempt, () => 0.999) < 625);
  }
}
assert.equal(R.recognitionRestartDelay("network", 1, () => 0), 500);
assert.equal(R.recognitionRestartDelay("network", 4, () => 0), 4000);
assert.equal(R.recognitionRestartDelay("network", 1000, () => 0), 30000);
""")


def test_instance_list_distinguishes_reporting_pages_from_stale_history() -> None:
    run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(path.dirname(process.argv[1]), "app.js"), "utf8");
const renderSource = source.slice(
  source.indexOf("function renderCaptionerInstances("),
  source.indexOf("async function captionerInstanceAction("),
);
class Node {
  constructor(tag, cls = "", text = "") {
    this.tag = tag; this.className = cls; this.textContent = text; this.children = [];
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  querySelector(tag) { return this.children.find(node => node.tag === tag); }
}
const body = new Node("div");
const context = {
  $: () => body,
  el: (tag, cls, text) => new Node(tag, cls, text),
  actionButton: (text, cls) => new Node("button", cls, text),
  mono: text => new Node("span", "mono", text),
  badge: (text, cls) => new Node("span", cls, text),
  table: (headers, rows) => Object.assign(new Node("table"), {headers, rows}),
};
vm.createContext(context);
vm.runInContext(renderSource, context);
const shared = {session_id: "session", enabled: true, queue_depth: 0};
const active = {...shared, instance_id: "current-123", selected: true, stale: false,
  state: "restarting", last_error: "no-speech", vad: {available: true, state: "silence"}};
const stale = {...shared, instance_id: "earlier-456", selected: false, stale: true, state: "listening"};
const registry = {instances: [stale, active], stale_after_seconds: 15, selection_mode: "automatic"};
context.renderCaptionerInstances(registry);
assert.match(body.children[0].textContent, /1 reporting captioner page \(heartbeat within 15s\)/);
const liveTable = body.children[1];
assert.equal(liveTable.rows.length, 1);
assert.match(liveTable.rows[0][3], /Recognition retry \(no-speech\); Detector listening \(silence\)/);
assert.equal(liveTable.rows[0][1].title, "current-123");
const history = body.querySelector("details");
assert.equal(history.open, false);
assert.match(history.children[1].textContent, /not additional open windows/);
assert.equal(history.children[2].rows.length, 1);
assert.match(history.children[2].rows[0][3], /^Last reported:/);
assert.equal(history.children[2].rows[0][7].children[0].disabled, true);
history.open = true;
context.renderCaptionerInstances(registry);
assert.equal(body.querySelector("details").open, true, "polling must preserve expanded history");
context.renderCaptionerInstances({...registry, instances: [stale]});
assert.match(body.children[0].textContent, /0 reporting captioner pages/);
assert.match(body.children[1].textContent, /Open captioner creates or reuses/);
context.renderCaptionerInstances({...registry, instances: [active]});
assert.equal(body.querySelector("details"), undefined);
""")


def test_caption_toolbar_labels_and_tooltips_explain_listening_controls() -> None:
    class Buttons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = {}

        def handle_starttag(self, tag, attrs):
            if tag == "button":
                attributes = dict(attrs)
                self.buttons[attributes.get("id")] = attributes

    admin = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    captioner = (ROOT / "captioner" / "index.html").read_text(encoding="utf-8")
    parser = Buttons()
    parser.feed(admin)
    for key in ["cc-open", "cc-focus", "cc-pause", "cc-resume", "cc-refresh"]:
        assert parser.buttons[key]["title"]
    assert "microphone" in parser.buttons["cc-pause"]["title"]
    assert "until you resume" in parser.buttons["cc-pause"]["title"]
    assert "front" in parser.buttons["cc-focus"]["title"]
    assert "reuse" in parser.buttons["cc-open"]["title"]
    assert ">Open captioner</button>" in admin
    assert ">Show window</button>" in admin
    for html in [admin, captioner]:
        assert ">Pause listening</button>" in html
        assert ">Resume listening</button>" in html
        assert "Silence means" in html
    for directory in ["admin", "captioner"]:
        script = (ROOT / directory / ("app.js" if directory == "admin" else "captioner.js")).read_text(encoding="utf-8")
        assert "new Transcript.LiveTranscriptTail" in script
