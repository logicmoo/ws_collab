from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

from ws_collab import language_chat as module
from ws_collab.errors import ConfigurationError, ConflictError, ValidationError, WsCollabError
from ws_collab.language_chat import LanguageChat


class Clock:
    def __init__(self):
        self.now = 1_750_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def event(content=None, finish=None):
    delta = {} if content is None else {"content": content}
    return ("data: " + json.dumps({"choices": [
        {"index": 0, "delta": delta, "finish_reason": finish},
    ]}, ensure_ascii=False) + "\n\n").encode()


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, gate=None, ignore_cancel=False):
        self.chunks = chunks
        self.gate = gate
        self.ignore_cancel = ignore_cancel
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index == 1 and self.gate:
                self.waiting.set()
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
                    await self.gate.wait()
            yield chunk

    async def aclose(self):
        self.closed = True


class Harness:
    def __init__(self, directory, response=None, **settings):
        self.clock = Clock()
        self.microphone = {"available": True, "state": "speech", "current_silence_ms": 0, "error": None}
        self.published = []
        self.audits = []
        self.requests = []
        self.stream = Stream([event("Hello there. "), event("Let's learn!"), event(finish="stop")])
        self.response = response
        self.options = []
        self.chat = LanguageChat(
            directory, microphone_state=lambda: dict(self.microphone),
            publish_message=lambda *args: self.published.append(args),
            audit=lambda *args: self.audits.append(args),
            client_factory=self.client, clock=self.clock,
        )
        self.chat.configure({"model": "test-model", **settings})

    def client(self, **options):
        self.options.append(options)

        def handler(request):
            self.requests.append(request)
            if self.response:
                return self.response(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **options)

    def silence(self, milliseconds=1500):
        self.microphone.update(state="silence", current_silence_ms=milliseconds)


@asynccontextmanager
async def running(directory, **kwargs):
    harness = Harness(directory, **kwargs)
    await harness.chat.startup()
    harness.chat.start("browser-one")
    try:
        yield harness
    finally:
        await harness.chat.shutdown()


async def finished(chat):
    for _ in range(200):
        if not any(m["status"] == "streaming" for m in chat.state()["messages"]):
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.005)
    pytest.fail("Model task did not finish")


def test_stream_chunk_boundary_does_not_split_decimal_speech(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.stream = Stream([event("Pi is 3."), event("14"), event(". "), event("Correct."), event(finish="stop")])
            h.chat.submit_text("browser-one", "Tell me pi")
            await finished(h.chat)
            state = h.chat.state()
            assert state["messages"][-1]["content"] == "Pi is 3.14. Correct."
            assert [item["text"] for item in state["speech_queue"]] == ["Pi is 3.14.", "Correct."]
    asyncio.run(scenario())


def test_muting_active_agent_cancels_speech_without_stopping_chat(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            speech = h.chat.state()["speech_queue"][0]
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": speech["id"]})
            generation = h.chat.state()["generation"]
            h.chat.configure({"agent_id": h.chat.config()["agent_id"], "speak_replies": False})
            state = h.chat.state()
            assert state["active"] is True
            assert state["generation"] == generation
            assert all(item["status"] == "cancelled" for item in state["speech_queue"])
            h.chat.configure({"speak_replies": True})
            assert h.chat.config()["speak_replies"] is True
            assert all(item["status"] == "cancelled" for item in h.chat.state()["speech_queue"])
            with pytest.raises(ConflictError):
                h.chat.configure({"agent_id": "other-agent", "speak_replies": False})
    asyncio.run(scenario())


def test_off_by_default_and_no_requests_before_explicit_start(tmp_path):
    async def scenario():
        h = Harness(tmp_path)
        assert h.chat.config()["speak_replies"] is True
        assert h.chat.state()["active"] is False
        assert h.chat.on_caption("ambient backlog", "old", h.clock()) is False
        await h.chat.startup()
        await h.chat.startup()
        h.chat._tick_once()
        await asyncio.sleep(0)
        assert not h.requests
        with pytest.raises(ConflictError, match="Start"):
            h.chat.submit_text("browser-one", "not started")
        h.chat.start("browser-one")
        h.chat.submit_text("browser-one", "hello")
        await finished(h.chat)
        await h.chat.shutdown()
        restored = Harness(tmp_path)
        state = restored.chat.state()
        assert state["active"] is False
        assert state["client_id"] is None
        assert state["speech_queue"] == []
        assert state["pending_text"] == ""
        assert state["messages"][-1]["content"] == "Hello there. Let's learn!"
        assert not restored.requests
    asyncio.run(scenario())


def test_default_model_existing_stt_auto_turn_and_auto_selected_voice_persistence(tmp_path):
    async def scenario():
        clock = Clock()
        requests = []
        microphone = {"available": True, "state": "speech", "current_silence_ms": 0}

        def provider(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{
                "message": {"role": "assistant", "content": "A spoken reply."},
                "finish_reason": "stop",
            }]})

        def make_chat():
            return LanguageChat(
                tmp_path, microphone_state=lambda: dict(microphone),
                publish_message=lambda *args: None, audit=lambda *args: None, clock=clock,
                client_factory=lambda **options: httpx.AsyncClient(
                    transport=httpx.MockTransport(provider), **options,
                ),
            )

        chat = make_chat()
        assert chat.config()["model"] == "emullm/default"
        assert chat.config()["voice_uri"] == ""
        assert chat.config()["speak_replies"]
        assert not chat.state()["active"]
        await chat.startup()
        try:
            # This models the UI's automatic voice binding, not a manual setting.
            chat.configure({"voice_uri": "installed-system-default-voice"})
            chat.start("browser-one")
            assert not requests
            assert chat.on_caption("Finalized by the existing STT pipeline", "stt-final", clock())
            microphone.update(state="silence", current_silence_ms=1500)
            clock.advance(1)
            chat._tick_once()
            await finished(chat)
            assert len(requests) == 1
            payload = json.loads(requests[0].content)
            assert payload["model"] == "emullm/default"
            assert payload["messages"][-1]["content"] == "Finalized by the existing STT pipeline"
            assert chat.state()["speech_queue"][0]["text"] == "A spoken reply."
        finally:
            await chat.shutdown()
        restored = make_chat()
        assert restored.config()["voice_uri"] == "installed-system-default-voice"
        assert restored.config()["model"] == "emullm/default"
        assert not restored.state()["active"]
        restored.configure({"agent_id": "another-test-agent"})
        assert restored.config()["model"] == "emullm/default"
        assert restored.config()["voice_uri"] == ""
        restored.configure({"agent_id": "language-chat"})
        assert restored.config()["voice_uri"] == "installed-system-default-voice"
    asyncio.run(scenario())


def test_streaming_exact_context_sentence_queue_and_completion_persistence(tmp_path):
    async def scenario():
        h = Harness(tmp_path, system_prompt="Teach carefully.", max_tokens=128, history_limit=2)
        h.chat.set_history({"agent_id": "language-chat", "messages": [
            {"role": "user", "content": "discarded"},
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ]})
        gate = asyncio.Event()
        h.stream = Stream([event("First sentence. "), event("Second sentence! tail"), event(finish="stop")], gate)
        await h.chat.startup()
        h.chat.start("browser-one")
        try:
            h.chat.submit_text("browser-one", "new question")
            await h.stream.waiting.wait()
            live = h.chat.state()
            assert live["messages"][-1]["status"] == "streaming"
            assert live["messages"][-1]["content"] == "First sentence. "
            assert [q["text"] for q in live["speech_queue"]] == ["First sentence."]
            payload = json.loads(h.requests[-1].content)
            assert payload == {
                "model": "test-model", "max_tokens": 128, "stream": True,
                "messages": [
                    {"role": "system", "content": "Teach carefully."},
                    {"role": "user", "content": "prior question"},
                    {"role": "assistant", "content": "prior answer"},
                    {"role": "user", "content": "new question"},
                ],
            }
            assert live["next_request"] == payload
            assert h.chat.request_preview() == payload
            gate.set()
            await finished(h.chat)
            final = h.chat.state()
            assert final["messages"][-1]["content"] == "First sentence. Second sentence! tail"
            assert final["messages"][-1]["status"] == "complete"
            assert [q["text"] for q in final["speech_queue"]] == ["First sentence.", "Second sentence!", "tail"]
            assert len({q["id"] for q in final["speech_queue"]}) == 3
            assert h.chat.state()["speech_queue"] == final["speech_queue"]
            assert [row[2] for row in h.published] == ["user", "assistant"]
            assert all(row[0] == "language-chat" and row[1] == final["session_id"] for row in h.published)
            assert h.stream.closed
            saved = json.loads(h.chat.path.read_text(encoding="utf-8"))
            assert saved["agents"]["language-chat"]["messages"][-1]["status"] == "complete"
        finally:
            gate.set()
            await h.chat.shutdown()
    asyncio.run(scenario())


def test_per_agent_history_edit_is_not_canonical_and_survives_switch_restart(tmp_path):
    h = Harness(tmp_path, system_prompt="Agent A")
    h.chat.set_history({"agent_id": "language-chat", "messages": [{"role": "user", "content": "A history"}]})
    h.chat.configure({"agent_id": "teacher", "model": "teacher-model", "system_prompt": "Agent B"})
    h.chat.set_history({"agent_id": "teacher", "messages": [{"role": "assistant", "content": "B history"}]})
    assert h.chat.config()["agent_id"] == "teacher"
    assert not h.published
    h.chat.configure({"agent_id": "language-chat"})
    assert h.chat.config()["system_prompt"] == "Agent A"
    assert h.chat.state()["messages"][0]["content"] == "A history"
    restored = Harness(tmp_path)
    restored.chat.configure({"agent_id": "teacher"})
    assert restored.chat.config()["model"] == "teacher-model"
    assert restored.chat.config()["system_prompt"] == "Agent B"
    assert restored.chat.state()["messages"][0]["content"] == "B history"
    restored.chat.set_history({"agent_id": "teacher", "messages": []})
    assert restored.chat.request_preview()["messages"] == [{"role": "system", "content": "Agent B"}]
    assert len(restored.chat.state()["agents"]) == 2
    assert not list(tmp_path.glob("*.pending"))


@pytest.mark.parametrize("settings", [
    {"unknown": 1}, {"token": "secret"}, {"turn_silence_ms": 299}, {"turn_silence_ms": 10001},
    {"turn_silence_ms": True}, {"speech_rate": 0.4}, {"speech_rate": float("nan")},
    {"speech_rate": True}, {"max_tokens": 4097}, {"history_limit": 1}, {"speak_replies": "true"},
    {"agent_id": " "}, {"model": None}, {"language": ""},
    {"endpoint": "file:///models"}, {"endpoint": "http://user:password@localhost/v1"},
    {"endpoint": "http://localhost/v1?token=secret"}, {"endpoint": "http://localhost/v1#secret"},
    {"endpoint": "http://localhost:99999/v1"}, {"system_prompt": "x" * (module.MAX_TEXT + 1)},
])
def test_config_strict_validation_and_no_mutation(tmp_path, settings):
    h = Harness(tmp_path)
    previous = h.chat.config()
    with pytest.raises(ValidationError):
        h.chat.configure(settings)
    assert h.chat.config() == previous


@pytest.mark.parametrize("messages", [
    [{"role": "system", "content": "bad"}], [{"role": "user", "content": ""}],
    [{"role": "assistant", "content": 3}], [{"role": "user", "content": "x", "extra": 1}],
    [{"role": "user", "content": "x"}] * 101,
    [{"role": "user", "content": "x" * module.MAX_OUTPUT}] * 5,
])
def test_history_validation(tmp_path, messages):
    h = Harness(tmp_path)
    with pytest.raises(ValidationError):
        h.chat.set_history({"agent_id": "language-chat", "messages": messages})
    assert h.chat.state()["messages"] == []


def test_old_duplicate_and_explicit_echo_captions_are_not_sent(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            assert not h.chat.on_caption("old", "old", h.clock() - 1)
            assert not h.chat.on_caption("bad date", "bad", "not-a-date")
            assert not h.chat.on_caption("future", "future", h.clock() + 10)
            assert not h.chat.on_caption("echo", "echo", h.clock(), is_echo=True)
            h.clock.advance(0.1)
            iso = datetime.fromtimestamp(h.clock(), tz=timezone.utc).isoformat()
            assert h.chat.on_caption("one", "first", iso)
            assert not h.chat.on_caption("one duplicated", "first", iso)
            assert h.chat.on_caption("two", "second", h.clock() * 1000)
            assert h.chat.state()["pending_text"] == "one two"
            assert h.chat.state()["echo_suppressed_count"] == 1
            assert not h.requests
            h.chat.send_now("browser-one")
            await finished(h.chat)
            assert json.loads(h.requests[0].content)["messages"][-1]["content"] == "one two"
    asyncio.run(scenario())


@pytest.mark.parametrize("microphone", [
    {"available": False, "state": "silence", "current_silence_ms": 2000},
    {"available": True, "state": "speech", "current_silence_ms": 2000},
    {"available": True, "state": "idle", "current_silence_ms": 2000},
    {"available": True, "state": "silence", "current_silence_ms": None},
    {"available": True, "state": "silence", "current_silence_ms": 999},
    {"available": True, "state": "silence", "current_silence_ms": 2000, "stale": True},
    {"available": True, "state": "silence", "current_silence_ms": 2000, "error": "broken"},
])
def test_missing_stale_or_insufficient_vad_never_completes_turn(tmp_path, microphone):
    async def scenario():
        async with running(tmp_path) as h:
            h.microphone = microphone
            h.chat.on_caption("unfinished thought", "utterance", h.clock())
            h.clock.advance(1)
            h.chat._tick_once()
            await asyncio.sleep(0)
            assert not h.requests
            assert h.chat.state()["pending_text"] == "unfinished thought"
    asyncio.run(scenario())


def test_actual_silence_groups_finals_and_observes_settle_window(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.on_caption("first sentence", "one", h.clock())
            h.clock.advance(1)
            h.chat._tick_once()
            assert not h.requests
            h.chat.on_caption("second sentence", "two", h.clock())
            h.silence()
            h.chat._tick_once()
            assert not h.requests
            h.clock.advance(module.SETTLE_SECONDS + 0.01)
            h.chat._tick_once()
            await finished(h.chat)
            assert len(h.requests) == 1
            assert json.loads(h.requests[0].content)["messages"][-1]["content"] == "first sentence second sentence"
    asyncio.run(scenario())


def test_real_background_tick_sends_turn_and_shutdown_removes_tasks(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.on_caption("tick-driven", "one", h.clock())
            h.clock.advance(1)
            h.silence()
            await asyncio.sleep(module.TICK_SECONDS + 0.1)
            await finished(h.chat)
            assert len(h.requests) == 1
        assert h.chat._tick_task is None
        assert not h.chat._tasks
        await h.chat.shutdown()
    asyncio.run(scenario())


def test_manual_submit_does_not_enable_or_require_microphone(tmp_path):
    async def scenario():
        async with running(tmp_path, speak_replies=False) as h:
            h.microphone = {"available": False, "state": "unavailable"}
            h.chat.submit_text("browser-one", "typed input")
            await finished(h.chat)
            assert not h.chat.state()["microphone"]["available"]
            assert not h.chat.state()["speech_queue"]
            assert json.loads(h.requests[0].content)["messages"][-1]["content"] == "typed input"
    asyncio.run(scenario())


def test_explicit_stop_and_interrupt_cancel_close_stream(tmp_path):
    async def scenario():
        for action in ("stop", "interrupt"):
            async with running(tmp_path / action) as h:
                gate = asyncio.Event()
                h.stream = Stream([event("Started reply. "), event("late"), event(finish="stop")], gate)
                h.chat.submit_text("browser-one", "test")
                await h.stream.waiting.wait()
                old_generation = h.chat.state()["generation"]
                if action == "stop":
                    h.chat.stop("browser-one")
                elif action == "interrupt":
                    h.chat.interrupt("browser-one")
                await asyncio.sleep(0.01)
                state = h.chat.state()
                assert state["generation"] > old_generation
                assert state["messages"][-1]["status"] == "interrupted"
                assert state["messages"][-1]["content"] == "Started reply. "
                assert all(q["status"] == "cancelled" for q in state["speech_queue"])
                assert h.stream.closed
                assert h.published[-1][-1] == "interrupted"
    asyncio.run(scenario())


def test_cancelled_generation_rejects_late_deltas_without_harming_next_reply(tmp_path):
    async def scenario():
        async with running(tmp_path, speak_replies=False) as h:
            gate = asyncio.Event()
            first = h.stream = Stream([event("old "), event("must not appear"), event(finish="stop")],
                                      gate, ignore_cancel=True)
            h.chat.submit_text("browser-one", "first")
            await first.waiting.wait()
            generation = h.chat.state()["generation"]
            h.chat.interrupt("browser-one")
            await asyncio.sleep(0)
            h.stream = Stream([event("new answer"), event(finish="stop")])
            h.chat.submit_text("browser-one", "second")
            gate.set()
            await finished(h.chat)
            state = h.chat.state()
            assert "must not appear" not in json.dumps(state)
            assert state["messages"][-1]["content"] == "new answer"
            assert state["messages"][-1]["status"] == "complete"
            with pytest.raises(asyncio.CancelledError):
                h.chat._delta(generation, "late callback")
            assert first.closed
            assert [row[-1] for row in h.published if row[2] == "assistant"] == ["interrupted", "complete"]
    asyncio.run(scenario())


def test_echo_context_and_voice_input_wait_for_full_playback(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            queue = h.chat.state()["speech_queue"]
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": queue[0]["id"]})
            generation = h.chat.state()["generation"]
            context = h.chat.active_tts_context()
            assert context[0]["tts_event_id"] == queue[0]["id"]
            assert context[0]["expected_text"] == "Hello there."
            assert not h.chat.on_speech_start()
            assert not h.chat.on_caption("Hello there", "echo", h.clock())
            assert not h.chat.on_partial("Hello")
            assert h.chat.state()["generation"] == generation
            assert h.chat.state()["suppressed_input_count"] == 2
            assert not h.chat.on_partial("stop")
            assert h.chat.state()["generation"] == generation
            for item in queue:
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"]})
            h.clock.advance(0.6)
            assert h.chat.on_caption("wait", "actual", h.clock())
            assert h.chat.state()["pending_text"] == "wait"
            assert h.chat.active_tts_context()
            h.clock.advance(module.ECHO_SECONDS + 0.1)
            assert h.chat.active_tts_context() == []
    asyncio.run(scenario())


def test_echo_caption_spanning_sentences_is_suppressed_after_playback(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            for item in h.chat.state()["speech_queue"]:
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": item["id"]})
                h.clock.advance(3)
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"]})
            h.clock.advance(0.6)
            assert not h.chat.on_caption("Hello there lets learn", "combined-echo", h.clock())
            assert h.chat.state()["pending_text"] == ""
            assert any(c["expected_text"] == "Hello there. Let's learn!" for c in h.chat.active_tts_context())
            assert h.chat.on_caption("no", "real-interruption", h.clock())
            assert h.chat.state()["pending_text"] == "no"
    asyncio.run(scenario())


def test_half_duplex_waits_for_model_and_all_speech_then_rejects_late_echo(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            gate = asyncio.Event()
            h.stream = Stream([event("First sentence. "), event("Second sentence."), event(finish="stop")], gate)
            h.chat.submit_text("browser-one", "Question")
            await h.stream.waiting.wait()
            generation = h.chat.state()["generation"]
            assert not h.chat.on_speech_start()
            assert not h.chat.on_partial("First sentence", "late-echo")
            assert not h.chat.on_caption("Any sound while output is pending", "during-output", h.clock())
            first = h.chat.state()["speech_queue"][0]
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": first["id"], "duration_ms": 900})
            h.clock.advance(1)
            assert h.chat.state()["input_accepting"] is False, "the model has not finished its full response"
            assert h.chat.state()["generation"] == generation
            gate.set()
            await finished(h.chat)
            remaining = [item for item in h.chat.state()["speech_queue"] if item["status"] == "pending"]
            assert remaining and not h.chat.state()["input_accepting"]
            for item in remaining:
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"], "duration_ms": 1000})
            assert not h.chat.state()["input_accepting"], "brief echo tail is still closed"
            h.clock.advance(0.6)
            assert h.chat.state()["input_accepting"]
            assert not h.chat.on_caption("A mistranscribed echo fragment", "late-echo", h.clock())
            assert h.chat.on_caption("Here is my next question", "fresh-user", h.clock())
            assert h.chat.state()["pending_text"] == "Here is my next question"
    asyncio.run(scenario())


def test_turn_timings_separate_stt_model_wait_and_actual_browser_audio(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            gate = asyncio.Event()
            h.stream = Stream([event(), event("Pong."), event(finish="stop")], gate)
            ended = h.clock()
            h.chat.on_speech_end(ended)
            h.clock.advance(0.2)
            assert h.chat.on_caption("ping", "timed-input", ended + 0.1)
            h.silence()
            h.clock.advance(1)
            h.chat._tick_once()
            await h.stream.waiting.wait()
            h.clock.advance(2)
            gate.set()
            await finished(h.chat)
            item = h.chat.state()["speech_queue"][0]
            h.clock.advance(0.4)
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": item["id"]})
            h.clock.advance(1.1)
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"], "duration_ms": 900})
            timing = h.chat.state()["turn_timings"][-1]
            assert timing["speech_to_stt_ms"] == 100
            assert timing["stt_delivery_ms"] == 100
            assert timing["turn_wait_ms"] == 1000
            assert timing["model_queue_ms"] == 0
            assert timing["emullm_first_token_ms"] == 2000
            assert timing["emullm_total_ms"] == 2000
            assert timing["tts_queue_wait_ms"] == 400
            assert timing["tts_playback_ms"] == 900
            assert timing["tts_playback_source"] == "browser"
            assert timing["total_ms"] == 4500
            assert timing["status"] == "complete"
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"], "duration_ms": 900})
            assert h.chat.state()["turn_timings"][-1]["tts_playback_ms"] == 900, "duplicate acknowledgements cannot inflate durations"
            with pytest.raises(ValidationError):
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "idle", "duration_ms": -1})
    asyncio.run(scenario())


def test_agent_voice_uses_monitored_tts_without_a_model_request_or_feedback(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            result = h.chat.speak_agent("copilot", "The timing update is ready.")
            assert result["queued"] is True
            assert not h.requests
            assert not h.chat.state()["input_accepting"]
            message = h.chat.state()["messages"][-1]
            assert message["source_agent_id"] == "copilot"
            assert message["channel"] == "agent_voice"
            assert h.published[-1][0] == "copilot"
            assert len(h.chat.request_preview()["messages"]) == 1, "agent announcements must not be recycled into model context"
            with pytest.raises(ConflictError):
                h.chat.speak_agent("copilot", "Do not interrupt the first announcement")
            item = h.chat.state()["speech_queue"][0]
            assert item["source_agent_id"] == "copilot"
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": item["id"]})
            assert not h.chat.on_partial("The timing update", "own-announcement")
            h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": item["id"], "duration_ms": 1400})
            h.clock.advance(0.6)
            assert h.chat.state()["input_accepting"]
            assert not h.chat.on_caption("The timing update is ready", "own-announcement", h.clock())
            assert h.chat.state()["pending_text"] == ""
            timing = h.chat.state()["turn_timings"][-1]
            assert timing["input_source"] == "agent_voice"
            assert timing["emullm_total_ms"] is None
            assert timing["tts_playback_ms"] == 1400
    asyncio.run(scenario())


def test_owner_lease_queue_order_idempotent_acks_and_timeout(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            first = h.chat.state()
            assert h.chat.start("browser-one")["session_id"] == first["session_id"]
            for method, args in (
                (h.chat.start, ("browser-two",)), (h.chat.stop, ("browser-two",)),
                (h.chat.interrupt, ("browser-two",)), (h.chat.submit_text, ("browser-two", "text")),
            ):
                with pytest.raises(ConflictError):
                    method(*args)
            with pytest.raises(ConflictError):
                h.chat.configure({"model": "other"})
            with pytest.raises(ConflictError):
                h.chat.set_history({"agent_id": "language-chat", "messages": []})
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            queue = h.chat.state()["speech_queue"]
            with pytest.raises(ConflictError, match="order"):
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "speaking", "speech_id": queue[1]["id"]})
            for state in ("speaking", "speaking", "done", "done"):
                ack = h.chat.heartbeat({"client_id": "browser-one", "speech_state": state, "speech_id": queue[0]["id"]})
                assert ack["acknowledged"]
            assert h.chat.state()["speech_queue"][0]["status"] == "done"
            unknown = h.chat.heartbeat({"client_id": "browser-one", "speech_state": "done", "speech_id": "stale"})
            assert unknown["acknowledged"] is False
            h.clock.advance(module.LEASE_SECONDS)
            h.chat._tick_once()
            expired = h.chat.state()
            assert not expired["active"]
            assert expired["client_id"] is None
            assert expired["speech_queue"][1]["status"] == "cancelled"
            assert "heartbeat expired" in expired["error"]
            with pytest.raises(ConflictError):
                h.chat.heartbeat({"client_id": "browser-one", "speech_state": "idle"})
            new = h.chat.start("browser-two")
            assert new["session_id"] != first["session_id"]
            assert new["generation"] > expired["generation"]
            h.chat.stop("browser-two")
    asyncio.run(scenario())


def test_browser_tts_error_is_sanitized_and_never_replayed(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            item = h.chat.state()["speech_queue"][0]
            result = h.chat.heartbeat({"client_id": "browser-one", "speech_state": "error",
                                       "speech_id": item["id"], "error": "sensitive browser data"})
            assert result["acknowledged"]
            assert result["speech_queue"][0]["status"] == "error"
            assert "sensitive browser data" not in json.dumps(result)
            assert "sensitive browser data" not in json.dumps(h.audits)
    asyncio.run(scenario())


def test_split_utf8_sse_done_usage_and_json_fallback(tmp_path):
    async def scenario():
        payload = b": keepalive\r\n\r\n" + event("Café. ") + (
            b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\n'
        ) + event("Voilà!") + b"data: [DONE]"
        async with running(tmp_path / "sse") as h:
            h.stream = Stream([payload[i:i + 1] for i in range(len(payload))])
            h.chat.submit_text("browser-one", "text")
            await finished(h.chat)
            assert h.chat.state()["messages"][-1]["content"] == "Café. Voilà!"
            assert h.chat.state()["messages"][-1]["status"] == "complete"
        async with running(tmp_path / "json", response=lambda _: httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Real JSON reply."}, "finish_reason": "length"}],
        })) as h:
            h.chat.submit_text("browser-one", "text")
            await finished(h.chat)
            last = h.chat.state()["messages"][-1]
            assert last["content"] == "Real JSON reply."
            assert last["status"] == "truncated"
            assert last["finish_reason"] == "length"
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [
    "http", "bad-json", "bad-shape", "empty", "unterminated", "invalid-event", "tool",
    "json-no-text", "wrong-type", "network", "utf8", "too-large", "content-filter",
])
def test_provider_failures_never_claim_completion_or_leak_secrets(tmp_path, monkeypatch, kind):
    secret = "auth-token-never-in-state"
    monkeypatch.setenv("WS_COLLAB_EMULLM_TOKEN", secret)

    async def scenario():
        def response(_):
            if kind == "http":
                return httpx.Response(401, json={"error": {"message": secret}})
            if kind == "bad-json":
                return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{invalid")
            if kind == "json-no-text":
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": []}}]})
            if kind == "wrong-type":
                return httpx.Response(200, text=secret)
            if kind == "network":
                raise httpx.ConnectError(secret)
            payloads = {
                "bad-shape": b'data: {"choices": {}}\n\n',
                "empty": event(finish="stop"),
                "unterminated": event("partial answer"),
                "invalid-event": b"data: no JSON\n\n",
                "tool": b'data: {"choices":[{"delta":{"tool_calls":[{}]},"finish_reason":"tool_calls"}]}\n\n',
                "utf8": b"data: \xff\n\n",
                "too-large": event("x" * (module.MAX_OUTPUT + 1)),
                "content-filter": event("partial", finish="content_filter"),
            }
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream([payloads[kind]]))

        async with running(tmp_path, response=response) as h:
            h.chat.submit_text("browser-one", "test failure")
            await finished(h.chat)
            state = h.chat.state()
            assert state["error"]
            assert state["messages"][-1]["status"] == "error"
            assert h.published[-1][-1] == "error"
            assert not any(q["status"] in {"pending", "playing"} for q in state["speech_queue"])
            assert secret not in json.dumps(state)
            assert secret not in json.dumps(h.audits)
            assert secret not in h.chat.path.read_text(encoding="utf-8")
            assert h.requests[0].headers["authorization"] == f"Bearer {secret}"
    asyncio.run(scenario())


def test_models_discovery_worker_endpoint_auth_and_no_automatic_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_COLLAB_EMULLM_TOKEN", "model-discovery-secret")

    async def scenario():
        h = Harness(tmp_path, model="", endpoint="http://provider.test/worker/123/v1/",
                    response=lambda _: httpx.Response(200, json={"data": [{"id": "provider/model"}]}))
        result = await h.chat.models()
        assert result == {"models": [{"id": "provider/model"}], "endpoint": "http://provider.test/worker/123/v1"}
        assert str(h.requests[0].url) == "http://provider.test/worker/123/v1/models"
        assert h.requests[0].headers["authorization"] == "Bearer model-discovery-secret"
        assert h.chat.config()["model"] == ""
        assert not h.chat.state()["active"]
        assert "model-discovery-secret" not in json.dumps(h.chat.state())
        assert h.options[0]["trust_env"] is False
        assert h.options[0]["follow_redirects"] is False
        await h.chat.startup()
        try:
            with pytest.raises(ValidationError, match="model"):
                h.chat.start("browser-one")
        finally:
            await h.chat.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{}, {"data": []}, {"data": [None]}, {"data": [{"id": ""}]}])
def test_models_failure_is_a_structured_error(tmp_path, body):
    async def scenario():
        h = Harness(tmp_path, response=lambda _: httpx.Response(200, json=body))
        with pytest.raises(WsCollabError):
            await h.chat.models()
    asyncio.run(scenario())


def test_httpx_is_lazy_and_missing_dependency_is_explicit(tmp_path, monkeypatch):
    original = module.importlib.import_module

    def missing(name):
        if name == "httpx":
            raise ImportError("not installed")
        return original(name)

    monkeypatch.setattr(module.importlib, "import_module", missing)

    async def scenario():
        h = Harness(tmp_path)
        await h.chat.startup()
        assert not h.chat.state()["active"]
        with pytest.raises(ConfigurationError, match="remote"):
            await h.chat.models()
        try:
            h.chat.start("browser-one")
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            assert "remote" in h.chat.state()["error"]
            assert h.chat.state()["messages"][-1]["status"] == "error"
        finally:
            await h.chat.shutdown()
    asyncio.run(scenario())


def test_timeout_closes_response_and_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.02)

    async def scenario():
        async with running(tmp_path) as h:
            h.stream = Stream([event("partial"), event("never")], asyncio.Event())
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            assert h.stream.closed
            assert h.chat.state()["messages"][-1]["status"] == "error"
            assert "timed out" in h.chat.state()["error"]
    asyncio.run(scenario())


def test_callbacks_are_outside_lock_and_sync_methods_work_from_other_thread(tmp_path):
    async def scenario():
        h = Harness(tmp_path, speak_replies=False)
        checks = []

        def unlocked_callback(*args):
            def probe():
                with h.chat._lock:
                    checks.append(True)
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(1)
            assert not worker.is_alive(), "Application callback was invoked with the language-chat lock held"

        h.chat._audit = unlocked_callback
        h.chat._publish_message = unlocked_callback
        h.chat._microphone_state = lambda: (unlocked_callback(), h.microphone)[1]
        await h.chat.startup()
        try:
            await asyncio.to_thread(h.chat.start, "browser-one")
            await asyncio.to_thread(h.chat.submit_text, "browser-one", "threaded input")
            await finished(h.chat)
            assert h.chat.state()["messages"][-1]["status"] == "complete"
            assert checks
        finally:
            await h.chat.shutdown()
    asyncio.run(scenario())


def test_caps_pending_text_history_queue_chunks_and_dedupe(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path) as h:
            with pytest.raises(ValidationError):
                h.chat.submit_text("browser-one", "x" * (module.MAX_TEXT + 1))
            for index in range(600):
                h.chat.on_caption("x", str(index), h.clock(), is_echo=True)
            assert len(h.chat._seen) == len(h.chat._seen_ids) == 512
            assert h.chat.on_caption("x" * module.MAX_TEXT, "full", h.clock())
            assert not h.chat.on_caption("overflow", "overflow", h.clock())
            assert len(h.chat.state()["pending_text"]) == module.MAX_TEXT
            h.chat.interrupt("browser-one")
            h.stream = Stream([event("x" * 1000), event(finish="stop")])
            h.chat.submit_text("browser-one", "chunk")
            await finished(h.chat)
            assert all(len(q["text"]) <= module.MAX_SPEECH_CHARS for q in h.chat.state()["speech_queue"])
            h.chat.interrupt("browser-one")
            monkeypatch.setattr(module, "MAX_QUEUE", 2)
            h.stream = Stream([event("One. Two. Three."), event(finish="stop")])
            h.chat.submit_text("browser-one", "overflow queue")
            await finished(h.chat)
            assert "queue is full" in h.chat.state()["error"]
            assert len(h.chat.state()["speech_queue"]) <= 2
    asyncio.run(scenario())


def test_persistence_failure_rolls_back_config_and_history(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    previous = h.chat.config()

    def failed(*args):
        raise OSError("disk failure contains sensitive information")

    monkeypatch.setattr(module.os, "replace", failed)
    with pytest.raises(ConfigurationError, match="persist"):
        h.chat.configure({"agent_id": "new-agent", "model": "different"})
    assert h.chat.config() == previous
    assert len(h.chat.state()["agents"]) == 1
    with pytest.raises(ConfigurationError, match="persist"):
        h.chat.set_history({"agent_id": "language-chat", "messages": [{"role": "user", "content": "new"}]})
    assert h.chat.state()["messages"] == []
    assert not list(tmp_path.glob("*.pending"))


def test_corrupt_persistence_is_not_silently_reset(tmp_path):
    (tmp_path / "language_chat.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid"):
        Harness(tmp_path)


def test_stopped_history_snapshots_are_detached(tmp_path):
    h = Harness(tmp_path)
    h.chat.set_history({"agent_id": "language-chat", "messages": [{"role": "user", "content": "kept"}]})
    state = h.chat.state()
    state["messages"][0]["content"] = "tampered"
    state["config"]["model"] = "tampered"
    state["agents"].clear()
    assert h.chat.state()["messages"][0]["content"] == "kept"
    assert h.chat.config()["model"] == "test-model"
    assert len(h.chat.state()["agents"]) == 1


def test_request_persistence_failure_keeps_unsent_text_and_does_not_call_provider(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path) as h:
            with monkeypatch.context() as patch:
                patch.setattr(h.chat, "_save_locked", lambda: (_ for _ in ()).throw(ConfigurationError("disk failed")))
                with pytest.raises(ConfigurationError):
                    h.chat.submit_text("browser-one", "preserve me")
                assert h.chat.state()["pending_text"] == "preserve me"
                assert not h.chat.state()["messages"]
                assert not h.published
                assert not h.requests
            h.chat.send_now("browser-one")
            await finished(h.chat)
            assert h.chat.state()["messages"][0]["content"] == "preserve me"
    asyncio.run(scenario())


def test_shutdown_closes_live_stream_even_when_interruption_cannot_be_saved(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path)
        h.stream = Stream([event("partial"), event("not reached")], asyncio.Event())
        await h.chat.startup()
        h.chat.start("browser-one")
        h.chat.submit_text("browser-one", "hello")
        await h.stream.waiting.wait()
        with monkeypatch.context() as patch:
            patch.setattr(h.chat, "_save_locked", lambda: (_ for _ in ()).throw(ConfigurationError("disk failed")))
            with pytest.raises(ConfigurationError):
                await h.chat.shutdown()
        assert h.stream.closed
        assert not h.chat.state()["active"]
        assert h.chat._tick_task is None
        assert not h.chat._tasks
        assert h.chat._model_task is None
    asyncio.run(scenario())


def test_backend_restart_discards_incomplete_listening_and_recovers_partial_history(tmp_path):
    h = Harness(tmp_path)
    data = json.loads(h.chat.path.read_text(encoding="utf-8"))
    data["agents"]["language-chat"]["messages"] = [{
        "id": "interrupted-by-crash", "role": "assistant", "content": "saved partial",
        "created_at": h.clock(), "status": "streaming",
    }]
    h.chat.path.write_text(json.dumps(data), encoding="utf-8")
    restored = Harness(tmp_path)
    state = restored.chat.state()
    assert not state["active"]
    assert state["messages"][-1]["status"] == "interrupted"
    assert not state["speech_queue"]
    assert state["next_request"]["messages"][-1]["content"] == "saved partial"


def test_message_history_cap_and_preview_only_use_recent_messages(tmp_path):
    async def scenario():
        h = Harness(tmp_path, speak_replies=False, history_limit=2)
        h.chat.set_history({"agent_id": "language-chat", "messages": [
            {"role": "user", "content": f"old {index}"} for index in range(100)
        ]})
        await h.chat.startup()
        h.chat.start("browser-one")
        try:
            h.chat.submit_text("browser-one", "newest")
            await finished(h.chat)
            messages = h.chat.state()["messages"]
            assert len(messages) == 100
            assert messages[0]["content"] == "old 2"
            submitted = json.loads(h.requests[0].content)["messages"]
            assert [m["content"] for m in submitted[1:]] == ["old 98", "old 99", "newest"]
        finally:
            await h.chat.shutdown()
    asyncio.run(scenario())


def test_agent_limit_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_AGENTS", 2)
    h = Harness(tmp_path)
    h.chat.configure({"agent_id": "second"})
    with pytest.raises(ValidationError, match="At most"):
        h.chat.configure({"agent_id": "third"})
    assert h.chat.config()["agent_id"] == "second"


def test_active_request_count_is_bounded_when_cancelled_provider_is_slow_to_close(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_MODEL_TASKS", 1)

    async def scenario():
        async with running(tmp_path, speak_replies=False) as h:
            gate = asyncio.Event()
            h.stream = Stream([event("old"), event("late")], gate, ignore_cancel=True)
            h.chat.submit_text("browser-one", "first")
            await h.stream.waiting.wait()
            h.chat.interrupt("browser-one")
            await asyncio.sleep(0)
            h.chat.submit_text("browser-one", "second")
            await finished(h.chat)
            assert "still closing" in h.chat.state()["error"]
            assert len(h.chat._tasks) == 1
            assert len(h.requests) == 1
            gate.set()
            await asyncio.sleep(0.01)
    asyncio.run(scenario())


def test_sse_multiline_data_and_terminal_finish_without_done(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            payload = (
                b'event: message\r\ndata: {"choices": [\r\n'
                b'data: {"index":0,"delta":{"role":"assistant","content":"Hi."},"finish_reason":null}]}\r\n\r\n'
                + event(finish="stop")
            )
            h.stream = Stream([payload])
            h.chat.submit_text("browser-one", "hello")
            await finished(h.chat)
            assert h.chat.state()["messages"][-1]["content"] == "Hi."
            assert h.chat.state()["messages"][-1]["status"] == "complete"
    asyncio.run(scenario())


def test_models_provider_error_and_redirect_do_not_leak_or_follow_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_COLLAB_EMULLM_TOKEN", "private-model-token")

    async def scenario():
        for code in (302, 503):
            h = Harness(tmp_path / str(code), response=lambda _: httpx.Response(
                code, headers={"location": "http://other.test/v1/models"}, text="private-model-token",
            ))
            with pytest.raises(WsCollabError) as caught:
                await h.chat.models()
            assert "private-model-token" not in json.dumps(caught.value.to_dict())
            assert len(h.requests) == 1
    asyncio.run(scenario())


def test_canonical_callback_failure_stops_chat_and_reports_sanitized_error(tmp_path):
    async def scenario():
        async with running(tmp_path) as h:
            def failed(*args):
                raise RuntimeError("sensitive sink details")
            h.chat._publish_message = failed
            with pytest.raises(ConfigurationError, match="callback failed"):
                h.chat.submit_text("browser-one", "test")
            await asyncio.sleep(0)
            state = h.chat.state()
            assert not state["active"]
            assert state["phase"] == "error"
            assert "sensitive sink details" not in json.dumps(state)
            assert not h.requests
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"client_id": "", "speech_state": "idle"},
    {"client_id": "browser-one", "speech_state": "other"},
    {"client_id": "browser-one", "speech_state": "speaking"},
    {"client_id": "browser-one", "speech_state": "done", "speech_id": ""},
    {"client_id": "browser-one", "speech_state": "idle", "unsupported": 1},
])
def test_heartbeat_validation(tmp_path, body):
    h = Harness(tmp_path)
    with pytest.raises(ValidationError):
        h.chat.heartbeat(body)
