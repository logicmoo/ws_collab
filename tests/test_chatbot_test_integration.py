"""ChatBot Test routing and its subscription to the shared STT pipeline."""

import asyncio
from pathlib import Path

import pytest

from ws_collab.events import STT_FINAL_RESULT
from test_captioner_silence_display import run_node


def test_chatbot_configuration_registers_agent_and_previews_context(
    client, app_context, admin_headers, viewer_headers, worker_headers
):
    endpoint = "/ws_collab/language-chat"
    config = {
        "agent_id": "spoken-test",
        "endpoint": "http://127.0.0.1:8801/v1",
        "model": "emullm/default",
        "system_prompt": "Be a concise spoken learning assistant.",
        "voice_uri": "test-browser-voice",
    }
    assert client.post(endpoint + "/config", headers=worker_headers, json=config).status_code == 403
    response = client.post(endpoint + "/config", headers=admin_headers, json=config)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["config"]["agent_id"] == "spoken-test"
    assert state["config"]["speak_replies"] is True
    worker = next(row for row in app_context.service.list_workers()["workers"] if row["worker_id"] == "spoken-test")
    assert worker["meta"]["client_type"] == "chatbot-test"
    assert worker["meta"]["voice_uri"] == "test-browser-voice"
    history = [
        {"role": "user", "content": "What did we discuss?"},
        {"role": "assistant", "content": "We discussed listening."},
    ]
    response = client.post(endpoint + "/history", headers=admin_headers,
                           json={"agent_id": "spoken-test", "messages": history})
    assert response.status_code == 200, response.text
    preview = client.get(endpoint + "/request-preview", headers=viewer_headers)
    assert preview.status_code == 200, preview.text
    request = preview.json()
    assert request["model"] == "emullm/default"
    assert request["messages"][0] == {"role": "system", "content": config["system_prompt"]}
    assert request["messages"][1:] == history
    assert "authorization" not in str(request).lower()


def test_chatbot_actions_reject_unknown_fields_and_worker_control(
    client, admin_headers, worker_headers
):
    endpoint = "/ws_collab/language-chat"
    for action in ("start", "stop", "interrupt", "send", "send-now", "heartbeat"):
        response = client.post(endpoint + "/" + action, headers=worker_headers,
                               json={"client_id": "client", "text": "hello"})
        assert response.status_code == 403
    response = client.post(endpoint + "/start", headers=admin_headers,
                           json={"client_id": "client", "override_policy": True})
    assert response.status_code == 400


def test_chatbot_monitors_finalized_stt_from_any_engine(service, monkeypatch):
    heard = []
    monkeypatch.setattr(service.language_chat, "on_caption",
                        lambda *args, **kwargs: heard.append((args, kwargs)))
    service.ingest_transcript(engine="external-one", text="First microphone phrase")
    service.ingest_transcript(engine="external-two", text="Another STT source")
    assert [args[0] for args, _ in heard] == ["First microphone phrase", "Another STT source"]
    assert all(kwargs["is_echo"] is False for _, kwargs in heard)
    assert heard[0][0][1] != heard[1][0][1]
    service.ingest_transcript(engine="external-one", text="Unfinished words", is_final=False)
    assert len(heard) == 2
    assert any(event.type == STT_FINAL_RESULT for event in service.store.tail("stt_transcripts", 20))


def test_chatbot_browser_tts_participates_in_existing_echo_filter(service, monkeypatch):
    received = []
    monkeypatch.setattr(service.language_chat, "active_tts_context", lambda: [{
        "tts_event_id": "browser-speech", "expected_text": "This is the spoken model response",
    }])
    monkeypatch.setattr(service.language_chat, "on_caption",
                        lambda *args, **kwargs: received.append((args, kwargs)))
    result = service.ingest_transcript(engine="external", text="This is the spoken model response")
    assert result["classification"]["is_echo"] is True
    assert result["classification"]["matched_tts_event_id"] == "browser-speech"
    assert received[-1][1]["is_echo"] is True


def test_chatbot_models_use_the_configured_provider(client, app_context, admin_headers, monkeypatch):
    async def models():
        return {"endpoint": "http://127.0.0.1:8801/v1", "models": [{"id": "emullm/default"}]}

    monkeypatch.setattr(app_context.service.language_chat, "models", models)
    result = client.get("/ws_collab/language-chat/models", headers=admin_headers)
    assert result.status_code == 200
    assert result.json()["models"] == [{"id": "emullm/default"}]


def test_interrupt_before_first_token_does_not_stop_chat(service, monkeypatch):
    async def scenario():
        monkeypatch.setattr(service.language_chat, "_launch", lambda *_args: None)
        await service.language_chat.startup()
        try:
            service.start_language_chat("client")
            service.language_chat.submit_text("client", "An unanswered question")
            service.language_chat.interrupt("client")
            state = service.language_chat.state()
            assert state["active"] is True
            assert state["error"] is None
            messages = service.store.tail("conversation", 20)
            assert all(event.data["text"].strip() for event in messages)
            assert any(event.type == "LANGUAGE_CHAT_MESSAGE_STATUS"
                       for event in service.store.tail("system_audit", 30))
        finally:
            await service.language_chat.shutdown()

    asyncio.run(scenario())


def test_chat_shutdown_error_still_closes_audio_and_monitoring(service, monkeypatch):
    cleaned = []

    async def failing_shutdown():
        raise RuntimeError("Chat state cannot be saved")

    async def stop_tts():
        cleaned.append("tts")

    monkeypatch.setattr(service.language_chat, "shutdown", failing_shutdown)
    monkeypatch.setattr(service.tts, "stop", stop_tts)
    monkeypatch.setattr(service.captioner_supervisor, "shutdown", lambda: cleaned.append("captioner"))
    with pytest.raises(RuntimeError, match="Chat state"):
        asyncio.run(service.shutdown())
    assert cleaned == ["captioner", "tts"]


def test_requested_chat_page_is_shown_before_large_transcript_backfill():
    script = (Path(__file__).parents[1] / "src" / "ws_collab" / "admin" / "app.js").read_text(encoding="utf-8")
    boot = script.split("async function boot() {", 1)[1].split("async function signIn", 1)[0]
    assert boot.index("showPage(pageFromHash())") < boot.index("await backfill(")


def test_rest_fallback_leaves_connections_free_and_aborts_on_websocket_recovery():
    run_node(r"""
const assert=require("node:assert/strict");
const fs=require("node:fs"), path=require("node:path"), vm=require("node:vm");
const source=fs.readFileSync(path.join(path.dirname(process.argv[1]),"app.js"),"utf8");
const start=source.indexOf("function startRestFallback()");
const end=source.indexOf("/* ------------------------------------------------------- virtualized stream view */",start);
assert.ok(start>=0 && end>start);
const state={restTimers:{},restControllers:new Map(),restInFlight:0,restGeneration:0,wsReady:false,cursors:{}};
const timers=new Map(), requests=[], errors=[];
let id=0;
const context={
  state,STREAMS:["a","b","c","d","e","f","g"],location:{protocol:"http:"},API_BASE:"/ws_collab",
  setTransport(){},pushError:error=>errors.push(error),logout(){throw new Error("unexpected logout");},
  URLSearchParams,AbortController,
  setTimeout:(callback)=>{timers.set(++id,callback);return id;},clearTimeout:key=>timers.delete(key),
  api:(url,{signal})=>new Promise((resolve,reject)=>{
    requests.push({url,signal,resolve});
    signal.addEventListener("abort",()=>reject(Object.assign(new Error("cancelled"),{name:"AbortError"})));
  }),
};
vm.createContext(context);
vm.runInContext(source.slice(start,end),context);
(async()=>{
  context.startRestFallback();
  const initial=[...timers.values()];
  const running=initial.map(callback=>callback());
  assert.equal(requests.length,2,"long polls must not consume all browser connections");
  assert.equal(state.restInFlight,2);
  assert.ok(requests.every(request=>request.url.includes("wait_ms=1000")));
  state.wsReady=true;
  context.stopRestFallback();
  assert.ok(requests.every(request=>request.signal.aborted));
  await Promise.all(running);
  assert.equal(state.restInFlight,0);
  assert.equal(state.restControllers.size,0);
  initial.forEach(callback=>callback());
  assert.equal(requests.length,2,"old poll generations cannot restart after recovery");
  assert.deepEqual(errors,[]);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_active_chat_resume_does_not_resubmit_locked_configuration():
    script = (Path(__file__).parents[1] / "src" / "ws_collab" / "admin" / "app.js").read_text(encoding="utf-8")
    start = script.split('$("lc-start").onclick =', 1)[1].split('$("lc-stop").onclick', 1)[0]
    assert start.index("if (latest.active) return;") < start.index("await autoVoice(")


def test_registered_worker_can_speak_through_the_monitored_chat_channel(
    client, app_context, admin_headers, worker_headers, viewer_headers
):
    endpoint = "/ws_collab/language-chat"
    payload = {"agent_id": "copilot", "text": "The updated chat controls are ready."}
    assert client.post(endpoint + "/agent-speech", headers=viewer_headers, json=payload).status_code == 403
    started = client.post(endpoint + "/start", headers=admin_headers, json={"client_id": "browser-test"})
    assert started.status_code == 200
    spoken = client.post(endpoint + "/agent-speech", headers=worker_headers, json=payload)
    assert spoken.status_code == 200, spoken.text
    state = spoken.json()
    assert state["queued"] is True and state["input_accepting"] is False
    assert state["messages"][-1]["source_agent_id"] == "copilot"
    assert state["speech_queue"][-1]["source_agent_id"] == "copilot"
    preview = client.get(endpoint + "/request-preview", headers=viewer_headers).json()
    assert len(preview["messages"]) == 1
    assert any(worker["worker_id"] == "copilot" for worker in app_context.service.list_workers()["workers"])
    invalid = client.post(endpoint + "/agent-speech", headers=worker_headers,
                          json={**payload, "unrecognized": True})
    assert invalid.status_code == 400
    assert client.post(endpoint + "/stop", headers=admin_headers, json={"client_id": "browser-test"}).status_code == 200
