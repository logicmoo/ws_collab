from __future__ import annotations

import subprocess
from pathlib import Path


def test_lifecycle_buttons_confirm_serialize_recover_and_bound_polling():
    app = Path(__file__).resolve().parents[1] / "src" / "ws_collab" / "admin" / "app.js"
    script = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync(process.argv[1], "utf8");
const controller = source.slice(source.indexOf("let lifecyclePending = false;"), source.indexOf("/* --------------------------------------------------------------- UI helpers */"));
function fixture(confirmed = true) {
  const elements = Object.fromEntries(["sy-restart", "sy-shutdown", "server-restart", "server-shutdown", "sy-lifecycle-result", "server-lifecycle-result"].map(id => [id, {disabled:false, hidden:true}]));
  const requests = [], timers = new Map(), warnings = [];
  let id = 0, time = 0, reloads = 0;
  const pending = (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
  const sandbox = {
    $: id => elements[id], API_BASE:"/custom", AbortController,
    location:{host:"localhost:9999", reload:() => reloads++},
    performance:{now:() => time},
    confirm: warning => { warnings.push(warning); return confirmed; },
    api:pending, fetch:pending,
    setTimeout(fn, delay) { timers.set(++id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(sandbox); vm.runInContext(controller, sandbox);
  return {
    elements, requests, warnings, timers,
    run: code => vm.runInContext(code, sandbox),
    reloads:() => reloads,
    tick(delay) {
      const entry = [...timers].find(([, v]) => v.delay === delay);
      assert.ok(entry, `missing ${delay}ms timer`);
      timers.delete(entry[0]); time += delay; return entry[1].fn();
    },
    advance: ms => { time += ms; },
  };
}
function allDisabled(f, expected) {
  for (const id of ["sy-restart", "sy-shutdown", "server-restart", "server-shutdown"]) assert.equal(f.elements[id].disabled, expected, id);
}
(async () => {
  const cancelled = fixture(false);
  await cancelled.run("requestLifecycle('shutdown')");
  assert.equal(cancelled.requests.length, 0);
  allDisabled(cancelled, false);
  assert.match(cancelled.warnings[0], /cannot start a stopped server/);

  const f = fixture();
  const action = f.run("requestLifecycle('restart')");
  allDisabled(f, true);
  await f.run("requestLifecycle('shutdown')");
  assert.equal(f.requests.length, 1, "duplicate click is ignored");
  assert.equal(f.requests[0].url, "/custom/admin/restart");
  f.requests[0].resolve({action:"restart", scheduled:true, boot_id:"old", status:"scheduled", pid:123});
  await action;
  const oldPoll = f.tick(500);
  assert.equal(f.requests.at(-1).url, "/custom/status");
  assert.equal(f.requests.at(-1).options.cache, "no-store");
  f.requests.at(-1).resolve({ok:true, json:async () => ({boot_id:"old"})});
  await oldPoll;
  assert.equal(f.reloads(), 0);
  const newPoll = f.tick(500);
  f.requests.at(-1).resolve({ok:true, json:async () => ({boot_id:"new"})});
  await newPoll;
  assert.equal(f.reloads(), 1);
  assert.equal(f.elements["sy-lifecycle-result"].textContent, f.elements["server-lifecycle-result"].textContent);

  const failed = fixture();
  const failure = failed.run("requestLifecycle('restart')");
  failed.requests[0].reject(new Error("operator permission required"));
  await failure;
  allDisabled(failed, false);
  assert.match(failed.elements["server-lifecycle-result"].textContent, /operator permission required/);

  const timeout = fixture();
  const request = timeout.run("requestLifecycle('restart')");
  timeout.tick(8000);
  assert.equal(timeout.requests[0].options.signal.aborted, true);
  timeout.requests[0].reject(Object.assign(new Error("aborted"), {name:"AbortError"}));
  await request;
  allDisabled(timeout, false);
  assert.match(timeout.elements["server-lifecycle-result"].textContent, /may have been accepted/);

  const pollTimeout = fixture();
  pollTimeout.run("lifecycleControls(true); pollForRestart('old')");
  const poll = pollTimeout.tick(500);
  pollTimeout.tick(2500);
  assert.equal(pollTimeout.requests[0].options.signal.aborted, true);
  pollTimeout.advance(60000);
  pollTimeout.requests[0].reject(Object.assign(new Error("aborted"), {name:"AbortError"}));
  await poll;
  allDisabled(pollTimeout, false);
  assert.match(pollTimeout.elements["server-lifecycle-result"].textContent, /within 60 seconds/);
  assert.equal(pollTimeout.timers.size, 0);

  const shutdown = fixture();
  const stop = shutdown.run("requestLifecycle('shutdown')");
  shutdown.requests[0].resolve({action:"shutdown", scheduled:true});
  await stop;
  allDisabled(shutdown, true);
  assert.equal(shutdown.timers.size, 0, "shutdown never schedules a restart or another POST");
  assert.match(shutdown.elements["server-lifecycle-result"].textContent, /plugin.start_server/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
