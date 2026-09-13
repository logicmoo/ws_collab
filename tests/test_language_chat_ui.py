from __future__ import annotations

import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ADMIN = Path(__file__).resolve().parents[1] / "src" / "ws_collab" / "admin"
RUNTIME = ADMIN / "language_chat_runtime.js"
APP = ADMIN / "app.js"
INDEX = ADMIN / "index.html"

PRELUDE = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const logic = require(process.argv[1]);
function storage() {
  const values = new Map();
  return { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
}
function fixture(extra = {}) {
  const store = extra.storage || storage();
  const acks = [], statuses = [], timers = new Map();
  let time = 1000, monotonicTime = 1000, timerId = 0;
  class Utterance { constructor(text) { this.text = text; } }
  const synthesis = {
    spoken: [], cancelled: 0, resumed: 0,
    speak(u) { this.spoken.push(u); },
    cancel() { this.cancelled++; },
    resume() { this.resumed++; },
    getVoices() { return [{ voiceURI: "voice-one", name: "Installed voice", lang: "en-US", localService: true }]; },
  };
  const playback = logic.createPlayback({
    clientId: "owner", storage: store, synthesis, Utterance,
    ack: async body => { acks.push(body); },
    onStatus: status => statuses.push(status),
    now: () => time,
    monotonicNow: () => monotonicTime,
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    ...extra,
  });
  const chunks = () => synthesis.spoken.filter(u => u.text);
  return { store, acks, statuses, synthesis, playback, chunks, timers, setTime(value) { time = value; }, setMonotonic(value) { monotonicTime = value; } };
}
function snapshot(extra = {}) {
  return {
    agents: [{agent_id: "language-chat", model: "emullm-model"}],
    agent_id: "language-chat", config: {
      model: "emullm-model", endpoint: "http://127.0.0.1:8801/v1", system_prompt: "Be helpful",
      speak_replies: true, speech_rate: 1, voice_uri: "", language: "en-US",
      turn_silence_ms: 1000, max_tokens: 512, history_limit: 24,
    },
    active: true, client_id: "owner", session_id: "session-1", generation: 1,
    input_accepting: true, input_gate_reason: "ready", suppressed_input_count: 0, turn_timings: [],
    phase: "listening", pending_text: "", microphone: {available: true, state: "listening", current_silence_ms: 200},
    messages: [{id: "m1", role: "assistant", content: "Hello", status: "streaming", created_at: "2026-09-12T01:00:00Z"}],
    speech_queue: [
      {id: "q1", message_id: "m1", text: "Hello", generation: 1, status: "pending"},
      {id: "q2", message_id: "m1", text: "there", generation: 1, status: "pending"},
    ],
    ...extra,
  };
}
class Element {
  constructor(tag = "div") {
    this.tag = tag; this.tagName = tag.toUpperCase(); this.children = [];
    this.dataset = {}; this.listeners = {}; this.value = ""; this.checked = false;
    this.textContent = ""; this.disabled = false; this.hidden = false;
    this.scrollTop = 0; this.scrollHeight = 100; this.clientHeight = 100;
  }
  get options() { return this.children; }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering"); }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  fire(type) { (this.listeners[type] || []).forEach(fn => fn({target: this})); }
  reportValidity() { return true; }
}
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
function controller(options = {}) {
  const html = fs.readFileSync(process.argv[3], "utf8");
  const app = fs.readFileSync(process.argv[2], "utf8");
  const source = app.split("/* ----------------------------------------------------------- language chat */")[1]
    .split("/* ------------------------------------------------------ persistent UI state */")[0];
  assert.ok(source, "Extract actual language chat controller, not a reimplementation");
  const elements = {};
  for (const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="(lc-[^"]+)"[^>]*>/g)) {
    const element = new Element(match[1]);
    element.id = match[2];
    element.value = (match[0].match(/\bvalue="([^"]*)"/) || [])[1] || "";
    elements[element.id] = element;
  }
  const timers = new Map(), requests = [], listeners = {};
  let timerId = 0;
  let speechTime = 1000;
  const store = options.storage || storage();
  const pageId = options.clientId || "owner";
  const synth = fixture().synthesis;
  const window = {
    WsCollabLanguageChat: logic, speechSynthesis: synth,
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    crypto: {randomUUID: () => pageId},
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  };
  synth.addEventListener = window.addEventListener;
  const sandbox = {
    window, sessionStorage: store, AbortController, Date,
    performance: {now: () => speechTime},
    document: { createElement: tag => new Element(tag) },
    state: { token: "test-only", page: "chatbot-test" }, API_BASE: "/ws_collab",
    $: id => { assert.ok(elements[id], `Unknown element ${id}`); return elements[id]; },
    el: (tag, cls, text) => { const e = new Element(tag); e.className = cls; e.textContent = text; return e; },
    fmt: value => JSON.stringify(value, null, 2),
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        requests.push({url, options, resolve: body => resolve({ok: true, json: async () => body}), reject});
      });
    },
  };
  // Let the playback helper share the same mock scheduler instead of real timers.
  const runtimeContext = { ...sandbox, module: {exports: {}}, globalThis: {} };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), runtimeContext);
  window.WsCollabLanguageChat = runtimeContext.module.exports;
  const context = vm.createContext(sandbox);
  vm.runInContext(source + "\ninitLanguageChat(); languageChat.setVisible(true);", context);
  return {
    elements, requests, synth, listeners, timers, store,
    setMonotonic(value) { speechTime = value; },
    run(code) { return vm.runInContext(code, context); },
    tick(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `No timer with delay ${delay}`);
      timers.delete(entry[0]); entry[1].fn();
    },
  };
}
async function bindStartingVoice(c) {
  const binding = c.requests.at(-1);
  assert.equal(binding.url, "/ws_collab/language-chat/config");
  const saved = {...snapshot().config, ...JSON.parse(binding.options.body)};
  binding.resolve(snapshot({active:false, config:saved, speech_queue:[]}));
  await settle();
  assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/start");
  return c.requests.at(-1);
}
async function startController(c, state = snapshot()) {
  c.elements["lc-start"].onclick();
  await settle();
  const start = await bindStartingVoice(c);
  start.resolve(state);
  await settle();
}
"""


def run_node(script: str) -> None:
    result = subprocess.run(
        [
            "node",
            "-e",
            PRELUDE + "\n(async () => {\n" + script + "\n})().catch(e => { console.error(e); process.exitCode = 1; });",
            str(RUNTIME),
            str(APP),
            str(INDEX),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=ADMIN.parents[2],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_queue_is_owned_ordered_and_acknowledged_once() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
const state = snapshot();
f.playback.update(state);
f.playback.update(state);
assert.deepEqual(f.chunks().map(u => u.text), ["Hello"]);
assert.equal(f.playback.speechState, "idle", "queued does not mean actually speaking");
f.chunks()[0].onstart();
f.playback.heartbeat();
f.chunks()[0].onend();
assert.deepEqual(f.chunks().map(u => u.text), ["Hello", "there"]);
f.chunks()[1].onstart();
f.chunks()[1].onend();
f.playback.update(state);
await f.playback.flushAcks();
assert.deepEqual(f.acks.filter(a => a.speech_state === "done").map(a => a.speech_id), ["q1", "q2"]);
assert.equal(f.acks[0].speech_state, "speaking");
assert.ok(f.acks.every(a => a.client_id === "owner"));
assert.equal(f.chunks().length, 2);
""")


def test_reserved_speech_is_not_replayed_after_remount_or_reload() -> None:
    run_node(r"""
const first = fixture();
first.playback.unlock();
first.playback.update(snapshot());
const second = fixture({storage: first.store});
second.playback.update(snapshot());
assert.equal(second.chunks().length, 0, "Reload does not implicitly opt in");
second.playback.unlock();
second.playback.update(snapshot());
assert.deepEqual(second.chunks().map(u => u.text), ["there"], "Reserved q1 must not repeat even without a done ack");
second.chunks()[0].onend();
second.playback.update(snapshot());
assert.equal(second.chunks().length, 1);
""")


def test_non_owner_is_silent_and_ownership_loss_cancels() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot({client_id: "someone-else"}));
assert.equal(f.chunks().length, 0);
assert.equal(f.playback.armed, false);
f.playback.unlock();
f.playback.update(snapshot());
const utterance = f.chunks()[0];
f.playback.update(snapshot({client_id: "someone-else"}));
utterance.onend();
await f.playback.flushAcks();
assert.equal(f.chunks().length, 1);
assert.equal(f.acks.length, 0);
assert.ok(f.synthesis.cancelled > 0);
""")


def test_generation_change_cancels_current_and_rejects_old_chunks() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot());
const old = f.chunks()[0];
f.playback.update(snapshot({generation: 2, speech_queue: [
  ...snapshot().speech_queue,
  {id:"new", message_id:"m2", text:"New reply", generation:2, status:"pending"},
]}));
old.onend();
old.onerror({error: "interrupted"});
assert.deepEqual(f.chunks().map(u => u.text), ["Hello", "New reply"]);
f.playback.update(snapshot());
assert.equal(f.chunks().length, 2, "Old generation is ignored");
await f.playback.flushAcks();
assert.equal(f.acks.length, 0, "Stale callbacks cannot acknowledge the new generation");
""")


def test_interrupt_blocks_old_revision_and_interrupted_messages() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot());
const old = f.chunks()[0];
f.playback.interrupt();
f.playback.update(snapshot());
old.onend();
assert.equal(f.chunks().length, 1);
f.playback.update(snapshot({
  generation: 2,
  messages: [{id:"m2", role:"assistant", status:"interrupted", content:"partial"}],
  speech_queue: [{id:"q3", message_id:"m2", text:"Must not speak", generation:2, status:"pending"}],
}));
assert.equal(f.chunks().length, 1);
f.playback.update(snapshot({
  generation: 3,
  speech_queue: [{id:"q4", message_id:"m3", text:"Next turn", generation:3, status:"pending"}],
}));
assert.equal(f.chunks().at(-1).text, "Next turn");
""")


def test_interrupted_message_cancels_speech_without_generation_bump() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot());
const old = f.chunks()[0];
f.playback.update(snapshot({messages: [{id:"m1", status:"interrupted", content:"partial"}]}));
old.onend();
assert.equal(f.chunks().length, 1);
assert.equal(f.playback.status, "idle");
assert.match(f.playback.detail, /interrupted/);
""")


def test_stop_and_stale_state_never_rearm_playback() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot());
f.playback.stop();
f.playback.update(snapshot({generation: 2, speech_queue: [
  {id:"new", message_id:"m2", text:"No", generation:2, status:"pending"},
]}));
assert.equal(f.chunks().length, 1);
assert.equal(f.playback.armed, false);
f.playback.unlock();
f.playback.update(snapshot({generation: 2, speech_queue: [
  {id:"new", message_id:"m2", text:"Yes", generation:2, status:"pending"},
]}));
f.setTime(6999);
f.playback.expire();
assert.equal(f.playback.armed, true, "Freshness budget is six seconds, not 3.5 seconds");
f.setTime(7000);
f.playback.expire();
assert.equal(f.playback.status, "unknown");
assert.equal(f.playback.armed, false);
f.playback.update(snapshot({generation: 3}));
assert.equal(f.chunks().length, 2, "Recovery requires a new user gesture");
""")


def test_browser_error_ack_does_not_claim_completion() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot());
f.chunks()[0].onstart();
f.chunks()[0].onerror({error:"not-allowed"});
await f.playback.flushAcks();
assert.deepEqual(f.acks.map(a => a.speech_state), ["speaking", "error"]);
assert.equal(f.acks[1].speech_id, "q1");
assert.equal(f.acks[1].error, "not-allowed");
assert.equal(f.playback.armed, false);
assert.match(f.playback.detail, /not-allowed/);
assert.equal(f.chunks().length, 1);
""")


def test_estimate_never_completes_speech_or_cancels_for_live_stt() -> None:
    run_node(r"""
assert.equal(logic.estimateSpeechDurationMs("one two three", 1), 1000);
assert.equal(logic.estimateSpeechDurationMs("one two three", 2), 500);
assert.equal(logic.estimateSpeechDurationMs("one two three", .5), 2000);
assert.equal(logic.estimateSpeechDurationMs("", 1), null);
const f = fixture();
const blocked = snapshot({input_accepting:false, input_gate_reason:"output_pending"});
f.playback.unlock();
f.playback.update(blocked);
f.chunks()[0].onstart();
await f.playback.flushAcks();
const cancelled = f.synthesis.cancelled;
const estimate = logic.queuedSpeechEstimateMs(blocked);
assert.equal(estimate, 667);
f.setTime(1000 + estimate + 1000);
f.playback.update({...blocked, microphone:{available:true,state:"speaking"}, pending_text:"agent echo"});
f.playback.expire();
assert.equal(f.playback.speechState, "speaking");
assert.equal(f.synthesis.cancelled, cancelled, "STT activity is not voice barge-in");
assert.equal(f.acks.some(a => a.speech_state === "done"), false, "Passing the estimate cannot fabricate onend");
assert.equal(blocked.input_accepting, false);
f.setMonotonic(3500);
f.chunks()[0].onend({elapsedTime:2.5});
await f.playback.flushAcks();
assert.equal(f.acks.find(a => a.speech_state === "done").duration_ms, 2500);
assert.equal(f.chunks().length, 2, "Full queue still needs its actual callbacks");
assert.equal(logic.queuedSpeechEstimateMs({...blocked, speech_queue:blocked.speech_queue.map(q => ({...q,status:"done"}))}), null);
""")


def test_actual_browser_duration_ack_uses_observed_monotonic_time() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot({speech_queue:[snapshot().speech_queue[0]]}));
f.chunks()[0].onstart();
f.setMonotonic(10000);
f.chunks()[0].onend({elapsedTime:1.2346});
await f.playback.flushAcks();
assert.equal(f.acks[0].speech_state, "speaking");
assert.equal("duration_ms" in f.acks[0], false);
assert.equal(f.acks[1].speech_state, "done");
assert.equal(f.acks[1].duration_ms, 9000, "Engine elapsedTime cannot override the observed monotonic interval");
assert.equal(Number.isInteger(f.acks[1].duration_ms), true);
""")


def test_actual_browser_duration_rejects_inconsistent_engine_units() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot({speech_queue:[snapshot().speech_queue[0]]}));
f.setMonotonic(2500);
f.chunks()[0].onstart();
f.setTime(-99999);
f.setMonotonic(6900);
f.chunks()[0].onend({});
await f.playback.flushAcks();
assert.equal(f.acks.find(a => a.speech_state === "done").duration_ms, 4400, "Wall-clock changes do not alter actual playback duration");
for (const elapsedTime of [null, undefined, NaN, Infinity, -1, 1801]) {
  assert.equal(logic.speechDurationMs({elapsedTime}, 200, 1200), 1000);
}
assert.equal(logic.speechDurationMs({elapsedTime:0}, 200, 1200), 1000);
assert.equal(logic.speechDurationMs({elapsedTime:1800}, 200, 1200), 1000);
assert.equal(logic.speechDurationMs({elapsedTime:1379.4}, 5000, 6379.4), 1379, "Native milliseconds must not be multiplied into a 23-minute pong");
assert.equal(logic.speechDurationMs({}, undefined, 1200), undefined, "Unknown duration is omitted, not zero");
assert.equal(logic.speechDurationMs({}, 0, 1800001), undefined, "Out-of-range durations cannot break the completion ack");
""")


def test_model_selection_displays_worker_pinning_instead_of_claiming_label_routing() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({
  active:false,
  config:{...snapshot().config,model:"gpt-5.6-luna",endpoint:"http://127.0.0.1:8801/emullm/specific_worker/worker-copilot-1/v1"},
}));
await settle();
assert.equal(c.elements["lc-model"].value,"gpt-5.6-luna");
assert.match(c.elements["lc-model-status"].textContent,/worker-copilot-1 \(pinned\)/);
assert.match(c.elements["lc-model-route-hint"].textContent,/changing only the model label does not change/);
""")


def test_owner_heartbeat_is_independent_of_audio_arming() -> None:
    run_node(r"""
const f = fixture();
f.playback.update(snapshot({speech_queue:[]}));
assert.equal(f.playback.armed, false);
f.playback.heartbeat();
await f.playback.flushAcks();
assert.deepEqual(f.acks, [{client_id:"owner",speech_state:"idle"}]);
f.playback.unavailable("Playback paused");
f.playback.heartbeat();
await f.playback.flushAcks();
assert.equal(f.acks.length, 2);
f.playback.update(snapshot({client_id:"other-client"}));
f.playback.heartbeat();
await f.playback.flushAcks();
assert.equal(f.acks.length, 2, "Only the owner renews the session");
""")


def test_missing_browser_voice_or_synthesis_is_an_error_not_fake_tts() -> None:
    run_node(r"""
const unavailable = fixture({synthesis:null});
unavailable.playback.unlock();
unavailable.playback.update(snapshot());
await unavailable.playback.flushAcks();
assert.equal(unavailable.playback.supported, false);
assert.equal(unavailable.acks[0].speech_state, "error");
assert.match(unavailable.acks[0].error, /unavailable/);
assert.equal(unavailable.chunks().length, 0);
const missing = fixture();
missing.playback.unlock();
missing.playback.update(snapshot({config:{speak_replies:true, voice_uri:"missing"}}));
await missing.playback.flushAcks();
assert.equal(missing.chunks().length, 0);
assert.equal(missing.acks[0].speech_state, "error");
assert.match(missing.playback.detail, /installed voice/);
""")


def test_installed_voice_rate_and_mute_are_used() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot({config:{voice_uri:"voice-one", speech_rate:1.4, language:"fr-FR", speak_replies:true}}));
const spoken = f.chunks()[0];
assert.equal(spoken.voice.voiceURI, "voice-one");
assert.equal(spoken.rate, 1.4);
assert.equal(spoken.lang, "fr-FR");
f.playback.mute(true);
spoken.onend();
f.playback.update(snapshot());
await f.playback.flushAcks();
assert.equal(f.chunks().length, 1);
assert.ok(f.acks.every(a => a.speech_state !== "done"));
assert.match(f.acks[0].error, /muted/);
""")


def test_agent_voice_queue_override_falls_back_to_bound_voice_and_keeps_actual_acks() -> None:
    run_node(r"""
const f = fixture();
const boundVoice = f.synthesis.getVoices()[0];
const copilotVoice = {voiceURI:"copilot-voice", name:"Copilot voice", lang:"en-US", localService:true};
f.synthesis.getVoices = () => [boundVoice, copilotVoice];
const state = snapshot({
  input_accepting:false, input_gate_reason:"agent_voice",
  config:{...snapshot().config, voice_uri:boundVoice.voiceURI},
  messages:[{id:"m1",role:"assistant",channel:"agent_voice",source_agent_id:"copilot",content:"Announcement"}],
  speech_queue:[
    {...snapshot().speech_queue[0],voice_uri:copilotVoice.voiceURI},
    snapshot().speech_queue[1],
  ],
});
f.playback.unlock();
f.playback.update(state);
assert.equal(f.chunks()[0].voice.voiceURI, "copilot-voice");
f.chunks()[0].onstart();
f.setMonotonic(2500);
f.chunks()[0].onend({elapsedTime:1.5});
assert.equal(f.chunks()[1].voice.voiceURI, "voice-one", "No source override uses the current bound voice");
f.chunks()[1].onstart();
f.setMonotonic(3250);
f.chunks()[1].onend({elapsedTime:.75});
await f.playback.flushAcks();
assert.deepEqual(f.acks.filter(a => a.speech_state === "done").map(a => a.duration_ms), [1500,750]);
assert.equal(state.input_accepting, false, "Announcements never locally reopen the feedback-input gate");
""")


def test_done_playing_error_queue_entries_never_replay() -> None:
    run_node(r"""
const f = fixture();
f.playback.unlock();
f.playback.update(snapshot({speech_queue: ["done", "playing", "error"].map((status, i) => ({
  id:`q${i}`, message_id:"m1", text:"Old", generation:1, status,
}))}));
assert.equal(f.chunks().length, 0);
""")


def test_failed_ack_and_browser_start_timeout_pause_playback() -> None:
    run_node(r"""
const f = fixture({ack: async () => { throw new Error("offline"); }});
f.playback.unlock();
f.playback.update(snapshot());
f.chunks()[0].onstart();
await f.playback.flushAcks();
assert.equal(f.playback.status, "unknown");
assert.equal(f.playback.armed, false);
const blocked = fixture();
blocked.playback.unlock();
blocked.playback.update(snapshot());
[...blocked.timers.values()].find(t => t.delay === 8000).fn();
await blocked.playback.flushAcks();
assert.equal(blocked.playback.status, "error");
assert.match(blocked.acks[0].error, /did not start/);
""")


def test_revision_gate_rejects_old_polls_commands_and_sessions() -> None:
    run_node(r"""
const gate = logic.createRevisionGate();
const old = gate.ticket();
gate.invalidate();
const stop = gate.ticket();
assert.equal(gate.accept(stop, snapshot({active:false})), true);
assert.equal(gate.accept(old, snapshot()), false);
assert.equal(gate.accept(gate.ticket(), snapshot({generation:2})), true);
assert.equal(gate.accept(gate.ticket(), snapshot({generation:1})), false);
assert.equal(gate.accept(gate.ticket(), snapshot({session_id:"session-2", generation:0})), true);
assert.equal(gate.accept(gate.ticket(), snapshot({session_id:"session-1", generation:10})), false);
assert.equal(gate.accept(gate.ticket(), snapshot({session_id:"", active:false, generation:0})), true);
""")


def test_dirty_drafts_survive_polls_and_save_races() -> None:
    run_node(r"""
const draft = logic.createDraft();
let value = "";
draft.sync(() => value = "server");
draft.edit();
value = "my edit";
draft.sync(() => value = "poll overwrite");
assert.equal(value, "my edit");
const saving = draft.capture();
draft.edit();
draft.saved(saving);
assert.equal(draft.dirty, true);
draft.sync(() => value = "stale save overwrite");
assert.equal(value, "my edit");
draft.saved(draft.capture());
draft.sync(() => value = "saved");
assert.equal(value, "saved");
""")


def test_history_validation_only_allows_chat_context_roles() -> None:
    run_node(r"""
assert.deepEqual(logic.parseHistory('[{"role":"user","content":"Hello"},{"role":"assistant","content":"Hi"}]'), [
  {role:"user",content:"Hello"}, {role:"assistant",content:"Hi"}
]);
assert.deepEqual(logic.parseHistory("[]"), []);
for (const value of [
  "bad JSON", "{}", '[{"role":"system","content":"hidden"}]',
  '[{"role":"tool","content":"output"}]', '[{"role":"user","content":null}]',
  '[{"role":"user","content":"  "}]', '[{"role":"user","content":"ok","tool_calls":[]}]',
]) assert.throws(() => logic.parseHistory(value), /history|History|role/);
""")


def test_messages_render_as_safe_text_with_timestamp_and_status() -> None:
    run_node(r"""
const document = {createElement: tag => new Element(tag)};
const text = '<img src=x onerror="alert(1)"> & <script>bad</script>';
const row = logic.renderMessage(document, {role:"assistant", content:text, status:"interrupted", created_at:"2026-09-12T01:02:03Z"});
assert.equal(row.children[1].textContent, text);
assert.equal(row.children[0].children[2].textContent, "interrupted");
assert.equal(row.children[0].children[1].dateTime, "2026-09-12T01:02:03.000Z");
assert.equal(row.children[1].children.length, 0);
""")


def test_source_agent_is_the_safe_announcement_speaker_label() -> None:
    run_node(r"""
const document = {createElement: tag => new Element(tag)};
for (const source_agent_id of ["copilot", "codex", "<script>not HTML</script>"]) {
  const row = logic.renderMessage(document, {role:"assistant",channel:"agent_voice",source_agent_id,content:"Ready",created_at:1789174923});
  assert.equal(row.children[0].children[0].textContent, source_agent_id);
  assert.equal(row.children[0].children[0].children.length, 0);
}
const normal = logic.renderMessage(document, {role:"assistant",content:"Model response"});
assert.equal(normal.children[0].children[0].textContent, "Agent");
""")


def test_message_timestamps_use_epoch_seconds_and_keep_unknown_times_unknown() -> None:
    run_node(r"""
const document = {createElement: tag => new Element(tag)};
const iso = "2026-09-12T01:02:03.500Z";
const seconds = Date.parse(iso) / 1000;
for (const created_at of [seconds, String(seconds), iso]) {
  const row = logic.renderMessage(document, {role:"user",content:"Test",created_at});
  assert.equal(row.children[0].children[1].dateTime, iso);
}
for (const created_at of [undefined, null, "", "invalid", false]) {
  const row = logic.renderMessage(document, {role:"user",content:"Test",created_at});
  assert.equal(row.children[0].children[1].textContent, "Time unavailable");
  assert.equal(row.children[0].children[1].dateTime, undefined);
}
assert.equal(logic.timestampMillis(0), 0, "A valid numeric epoch is distinct from null");
""")


def test_timing_trace_distinguishes_measured_waiting_and_unknown_without_guesses() -> None:
    run_node(r"""
const trace = logic.timingTrace([{
  message_id:"turn-2", status:"speaking", input_received_at:200, request_started_at:201,
  first_token_at:203, response_finished_at:260, tts_started_at:261,
  speech_to_stt_ms:null, stt_delivery_ms:0, turn_wait_ms:1000, model_queue_ms:null,
  emullm_first_token_ms:2000, emullm_total_ms:59000, tts_queue_wait_ms:1000,
  tts_playback_ms:null, elapsed_ms:65000, total_ms:null,
}, {message_id:"turn-1",status:"complete",input_received_at:100,total_ms:1000}]);
assert.equal(trace.latest.message_id, "turn-2", "Latest turn does not depend on list ordering when timestamps exist");
const byKey = Object.fromEntries(trace.stages.map(s => [s.key,s]));
assert.equal(byKey.speech_to_stt_ms.display, "Not measured");
assert.equal(byKey.stt_delivery_ms.display, "0 ms");
assert.equal(byKey.model_queue_ms.display, "Not measured", "Do not derive a missing duration from neighboring timestamps");
assert.equal(byKey.emullm_total_ms.display, "59.0 s");
assert.equal(byKey.emullm_total_ms.state, "measured");
assert.equal(byKey.tts_playback_ms.display, "Waiting for measurement");
assert.equal(byKey.tts_playback_ms.state, "waiting");
assert.equal(trace.elapsed, "65.0 s");
assert.equal(trace.total, "Pending");
const waiting = logic.timingTrace([{status:"thinking",input_received_at:100,request_started_at:101,emullm_first_token_ms:null,emullm_total_ms:null}]);
assert.equal(waiting.stages.find(s => s.key === "emullm_first_token_ms").state, "waiting");
assert.equal(waiting.stages.find(s => s.key === "tts_playback_ms").state, "pending");
assert.equal(waiting.elapsed, "Not measured");
for (const status of ["complete","interrupted","error"]) {
  const empty = logic.timingTrace([{status}]);
  assert.equal(empty.stages.every(s => s.display === "Not measured"), true);
  assert.equal(empty.total, "Not measured");
}
for (const value of [null, undefined, NaN, "0", -1]) assert.equal(logic.formatDurationMs(value), "Not measured");
assert.equal(logic.timingTrace([]), null);
""")


def test_client_id_is_fresh_for_each_page_even_with_copied_session_storage() -> None:
    run_node(r"""
const store = storage();
const crypto = require("node:crypto").webcrypto;
const id = logic.createClientId(store, crypto);
assert.match(id, /^[0-9a-f-]{36}$/);
const copied = storage();
copied.setItem("ws_collab_language_chat_client", id);
const duplicateId = logic.createClientId(copied, crypto);
assert.notEqual(duplicateId, id, "A duplicated tab cannot inherit ownership");
assert.notEqual(logic.createClientId(store, crypto), id, "Reload also gets a fresh page id");
assert.equal(copied.getItem("ws_collab_language_chat_client"), duplicateId);
assert.notEqual(logic.createClientId(storage(), crypto), id);
const fallback = logic.createClientId(storage(), {getRandomValues: array => crypto.getRandomValues(array)});
assert.match(fallback, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
""")


def test_reloaded_page_retains_speech_journal_under_a_new_owner_id() -> None:
    run_node(r"""
const old = fixture();
old.playback.unlock();
old.playback.update(snapshot());
const reloaded = fixture({storage:old.store, clientId:"new-page"});
reloaded.playback.update(snapshot());
assert.equal(reloaded.chunks().length, 0);
reloaded.playback.unlock();
reloaded.playback.update(snapshot({client_id:"new-page"}));
assert.deepEqual(reloaded.chunks().map(u => u.text), ["there"], "Changing owner id does not replay reserved q1");
""")


def test_duplicated_controller_cannot_speak_or_adopt_the_original_owner() -> None:
    run_node(r"""
const first = controller({clientId:"page-one"});
first.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
await startController(first, snapshot({client_id:"page-one"}));
assert.equal(first.synth.spoken.filter(u => u.text).length, 1);
const copied = storage();
copied.setItem("ws_collab_language_chat_client", first.store.getItem("ws_collab_language_chat_client"));
copied.setItem("ws_collab_language_chat_spoken", first.store.getItem("ws_collab_language_chat_spoken"));
const duplicate = controller({clientId:"page-two", storage:copied});
duplicate.requests[0].resolve(snapshot({client_id:"page-one"}));
await settle();
assert.notEqual(first.store.getItem("ws_collab_language_chat_client"), duplicate.store.getItem("ws_collab_language_chat_client"));
assert.equal(duplicate.synth.spoken.length, 0);
assert.equal(duplicate.elements["lc-start"].disabled, true);
assert.equal(duplicate.elements["lc-global-stop"].disabled, true);
duplicate.run("initLanguageChat()");
assert.equal(duplicate.store.getItem("ws_collab_language_chat_client"), "page-two", "Remounting this page reuses its existing controller");
assert.equal(duplicate.listeners.pagehide.length, 1);
""")


def test_browser_voice_is_automatically_selected_without_manual_setup() -> None:
    run_node(r"""
const voices = [
  {voiceURI:"fr", lang:"fr-FR", localService:true},
  {voiceURI:"en", lang:"en-US", localService:true},
  {voiceURI:"default-en", lang:"en-US", default:true},
];
assert.equal(logic.chooseVoice(voices, {language:"en-US"}).voiceURI, "default-en");
assert.equal(logic.chooseVoice(voices, {voice_uri:"fr",language:"en-US"}).voiceURI, "fr");
assert.equal(logic.chooseVoice(voices, {voice_uri:"removed",language:"fr-FR"}).voiceURI, "fr");
assert.equal(logic.chooseVoice(voices, {language:"en-GB"}).voiceURI, "default-en");
assert.equal(logic.chooseVoice(voices, {language:"de-DE"}).voiceURI, "default-en");
assert.equal(logic.chooseVoice([], {}), null);
""")


def test_controller_start_saves_default_model_and_installed_voice_then_monitors() -> None:
    run_node(r"""
const c = controller();
const config = {...snapshot().config, model:"", voice_uri:"removed"};
c.requests[0].resolve(snapshot({active:false, config, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-model"].value, "emullm/default");
assert.equal(c.elements["lc-start"].disabled, false, "No manual model/voice setup is necessary");
c.elements["lc-start"].onclick();
await settle();
const binding = c.requests.at(-1);
assert.equal(binding.url, "/ws_collab/language-chat/config");
assert.deepEqual(JSON.parse(binding.options.body), {agent_id:"language-chat", voice_uri:"voice-one", model:"emullm/default"});
const started = await bindStartingVoice(c);
started.resolve(snapshot({config:{...config,model:"emullm/default",voice_uri:"voice-one"}}));
await settle();
assert.equal(c.synth.spoken.filter(u => u.text)[0].voice.voiceURI, "voice-one");
assert.equal(c.requests.filter(r => /\/send(?:-now)?$/.test(r.url)).length, 0, "Server turns and browser replies require no per-turn Send");
assert.match(c.elements["lc-compose-help"].textContent, /Automatically monitoring finalized non-echo STT/);
""")


def test_controller_stop_during_voice_binding_never_enables_agent() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
c.elements["lc-start"].onclick();
await settle();
const binding = c.requests.at(-1);
assert.equal(binding.url, "/ws_collab/language-chat/config");
c.elements["lc-stop"].onclick();
binding.resolve(snapshot({active:false}));
await settle();
assert.equal(c.requests.some(r => r.url.endsWith("/start")), false);
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/stop");
c.requests.at(-1).resolve(snapshot({active:false, speech_queue:[]}));
await settle();
assert.equal(c.synth.spoken.filter(u => u.text).length, 0);
""")


def test_controller_voice_binding_failure_never_enables_agent() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
c.elements["lc-start"].onclick();
await settle();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/config");
c.requests.at(-1).reject(new Error("Could not save voice"));
await settle();
assert.equal(c.requests.some(r => r.url.endsWith("/start")), false);
assert.match(c.elements["lc-error"].textContent, /Could not save voice/);
assert.equal(c.synth.spoken.filter(u => u.text).length, 0);
""")


def test_controller_waits_for_voice_enumeration_before_binding() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
const voices = c.synth.getVoices;
c.synth.getVoices = () => [];
c.elements["lc-start"].onclick();
await settle();
assert.equal(c.requests.length, 1, "Wait for initial browser voice enumeration before saving");
c.synth.getVoices = voices;
c.tick(1200);
await settle();
assert.equal(JSON.parse(c.requests.at(-1).options.body).voice_uri, "voice-one");
const started = await bindStartingVoice(c);
started.resolve(snapshot());
await settle();
assert.equal(c.synth.spoken.filter(u => u.text).length, 1);
""")


def test_controller_preserves_edits_and_does_not_duplicate_initialization() -> None:
    run_node(r"""
const c = controller();
c.run("initLanguageChat()");
assert.equal(c.requests.length, 1);
assert.equal(c.listeners.pagehide.length, 1);
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-prompt"].value, "Be helpful");
c.elements["lc-prompt"].value = "Keep my edit";
c.elements["lc-settings"].listeners.input[0]({target:c.elements["lc-prompt"]});
c.elements["lc-history"].value = '[{"role":"user","content":"Keep history"}]';
c.elements["lc-history"].fire("input");
c.tick(2000);
c.requests.at(-1).resolve(snapshot({active:false, config:{...snapshot().config, system_prompt:"Server changed"}}));
await settle();
assert.equal(c.elements["lc-prompt"].value, "Keep my edit");
assert.match(c.elements["lc-history"].value, /Keep history/);
assert.equal(c.elements["lc-start"].disabled, true, "Do not start with unsaved context");
c.elements["lc-load-agent"].onclick();
assert.match(c.elements["lc-error"].textContent, /Save the current settings and history/);
""")


def test_model_and_endpoint_are_typable_drafts_while_chat_is_active() -> None:
    run_node(r"""
const c = controller();
assert.equal(c.elements["lc-model"].disabled, true, "Wait for initial configuration");
c.requests[0].resolve(snapshot({client_id:"another-client", speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-settings-fields"].disabled, false);
assert.equal(c.elements["lc-model"].disabled, false);
assert.equal(c.elements["lc-endpoint"].disabled, false);
assert.equal(c.elements["lc-prompt"].disabled, true);
assert.equal(c.elements["lc-save-settings"].disabled, true);
for (const [id, value] of [["lc-model", "custom/not-in-catalog"], ["lc-endpoint", "https://custom.example/api/v1"]]) {
  c.elements[id].value = value;
  c.elements["lc-settings"].listeners.input[0]({target:c.elements[id]});
}
const count = c.requests.length;
c.elements["lc-settings"].onsubmit({preventDefault() {}});
assert.equal(c.requests.length, count);
assert.match(c.elements["lc-settings-result"].textContent, /Stop voice chat/);
c.tick(450);
c.requests.at(-1).resolve(snapshot({active:false, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-model"].value, "custom/not-in-catalog");
assert.equal(c.elements["lc-endpoint"].value, "https://custom.example/api/v1");
c.elements["lc-settings"].onsubmit({preventDefault() {}});
await settle();
const save = c.requests.at(-1);
const body = JSON.parse(save.options.body);
assert.equal(save.url, "/ws_collab/language-chat/config");
assert.equal(body.model, "custom/not-in-catalog");
assert.equal(body.endpoint, "https://custom.example/api/v1");
save.resolve(snapshot({active:false, config:body, speech_queue:[]}));
await settle();
assert.match(c.elements["lc-settings-result"].textContent, /saved/);
""")


def test_model_endpoint_drafts_survive_disconnect_and_inflight_save() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
c.tick(2000);
c.requests.at(-1).reject(new Error("offline"));
await settle();
assert.equal(c.elements["lc-model"].disabled, false);
assert.equal(c.elements["lc-endpoint"].disabled, false);
assert.equal(c.elements["lc-save-settings"].disabled, true);
c.elements["lc-model"].value = "draft-one";
c.elements["lc-settings"].listeners.input[0]({target:c.elements["lc-model"]});
c.tick(2000);
c.requests.at(-1).resolve(snapshot({active:false, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-model"].value, "draft-one");
c.elements["lc-settings"].onsubmit({preventDefault() {}});
await settle();
const save = c.requests.at(-1);
const submitted = JSON.parse(save.options.body);
assert.equal(c.elements["lc-model"].disabled, false, "Can keep drafting while save is in flight");
c.elements["lc-model"].value = "draft-two";
c.elements["lc-endpoint"].value = "https://next.example/v1";
c.elements["lc-settings"].listeners.input[0]({target:c.elements["lc-model"]});
save.resolve(snapshot({active:false, config:submitted, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-model"].value, "draft-two");
assert.equal(c.elements["lc-endpoint"].value, "https://next.example/v1");
assert.match(c.elements["lc-settings-result"].textContent, /newer edits remain unsaved/);
""")


def test_controller_start_is_opt_in_and_stop_wins_inflight_start() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
assert.equal(c.synth.spoken.length, 0, "Initial state fetch never starts TTS");
c.elements["lc-start"].onclick();
assert.equal(c.synth.resumed, 1, "Unlock happens synchronously in the Start gesture");
await settle();
const start = await bindStartingVoice(c);
assert.equal(start.url, "/ws_collab/language-chat/start");
assert.equal(JSON.parse(start.options.body).client_id, "owner");
c.elements["lc-stop"].onclick();
assert.ok(c.synth.cancelled > 0, "Stop cancels before waiting on network");
start.resolve(snapshot());
await settle();
assert.equal(c.synth.spoken.filter(u => u.text).length, 0, "Late start response must not speak");
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/stop");
c.requests.at(-1).resolve(snapshot({active:false, generation:2, speech_queue:[]}));
await settle();
assert.equal(c.elements["lc-global"].hidden, true);
""")


def test_controller_background_status_heartbeat_and_pagehide_stop() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
await startController(c);
c.run("languageChat.setVisible(false)");
assert.equal(c.elements["lc-global"].hidden, false);
c.tick(0);
const poll = c.requests.at(-1);
assert.equal(poll.url, "/ws_collab/language-chat");
poll.resolve(snapshot());
await settle();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/heartbeat");
c.requests.at(-1).resolve({ok:true});
await settle();
c.listeners.pagehide[0]();
const stop = c.requests.at(-1);
assert.equal(stop.url, "/ws_collab/language-chat/stop");
assert.equal(stop.options.keepalive, true);
assert.equal(JSON.parse(stop.options.body).client_id, "owner");
assert.ok(c.synth.cancelled > 0);
stop.resolve(snapshot({active:false}));
await settle();
""")


def test_controller_renews_owner_when_browser_speech_is_disarmed() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({speech_queue:[]}));
await settle();
assert.equal(c.synth.spoken.length, 0);
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/heartbeat");
assert.deepEqual(JSON.parse(c.requests.at(-1).options.body), {client_id:"owner",speech_state:"idle"});
c.requests.at(-1).resolve({ok:true});
await settle();
""")


def test_controller_half_duplex_waits_for_server_gate_not_tts_estimate_or_onend() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
const replying = snapshot({
  input_accepting:false, input_gate_reason:"model_and_playback", suppressed_input_count:3,
  speech_queue:[snapshot().speech_queue[0]], phase:"responding",
});
await startController(c, replying);
c.elements["lc-text"].value = "Next question";
c.elements["lc-text"].fire("input");
assert.equal(c.elements["lc-input-gate"].textContent, "Agent replying — waiting for playback");
assert.match(c.elements["lc-input-detail"].textContent, /Suppressed input: 3/);
assert.equal(c.elements["lc-send"].disabled, true);
assert.equal(c.elements["lc-send-now"].disabled, true);
assert.equal(c.elements["lc-interrupt"].disabled, false);
assert.match(c.elements["lc-speech-estimate"].textContent, /Approximate speech duration/);
assert.match(c.elements["lc-speech-estimate"].textContent, /Not a completion signal/);
const utterance = c.synth.spoken.find(u => u.text);
utterance.onstart();
await settle();
c.requests.at(-1).resolve({ok:true});
await settle();
c.setMonotonic(1750);
utterance.onend({elapsedTime:.75});
await settle();
const done = c.requests.at(-1);
assert.equal(done.url, "/ws_collab/language-chat/heartbeat");
assert.equal(JSON.parse(done.options.body).duration_ms, 750);
done.resolve({ok:true});
await settle();
assert.equal(c.elements["lc-send"].disabled, true, "Local onend does not bypass model completion or the echo tail");
assert.equal(c.requests.some(r => r.url.endsWith("/interrupt")), false, "No automatic voice barge-in");
c.tick(0);
c.requests.at(-1).resolve({...replying, speech_queue:[], input_gate_reason:"echo_tail"});
await settle();
assert.equal(c.elements["lc-speech-estimate"].hidden, true);
assert.equal(c.elements["lc-input-gate"].textContent, "Agent replying — waiting for playback");
assert.match(c.elements["lc-input-detail"].textContent, /echo_tail/);
c.elements["lc-interrupt"].onclick();
await settle();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/interrupt");
c.requests.at(-1).resolve(snapshot({generation:2,speech_queue:[],input_accepting:true,input_gate_reason:"ready"}));
await settle();
assert.equal(c.elements["lc-input-gate"].textContent, "Ready for your speech");
assert.equal(c.elements["lc-send"].disabled, false);
""")


def test_controller_missing_input_gate_fails_closed_and_timing_nulls_stay_unknown() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({
  input_accepting:undefined, speech_queue:[], turn_timings:[{
    message_id:"<turn>", agent_id:"language-chat", input_source:"<STT>", status:"thinking",
    input_received_at:1789174923, request_started_at:1789174924,
    emullm_first_token_ms:null, emullm_total_ms:null, elapsed_ms:61000,
  }],
}));
await settle();
assert.equal(c.elements["lc-input-gate"].textContent, "Input gate unknown");
assert.equal(c.elements["lc-send"].disabled, true);
assert.equal(c.elements["lc-send-now"].disabled, true);
assert.equal(c.elements["lc-timing-status"].textContent, "thinking");
assert.match(c.elements["lc-timing-turn"].textContent, /<turn>/);
assert.match(c.elements["lc-timing-turn"].textContent, /<STT>/);
assert.equal(c.elements["lc-timing-total"].textContent, "Elapsed: 61.0 s · Total: Pending");
const rows = c.elements["lc-timing-stages"].children;
assert.equal(rows.length, 8);
assert.equal(rows.find(r => r.children[0].textContent === "emullm response total").children[1].textContent, "Waiting for measurement");
assert.equal(rows.find(r => r.children[0].textContent === "Speech → STT").children[1].textContent, "Not measured");
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/heartbeat");
c.requests.at(-1).resolve({ok:true});
await settle();
""")


def test_controller_inactive_hidden_page_does_not_keep_polling() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
c.run("languageChat.setVisible(false)");
assert.equal(c.timers.size, 0, "No background loop for an inactive hidden page");
""")


def test_controller_saves_settings_and_replaces_history_for_loaded_agent() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
c.elements["lc-prompt"].value = "Teach me Spanish";
c.elements["lc-settings"].listeners.input[0]({target:c.elements["lc-prompt"]});
c.elements["lc-settings"].onsubmit({preventDefault() {}});
await settle();
const save = c.requests.at(-1);
assert.equal(save.url, "/ws_collab/language-chat/config");
const settings = JSON.parse(save.options.body);
assert.equal(settings.system_prompt, "Teach me Spanish");
assert.equal(settings.model, "emullm-model");
assert.equal(settings.speak_replies, true);
assert.equal(settings.turn_silence_ms, 1000);
assert.equal(settings.endpoint, "http://127.0.0.1:8801/v1");
assert.equal(settings.voice_uri, "voice-one", "Save automatically persists an installed voice");
assert.equal(c.requests.some(r => r.url.endsWith("/start")), false, "Saving settings is not consent to enable monitoring");
assert.equal(c.synth.spoken.length, 0, "Saving does not unlock or start browser speech");
assert.equal(Object.keys(settings).some(k => /secret|token$/.test(k) && k !== "max_tokens"), false);
save.resolve(snapshot({active:false, config:settings, speech_queue:[]}));
await settle();
c.elements["lc-history"].value = '[{"role":"user","content":"Hola"}]';
c.elements["lc-history"].fire("input");
c.elements["lc-history-form"].onsubmit({preventDefault() {}});
await settle();
const history = c.requests.at(-1);
assert.equal(history.url, "/ws_collab/language-chat/history");
assert.deepEqual(JSON.parse(history.options.body), {agent_id:"language-chat", messages:[{role:"user",content:"Hola"}]});
history.resolve(snapshot({active:false, config:settings, speech_queue:[], messages:[{role:"user",content:"Hola"}]}));
await settle();
assert.match(c.elements["lc-history-result"].textContent, /Audit log retained/);
""")


def test_agent_voice_is_visible_but_excluded_from_editable_and_saved_model_history() -> None:
    run_node(r"""
const c = controller();
const messages = [
  {id:"u1",role:"user",content:"Question",created_at:1789174923},
  {id:"a1",role:"assistant",content:"Model answer",created_at:1789174924},
  {id:"v1",role:"assistant",channel:"agent_voice",source_agent_id:"copilot",content:"Operator announcement",created_at:1789174925},
];
c.requests[0].resolve(snapshot({active:false,speech_queue:[],messages}));
await settle();
const history = JSON.parse(c.elements["lc-history"].value);
assert.deepEqual(history, [{role:"user",content:"Question"},{role:"assistant",content:"Model answer"}]);
const log = c.elements["lc-messages"];
assert.equal(log.children.length, 3);
assert.equal(log.children[2].children[0].children[0].textContent, "copilot");
assert.equal(log.children[2].children[1].textContent, "Operator announcement");
c.elements["lc-history-form"].onsubmit({preventDefault() {}});
await settle();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/history");
assert.deepEqual(JSON.parse(c.requests.at(-1).options.body).messages, history);
c.requests.at(-1).resolve(snapshot({active:false,speech_queue:[],messages}));
await settle();
assert.equal(c.synth.spoken.length, 0);
assert.equal(c.requests.some(r => /\/(start|send|send-now|agent-speech)$/.test(r.url)), false);
""")


def test_controller_preview_models_and_foreground_use_canonical_safe_routes() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
const refreshing = c.elements["lc-refresh-models"].onclick();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/models");
c.requests.at(-1).resolve({endpoint:"http://127.0.0.1:8801/v1", models:[{id:"emullm-special"}]});
await refreshing;
assert.equal(c.elements["lc-models"].children[0].value, "emullm-special");
assert.equal(c.elements["lc-model"].value, "emullm-model", "Refreshing never silently changes models");
const previewing = c.elements["lc-preview"].onclick();
assert.equal(c.requests.at(-1).url, "/ws_collab/language-chat/request-preview");
const payload = {model:"emullm-model", messages:[{role:"user",content:"<script>not HTML</script>"}], stream:true, max_tokens:512};
c.requests.at(-1).resolve(payload);
await previewing;
assert.deepEqual(JSON.parse(c.elements["lc-request"].textContent), payload);
const foregrounding = c.elements["lc-captioner"].onclick();
assert.equal(c.requests.at(-1).url, "/ws_collab/captioner/foreground");
assert.equal(c.requests.at(-1).options.method, "POST");
assert.deepEqual(JSON.parse(c.requests.at(-1).options.body), {});
c.requests.at(-1).resolve({ok:true});
await foregrounding;
""")


def test_controller_stream_render_respects_scroll_position_and_composer_edits() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
await startController(c, snapshot({speech_queue:[]}));
const log = c.elements["lc-messages"];
log.scrollHeight = 1000; log.clientHeight = 100; log.scrollTop = 200;
log.fire("scroll");
c.elements["lc-text"].value = "Original message";
c.elements["lc-text"].fire("input");
c.elements["lc-composer"].onsubmit({preventDefault() {}});
await settle();
const sending = c.requests.at(-1);
assert.equal(sending.url, "/ws_collab/language-chat/send");
assert.equal(JSON.parse(sending.options.body).text, "Original message");
c.elements["lc-text"].value = "Next draft";
sending.resolve(snapshot({speech_queue:[], messages:[{id:"m1",role:"assistant",content:"<b>streamed</b>",status:"streaming"}]}));
await settle();
assert.equal(c.elements["lc-text"].value, "Next draft");
assert.equal(log.scrollTop, 200);
assert.equal(log.children[0].children[1].textContent, "<b>streamed</b>");
assert.equal(c.elements["lc-latest"].hidden, false);
c.elements["lc-latest"].onclick();
assert.equal(log.scrollTop, 1000);
""")


def test_controller_network_loss_cancels_tts_and_does_not_resume_on_recovery() -> None:
    run_node(r"""
const c = controller();
c.requests[0].resolve(snapshot({active:false, speech_queue:[]}));
await settle();
await startController(c);
assert.equal(c.synth.spoken.filter(u => u.text).length, 1);
c.tick(0);
c.requests.at(-1).reject(new Error("Disconnected"));
await settle();
assert.match(c.elements["lc-phase"].textContent, /Unknown/);
assert.match(c.elements["lc-tts"].textContent, /unknown/);
assert.ok(c.synth.cancelled > 0);
c.tick(450);
c.requests.at(-1).resolve(snapshot());
await settle();
assert.equal(c.synth.spoken.filter(u => u.text).length, 1, "Do not resume stale queued speech");
assert.equal(c.elements["lc-start"].disabled, false, "Explicit recovery gesture is available");
""")


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "id" in attributes:
            self.by_id[str(attributes["id"])] = attributes


@pytest.mark.parametrize(
    ("control", "label", "tooltip"),
    [
        ("lc-start", "Start voice chat", "Opt in once: automatically monitor finalized non-echo STT"),
        ("lc-stop", "Stop", "Stop LLM turns and cancel browser speech immediately"),
        ("lc-interrupt", "Interrupt reply", "Cancel the current generated reply"),
        ("lc-send-now", "Send now", "Submit the pending spoken turn now"),
        ("lc-save-settings", "Save agent settings", "Save this agent's model"),
        ("lc-save-history", "Save history", "Replace this agent's editable conversation context"),
        ("lc-preview", "Inspect exact next submitted request", "Fetch the next emullm request payload without credentials"),
    ],
)
def test_exact_control_labels_and_explanatory_tooltips(control: str, label: str, tooltip: str) -> None:
    html = INDEX.read_text(encoding="utf-8")
    controls = Controls()
    controls.feed(html)
    assert tooltip in str(controls.by_id[control]["title"])
    assert f">{label}</button>" in html.split(f'id="{control}"', 1)[1].split("</button>", 1)[0] + "</button>"


def test_route_assets_transparency_and_no_second_microphone() -> None:
    html = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    controller = app.split("/* ----------------------------------------------------------- language chat */", 1)[1].split(
        "/* ------------------------------------------------------ persistent UI state */", 1
    )[0]
    page = html.split('<section class="page" data-page="chatbot-test"', 1)[1].split("</section>", 1)[0]
    assert 'data-page="chatbot-test"' in html
    assert '<h2>ChatBot Test</h2>' in page
    assert 'href="#chatbot-test"' in html
    assert '"chatbot-test": loadLanguageChat' in app
    assert '"chatbot-test": "ChatBot Test"' in app
    assert '"language-chat": "chatbot-test"' in app
    assert html.index('src="language_chat_runtime.js"') < html.index('src="app.js"')
    assert 'languageChat.setVisible(page === "chatbot-test")' in app
    assert 'if (page === "chatbot-test") return;' in app
    assert "http://127.0.0.1:8801/v1" in page
    assert 'value="emullm/default"' in page
    assert "automatic monitoring of the existing STT pipeline" in page
    assert "no per-turn Send is required" in page
    assert "no manual voice setup is needed" in page
    assert "including Chrome Captions and other enabled STT sources" in page
    assert "<strong>Half-duplex:</strong>" in page
    assert "entire browser TTS queue" in page
    assert "There is no automatic voice barge-in" in page
    assert "Shared STT/VAD continues independently" in page
    assert 'id="lc-timing-stages"' in page
    assert "Not measured means unknown, not zero" in page
    assert "input_accepting === true" in controller
    assert 'const staleAfterMs = 6000;' in runtime
    heartbeat = runtime.split("heartbeat() {", 1)[1].split("},", 1)[0]
    assert "if (owns())" in heartbeat
    assert "armed" not in heartbeat
    poll_heartbeat = controller.split('if (accept(payload, ticket)', 1)[1].split("} catch", 1)[0]
    assert "playback.armed" not in poll_heartbeat
    assert "/captioner/foreground" in controller
    assert "getVoices()" in controller
    assert "voiceschanged" in controller
    assert "sessionStorage" in controller
    assert "external emullm agent permissions govern its actions" in page
    assert "not the append-only audit log" in page
    assert "Use headphones" in page
    assert "Echo cancellation is not a guarantee" in page
    assert 'id="lc-mute"' in page
    for forbidden in ("getUserMedia", "SpeechRecognition", "webkitSpeechRecognition", "MediaRecorder", "innerHTML", "/tts/", "/captioner/open", "/caption-sources/"):
        assert forbidden not in controller + runtime
    for path in ("start", "stop", "interrupt", "send", "send-now", "config", "history"):
        assert f'command("{path}"' in controller
    for path in ("models", "request-preview", "heartbeat"):
        assert f'"/language-chat/{path}"' in controller


def test_javascript_syntax() -> None:
    for source in (RUNTIME, APP):
        result = subprocess.run(["node", "--check", str(source)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
