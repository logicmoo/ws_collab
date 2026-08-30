from __future__ import annotations

import json
import threading
import time
from urllib.parse import parse_qs, urlparse

from ws_collab.meet_bridge import cdp
from ws_collab.meet_bridge.bridge import (
    CaptionEmitter,
    RecentCaptionDeduplicator,
    apply_caption_payload,
    cached_sso_accounts_status,
    caption_raw_text_for_key,
    drain_caption_push_events,
    invalidate_sso_satisfaction,
    install_caption_push,
    read_caption_payloads,
    record_caption_raw_diagnostics,
    role_caption_key,
    scan_sso_accounts_if_permitted,
    sso_probe_authusers,
    update_sso_satisfaction,
)
from ws_collab.meet_bridge.scripts_js import CAPTION_OBSERVER_JS, CAPTIONS_JS
from ws_collab.meet_bridge.tracker import CaptionTracker

V1 = "/ws_collab/v1"


def test_status_sso_payload_uses_cache_without_live_scan(monkeypatch) -> None:
    from ws_collab.meet_bridge import bridge

    monkeypatch.setattr(
        bridge,
        "scan_signed_in_sso_accounts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("status must not scan")),
    )

    missing = cached_sso_accounts_status({}, now=100.0)
    cached = cached_sso_accounts_status(
        {"sso_accounts": [{"email": "one@example.test", "authuser": 0}], "sso_accounts_scanned_at": 95.0},
        now=100.0,
    )

    assert missing == {
        "ssoAccounts": [],
        "ssoAccountsScannedAt": None,
        "ssoAccountsStale": True,
        "ssoSatisfied": False,
        "ssoSatisfiedAt": None,
    }
    assert cached["ssoAccounts"] == [{"email": "one@example.test", "authuser": 0}]
    assert cached["ssoAccountsScannedAt"] == 95.0
    assert cached["ssoAccountsStale"] is False
    assert cached["ssoSatisfied"] is False


def test_sso_probe_slots_use_configured_role_authusers() -> None:
    assert sso_probe_authusers({"host": 3, "companion": 5, "guest": 5}) == [3, 5]


def test_sso_satisfied_state_serves_cached_accounts_without_scan() -> None:
    holder = {
        "sso_accounts": [
            {"email": "one@example.test", "authuser": 0, "signedIn": True},
            {"email": "two@example.test", "authuser": 1, "signedIn": True},
        ],
        "sso_verified_roles": {
            "host": {"email": "one@example.test"},
            "companion": {"email": "two@example.test"},
        },
    }
    role_authusers = {"host": 0, "companion": 1}
    role_emails = {"host": "one@example.test", "companion": "two@example.test"}
    assert update_sso_satisfaction(
        holder,
        role_authusers=role_authusers,
        role_emails=role_emails,
        required_roles=["host", "companion"],
        now=100.0,
    ) is True

    accounts = scan_sso_accounts_if_permitted(
        holder,
        lambda: (_ for _ in ()).throw(AssertionError("satisfied SSO must not scan")),
        role_authusers=role_authusers,
        role_emails=role_emails,
        required_roles=["host", "companion"],
    )
    status = cached_sso_accounts_status(holder, now=1000.0)

    assert [account["authuser"] for account in accounts] == [0, 1]
    assert status["ssoSatisfied"] is True
    assert status["ssoSatisfiedAt"] == 100.0
    assert status["ssoAccountsStale"] is False


def test_sso_invalidation_triggers_each_permit_exactly_one_rescan() -> None:
    for reason in (
        "operator-sso-scan",
        "host-verification-failed",
        "host-tab-reconnected",
        "companion-cdp-disconnected",
        "role-assignments-changed",
    ):
        holder = {
            "sso_accounts": [{"email": "old@example.test", "authuser": 0, "signedIn": True}],
            "sso_satisfied": True,
            "sso_satisfied_at": 10.0,
            "sso_verified_roles": {"host": {"email": "one@example.test"}},
        }
        calls = 0

        def scanner():
            nonlocal calls
            calls += 1
            return [{"email": "one@example.test", "authuser": 0, "signedIn": True}]

        invalidate_sso_satisfaction(holder, reason, clear_verified=False, now=20.0)
        first = scan_sso_accounts_if_permitted(
            holder,
            scanner,
            role_authusers={"host": 0},
            role_emails={"host": "one@example.test"},
            required_roles=["host"],
            now=21.0,
        )
        second = scan_sso_accounts_if_permitted(
            holder,
            scanner,
            role_authusers={"host": 0},
            role_emails={"host": "one@example.test"},
            required_roles=["host"],
            now=22.0,
        )

        assert calls == 1
        assert first == second == [{"email": "one@example.test", "authuser": 0, "signedIn": True}]
        assert holder["sso_satisfied"] is True
        assert holder["sso_satisfied_at"] == 21.0


def test_bridge_records_latest_and_distinct_raw_caption_snapshots() -> None:
    holder: dict = {}
    status: dict = {}
    first = {
        "rawText": "Alice | hello there ‖ Bob | hi",
        "rawRows": [
            {"key": "row-1", "rawText": "Alice | hello there", "childCount": 2},
            {"key": "row-2", "rawText": "Bob | hi", "childCount": 2},
        ],
        "rowCount": 2,
        "childCount": 2,
    }

    record_caption_raw_diagnostics(holder, status, first, now=100.0)
    record_caption_raw_diagnostics(holder, status, first, now=101.0)
    record_caption_raw_diagnostics(
        holder,
        status,
        {**first, "rawText": "Alice | hello there ‖ Bob | hi ‖ Carol | welcome"},
        now=102.0,
    )

    assert holder["rawText"] == "Alice | hello there ‖ Bob | hi ‖ Carol | welcome"
    assert holder["rawAt"] == 102.0
    assert status["rawRows"] == first["rawRows"]
    assert status["rawByRole"]["host"]["rawRows"] == first["rawRows"]
    assert status["rawHistoryCount"] == 2
    assert [entry["at"] for entry in holder["rawHistory"]] == [100.0, 102.0]

    for idx in range(60):
        record_caption_raw_diagnostics(
            holder,
            status,
            {**first, "rawText": f"snapshot {idx}"},
            now=200.0 + idx,
        )

    assert len(holder["rawHistory"]) == 50
    assert holder["rawHistory"][0]["rawText"] == "snapshot 10"
    assert holder["rawHistory"][-1]["rawText"] == "snapshot 59"


def test_caption_raw_text_lookup_handles_tracker_clone_keys() -> None:
    holder = {
        "rawRows": [{"key": "row-1", "rawText": "Alice | host top-level row", "childCount": 2}],
        "rawByRole": {
            "host": {
                "rawRows": [
                    {"key": "row-1", "rawText": "Alice | original DOM row", "childCount": 2},
                    {"key": "row-2", "rawText": "Bob | another DOM row", "childCount": 2},
                ]
            },
            "companion": {
                "rawRows": [
                    {"key": "row-1", "rawText": "Alice | companion DOM row", "childCount": 2},
                ]
            },
        },
    }

    assert caption_raw_text_for_key(holder, "row-1") == "Alice | original DOM row"
    assert caption_raw_text_for_key(holder, "row-1#2") == "Alice | original DOM row"
    assert caption_raw_text_for_key(holder, "companion:row-1#2") == "Alice | companion DOM row"
    assert caption_raw_text_for_key(holder, "missing") == ""


def test_companion_raw_diagnostics_are_per_role_without_overwriting_host() -> None:
    holder: dict = {}
    status: dict = {}
    host_payload = {
        "rawText": "Alice | host view",
        "rawRows": [{"key": "row-1", "rawText": "Alice | host view", "childCount": 2}],
        "rowCount": 1,
        "childCount": 2,
    }
    companion_payload = {
        "rawText": "Alice | companion view",
        "rawRows": [{"key": "row-1", "rawText": "Alice | companion view", "childCount": 2}],
        "rowCount": 1,
        "childCount": 2,
    }

    record_caption_raw_diagnostics(holder, status, host_payload, role="host", now=10.0)
    record_caption_raw_diagnostics(holder, status, companion_payload, role="companion", now=11.0)

    assert holder["rawText"] == "Alice | host view"
    assert status["rawText"] == "Alice | host view"
    assert status["rawByRole"]["host"]["rawText"] == "Alice | host view"
    assert status["rawByRole"]["companion"]["rawText"] == "Alice | companion view"
    assert status["rawByRole"]["companion"]["rawAt"] == 11.0


def test_captions_js_pipe_joins_diagnostic_raw_text_without_changing_parsed_text() -> None:
    assert "const rawText = diagnosticRegionRawText(region);" in CAPTIONS_JS
    assert "const rawRowText = diagnosticRawText(rowEl);" in CAPTIONS_JS
    assert ".map((child) => diagnosticRawText(child))" in CAPTIONS_JS
    assert '.filter((text) => text)' in CAPTIONS_JS
    assert '.join(" | ")' in CAPTIONS_JS
    assert '.join(" \\u2016 ")' in CAPTIONS_JS
    assert 'const restText = [...rowEl.children].slice(1).map((c) => c.innerText || "").join(" ")' in CAPTIONS_JS


def test_caption_observer_buffers_and_swallows_push_failures() -> None:
    assert "%s" not in CAPTION_OBSERVER_JS
    assert "window.__wsCollabReadCaptionPayload = () =>" in CAPTION_OBSERVER_JS
    assert "try {" in CAPTION_OBSERVER_JS
    assert "catch (error)" in CAPTION_OBSERVER_JS
    assert "maxQueue: 5" in CAPTION_OBSERVER_JS
    assert "typeof binding !== \"function\"" in CAPTION_OBSERVER_JS
    assert "this.queue.splice(0, this.queue.length - this.maxQueue)" in CAPTION_OBSERVER_JS
    assert "window.__wsCollabCaptionPushDebug" in CAPTION_OBSERVER_JS


def test_install_caption_push_enables_runtime_page_and_logs_lifecycle() -> None:
    from ws_collab.meet_bridge import bridge

    bridge._CAPTION_PUSH_LIFECYCLE_LOG_AT.clear()
    bridge._CAPTION_PUSH_INSTALL_ATTEMPT_LOGGED.clear()

    class FakeTab:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []

        def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
            self.calls.append((method, params))
            return {"ok": True}

        def evaluate(self, expression: str, timeout: float = 10.0) -> str:
            assert expression == CAPTION_OBSERVER_JS
            return "installed"

    tab = FakeTab()
    logs: list[tuple[str, str]] = []

    assert install_caption_push(tab, role="host", log=lambda text, **kwargs: logs.append((text, kwargs.get("role", ""))))
    assert [method for method, _params in tab.calls] == [
        "Runtime.enable",
        "Runtime.addBinding",
        "Page.enable",
        "Page.addScriptToEvaluateOnNewDocument",
    ]
    assert tab.calls[1][1] == {"name": "__wsCollabCaptionPush"}
    assert [role for _text, role in logs] == ["host", "host", "host", "host"]
    assert any("push observer install attempt" in text for text, _role in logs)
    assert any("binding registered" in text for text, _role in logs)
    assert any("observer registered" in text for text, _role in logs)
    assert any("observer installed" in text for text, _role in logs)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.frames: list[object] = []
        self.closed = False
        self.condition = threading.Condition()

    def send(self, text: str) -> None:
        with self.condition:
            self.sent.append(json.loads(text))
            self.condition.notify_all()

    def recv(self) -> str:
        with self.condition:
            while not self.frames and not self.closed:
                self.condition.wait(0.25)
            if self.closed:
                raise RuntimeError("closed")
            frame = self.frames.pop(0)
            if isinstance(frame, (str, bytes, bytearray)):
                return frame
            return json.dumps(frame)

    def push(self, payload: dict) -> None:
        with self.condition:
            self.frames.append(payload)
            self.condition.notify_all()

    def push_raw(self, payload: object) -> None:
        with self.condition:
            self.frames.append(payload)
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def test_cdp_tab_queues_events_without_breaking_concurrent_call(monkeypatch) -> None:
    fake_ws = _FakeWebSocket()
    monkeypatch.setattr(cdp, "create_connection", lambda *_args, **_kwargs: fake_ws)
    tab = cdp.CdpTab("ws://tab")
    result: dict[str, object] = {}

    caller = threading.Thread(target=lambda: result.setdefault("value", tab.call("Runtime.evaluate")), daemon=True)
    caller.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not fake_ws.sent:
        time.sleep(0.01)
    assert fake_ws.sent

    event = {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": "{}"}}
    fake_ws.push(event)
    fake_ws.push({"id": fake_ws.sent[0]["id"], "result": {"ok": True}})
    caller.join(2.0)
    tab.close()

    assert result["value"] == {"ok": True}
    assert tab.drain_events() == [event]


def test_cdp_tab_logs_bad_json_once_and_keeps_receiving(monkeypatch) -> None:
    fake_ws = _FakeWebSocket()
    logs: list[str] = []
    monkeypatch.setattr(cdp, "create_connection", lambda *_args, **_kwargs: fake_ws)
    tab = cdp.CdpTab("ws://tab", error_handler=logs.append)
    fake_ws.push_raw("{not json")
    event = {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": "{}"}}
    fake_ws.push(event)

    deadline = time.time() + 2.0
    drained: list[dict] = []
    while time.time() < deadline:
        drained = tab.drain_events()
        if drained:
            break
        time.sleep(0.01)
    tab.close()

    assert drained == [event]
    assert len(logs) == 1
    assert "invalid JSON" in logs[0]


def test_cdp_tab_normalizes_empty_and_binary_frames_without_spurious_errors(monkeypatch) -> None:
    fake_ws = _FakeWebSocket()
    logs: list[str] = []
    monkeypatch.setattr(cdp, "create_connection", lambda *_args, **_kwargs: fake_ws)
    tab = cdp.CdpTab("ws://tab", error_handler=logs.append)
    byte_event = {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": "{\"ok\":true}"}}
    text_event = {"method": "Runtime.consoleAPICalled", "params": {"type": "log"}}

    fake_ws.push_raw(b"")
    fake_ws.push_raw("")
    fake_ws.push_raw(" \r\n\t ")
    fake_ws.push_raw(json.dumps(byte_event).encode("utf-8"))
    fake_ws.push_raw(json.dumps(text_event))

    deadline = time.time() + 2.0
    drained: list[dict] = []
    while time.time() < deadline:
        drained = tab.drain_events()
        if len(drained) >= 2:
            break
        time.sleep(0.01)
    tab.close()

    assert drained == [byte_event, text_event]
    assert logs == []


class _FakeMailbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, dict]] = []
        self.ingested: list[tuple[str, str, str, dict]] = []

    def send(self, recipient: str, line: str, *, sender: str, metadata: dict) -> None:
        self.sent.append((recipient, line, sender, metadata))

    def ingest_transcript(self, text: str, *, correlation_id: str, source_kind: str, audio_meta: dict) -> None:
        self.ingested.append((text, correlation_id, source_kind, audio_meta))


def _emitter(holder: dict, status: dict, captions_log: list[dict], mailbox: _FakeMailbox) -> CaptionEmitter:
    return CaptionEmitter(
        holder=holder,
        status=status,
        captions_log=captions_log,
        captions_index={},
        captions_lock=threading.Lock(),
        mailbox=mailbox,
        recipients=["conversation"],
        ignore=set(),
        self_name="Host User",
        sender_prefix="meet-",
        deduplicator=RecentCaptionDeduplicator(window_seconds=15.0, clock=lambda: 100.0),
        printer=lambda _line: None,
    )


def test_caption_emitter_prints_structured_new_caption_fields() -> None:
    holder = {"url": "https://meet.google.com/abc-defg-hij"}
    status = {
        "captionTransport": "poll",
        "captionTransportByRole": {
            "host": {"captionTransport": "push"},
            "companion": {"captionTransport": "poll"},
        },
    }
    record_caption_raw_diagnostics(
        holder,
        status,
        {
            "rawText": "Alice | Hello ‖ Bob | ok",
            "rawRows": [{"key": "row-1", "rawText": "Alice | Hello ‖ Bob | ok", "childCount": 2}],
        },
        role="host",
        now=10.0,
    )
    record_caption_raw_diagnostics(
        holder,
        status,
        {
            "rawText": "Alice | Hello",
            "rawRows": [{"key": "row-2", "rawText": "Alice | Hello", "childCount": 2}],
        },
        role="companion",
        now=11.0,
    )
    mailbox = _FakeMailbox()
    printed: list[str] = []
    emitter = CaptionEmitter(
        holder=holder,
        status=status,
        captions_log=[],
        captions_index={},
        captions_lock=threading.Lock(),
        mailbox=mailbox,
        recipients=["conversation"],
        ignore=set(),
        self_name="Host User",
        sender_prefix="meet-",
        deduplicator=RecentCaptionDeduplicator(window_seconds=15.0, clock=lambda: 100.0),
        printer=printed.append,
    )

    emitter.emit("host", "row-1", "Alice", "Hello", final=True)
    emitter.emit("companion", "row-2", "Alice", "Hello", final=True, replaces="row-1")

    assert printed[0].startswith("[caption] Alice: Hello ")
    host_detail = json.loads(printed[0][printed[0].index("{"):])
    assert host_detail == {
        "role": "host",
        "key": "host:row-1",
        "replaces": None,
        "rawText": "Alice | Hello ‖ Bob | ok",
        "captionTransport": "push",
    }
    assert printed[1].startswith("[caption:companion:duplicate] Alice: Hello (duplicate of host:row-1) ")
    companion_detail = json.loads(printed[1][printed[1].index("{"):])
    assert companion_detail["role"] == "companion"
    assert companion_detail["key"] == "companion:row-2"
    assert companion_detail["replaces"] == "companion:row-1"
    assert companion_detail["duplicateOf"] == "host:row-1"
    assert companion_detail["rawText"] == "Alice | Hello"
    assert companion_detail["captionTransport"] == "poll"


class _CaptureCaptionEmitter:
    def __init__(self) -> None:
        self.emits: list[tuple[str, str, str, str, bool, str | None]] = []

    def emit(self, role: str, key: str, speaker: str, text: str, final: bool = False, replaces: str | None = None) -> None:
        self.emits.append((role, key, speaker, text, final, replaces))


def _caption_payload(rows: list[dict[str, str]], live_keys: list[str]) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "liveKeys": live_keys,
        "rawText": " ‖ ".join(f"{row['speaker']} | {row['text']}" for row in rows),
        "rawRows": [
            {"key": row["key"], "rawText": f"{row['speaker']} | {row['text']}", "childCount": 2}
            for row in rows
        ],
        "rowCount": len(rows),
        "childCount": len(rows),
    }


def _apply_for_test(tracker: CaptionTracker, emitter: _CaptureCaptionEmitter, payload: dict, *, transport: str) -> dict:
    holder: dict = {"rawByRole": {"host": {"rawRows": []}}}
    status: dict = {}
    apply_caption_payload(
        "host",
        payload,
        holder=holder,
        status=status,
        tracker=tracker,
        caption_emitter=emitter,
        captions_lock=threading.Lock(),
        transport=transport,
        now=10.0,
    )
    return status


def test_malformed_and_oversized_push_payloads_are_skipped_without_emitting() -> None:
    class FakePushTab:
        def drain_events(self) -> list[dict]:
            return [
                {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": "{bad json"}},
                {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": "x" * (300 * 1024)}},
                {"method": "Runtime.bindingCalled", "params": {"name": "__wsCollabCaptionPush", "payload": json.dumps([])}},
            ]

    emitter = _CaptureCaptionEmitter()
    logs: list[tuple[str, str]] = []
    handled = drain_caption_push_events(
        "host",
        FakePushTab(),
        holder={"rawByRole": {"host": {"rawRows": []}}},
        status={},
        tracker=CaptionTracker(0),
        caption_emitter=emitter,
        captions_lock=threading.Lock(),
        log=lambda text, **kwargs: logs.append((text, kwargs.get("role", ""))),
    )

    assert handled == 0
    assert emitter.emits == []
    assert [role for _text, role in logs] == ["host", "host", "host"]


def test_pushed_payload_updates_tracker_like_equivalent_poll() -> None:
    baseline = _caption_payload([{"key": "row-1", "speaker": "Alice", "text": "Already here."}], ["row-1"])
    update = _caption_payload(
        [
            {"key": "row-1", "speaker": "Alice", "text": "Already here."},
            {"key": "row-2", "speaker": "Bob", "text": "New pushed sentence."},
        ],
        ["row-1"],
    )
    push_tracker, poll_tracker = CaptionTracker(0), CaptionTracker(0)
    push_emitter, poll_emitter = _CaptureCaptionEmitter(), _CaptureCaptionEmitter()

    _apply_for_test(push_tracker, push_emitter, baseline, transport="poll")
    _apply_for_test(poll_tracker, poll_emitter, baseline, transport="poll")
    push_status = _apply_for_test(push_tracker, push_emitter, update, transport="push")
    poll_status = _apply_for_test(poll_tracker, poll_emitter, update, transport="poll")

    assert push_emitter.emits == poll_emitter.emits == [
        ("host", "row-2", "Bob", "New pushed sentence.", True, None)
    ]
    assert push_tracker.raw == poll_tracker.raw
    assert push_status["rawRows"] == poll_status["rawRows"]
    assert push_status["captionTransport"] == "push"
    assert push_status["pushFrameCount"] == 1


def test_duplicate_push_and_poll_frame_does_not_double_emit() -> None:
    baseline = _caption_payload([{"key": "row-1", "speaker": "Alice", "text": "Already here."}], ["row-1"])
    update = _caption_payload(
        [
            {"key": "row-1", "speaker": "Alice", "text": "Already here."},
            {"key": "row-2", "speaker": "Bob", "text": "Only once."},
        ],
        ["row-1"],
    )
    tracker = CaptionTracker(0)
    emitter = _CaptureCaptionEmitter()

    _apply_for_test(tracker, emitter, baseline, transport="poll")
    _apply_for_test(tracker, emitter, update, transport="push")
    _apply_for_test(tracker, emitter, update, transport="poll")

    assert emitter.emits == [("host", "row-2", "Bob", "Only once.", True, None)]


def test_companion_click_row_break_metric_counts_new_host_row() -> None:
    holder = {
        "rawByRole": {"host": {"rawRows": []}},
        "companion_click_pending_breaks": [{"at": 100.0, "priorHostKey": "row-1", "observed": False}],
        "companion_click_row_breaks_observed": 0,
    }
    status: dict = {}
    emitter = _CaptureCaptionEmitter()

    apply_caption_payload(
        "host",
        _caption_payload([{"key": "row-2", "speaker": "Alice", "text": "New row."}], ["row-2"]),
        holder=holder,
        status=status,
        tracker=CaptionTracker(0),
        caption_emitter=emitter,
        captions_lock=threading.Lock(),
        now=101.0,
    )

    assert status["companionClick"]["rowBreaksObserved"] == 1


def test_companion_click_artifact_rows_are_marked_and_not_emitted() -> None:
    holder = {
        "rawByRole": {"companion": {"rawRows": []}},
        "companion_click_artifact_until": 20.0,
    }
    status: dict = {}
    emitter = _CaptureCaptionEmitter()

    apply_caption_payload(
        "companion",
        _caption_payload([{"key": "click-row", "speaker": "Companion", "text": "uh"}], ["click-row"]),
        holder=holder,
        status=status,
        tracker=CaptionTracker(0),
        caption_emitter=emitter,
        captions_lock=threading.Lock(),
        now=10.0,
    )

    assert emitter.emits == []
    assert status["rawByRole"]["companion"]["rawRows"][0]["clickArtifact"] is True
    assert status["companionClickArtifactsSuppressed"] == 1


def test_role_prefixed_caption_keys_and_replaces_do_not_collide() -> None:
    holder = {
        "url": "https://meet.google.com/abc-defg-hij",
        "rawByRole": {
            "host": {"rawRows": [{"key": "row-1", "rawText": "Alice | host sentence.", "childCount": 2}]},
            "companion": {"rawRows": [{"key": "row-1", "rawText": "Alice | companion sentence.", "childCount": 2}]},
        },
    }
    status: dict = {}
    captions_log: list[dict] = []
    mailbox = _FakeMailbox()
    emitter = _emitter(holder, status, captions_log, mailbox)

    emitter.emit("host", "row-1", "Alice", "Host sentence.", final=True)
    emitter.emit("companion", "row-1#1", "Alice", "Companion sentence.", final=True, replaces="row-1")

    assert role_caption_key("host", "row-1") == "host:row-1"
    assert [row["key"] for row in captions_log] == ["host:row-1", "companion:row-1#1"]
    assert captions_log[1]["replaces"] == "companion:row-1"
    assert captions_log[0]["rawText"] == "Alice | host sentence."
    assert captions_log[1]["rawText"] == "Alice | companion sentence."


def test_duplicate_caption_from_second_role_skips_mailbox_and_stt_side_effects() -> None:
    holder = {
        "url": "https://meet.google.com/abc-defg-hij",
        "rawByRole": {
            "host": {"rawRows": [{"key": "row-1", "rawText": "Alice | Same sentence.", "childCount": 2}]},
            "companion": {"rawRows": [{"key": "row-1", "rawText": "Alice | Same sentence.", "childCount": 2}]},
        },
    }
    status: dict = {}
    captions_log: list[dict] = []
    mailbox = _FakeMailbox()
    emitter = _emitter(holder, status, captions_log, mailbox)

    emitter.emit("host", "row-1", "Alice", "Same sentence.", final=True)
    emitter.emit("companion", "row-1", "Alice", "  same   sentence.  ", final=True)

    assert len(mailbox.sent) == 1
    assert len(mailbox.ingested) == 1
    assert [row["role"] for row in captions_log] == ["host", "companion"]
    assert captions_log[0]["duplicateOf"] is None
    assert captions_log[1]["duplicateOf"] == "host:row-1"


def test_companion_caption_read_failure_still_returns_host_payload() -> None:
    class HostTab:
        def evaluate(self, _script: str) -> str:
            return json.dumps({"ok": True, "rows": [{"key": "row-1", "speaker": "Alice", "text": "Hello."}], "liveKeys": ["row-1"]})

    class BrokenCompanionTab:
        def evaluate(self, _script: str) -> str:
            raise RuntimeError("closed")

    logs: list[tuple[str, str]] = []

    payloads = read_caption_payloads(
        {"tab": HostTab(), "companion_tab": BrokenCompanionTab()},
        log=lambda text, **kwargs: logs.append((text, kwargs.get("role", ""))),
    )

    assert payloads == [("host", {"ok": True, "rows": [{"key": "row-1", "speaker": "Alice", "text": "Hello."}], "liveKeys": ["row-1"]})]
    assert logs == [("[captions] companion read failed: closed", "companion")]


def test_service_captions_proxy_preserves_raw_payload_and_query(service, monkeypatch) -> None:
    worker_payload = {
        "captions": [{"key": "row-1", "updated_at": 12.0, "speaker": "Alice", "text": "parsed", "rawText": "Alice | parsed"}],
        "now": 13.0,
        "rawText": "Alice | parsed ‖ Bob | second",
        "rawRows": [
            {"key": "row-1", "rawText": "Alice | parsed", "childCount": 2},
            {"key": "row-2", "rawText": "Bob | second", "childCount": 2},
        ],
        "rawAt": 12.5,
        "rawHistory": [{"at": 12.5, "rawText": "Alice | parsed ‖ Bob | second", "rawRows": []}],
        "diagnostic": {"kept": True},
    }
    calls: list[tuple[str, float]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self) -> bytes:
            return json.dumps(worker_payload).encode("utf-8")

    def fake_urlopen(url: str, *, timeout: float):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert service.meet_bridge_captions(since="12.25", from_end="3") == worker_payload
    query = parse_qs(urlparse(calls[0][0]).query)
    assert query == {"since": ["12.25"], "fromEnd": ["3"]}
    assert calls[0][1] == 2.0


def test_rest_captions_route_preserves_worker_payload_and_query(client, viewer_headers, app_context, monkeypatch) -> None:
    worker_payload = {
        "captions": [{"key": "row-1", "speaker": "Speaker", "text": "parsed", "rawText": "Speaker | parsed"}],
        "now": 13.0,
        "rawText": "Speaker | parsed ‖ Bob | second",
        "rawRows": [
            {"key": "row-1", "rawText": "Speaker | parsed", "childCount": 2},
            {"key": "row-2", "rawText": "Bob | second", "childCount": 2},
        ],
        "rawAt": 12.5,
        "rawHistory": [{"at": 12.5, "rawText": "Speaker | parsed ‖ Bob | second", "rawRows": []}],
        "diagnostic": {"passthrough": True},
    }
    captured: dict[str, str | None] = {}

    def fake_captions(*, since: str = "0", from_end: str | None = None) -> dict:
        captured["since"] = since
        captured["from_end"] = from_end
        return worker_payload

    monkeypatch.setattr(app_context.service, "meet_bridge_captions", fake_captions)

    response = client.get(
        f"{V1}/meet/bridge/captions",
        headers=viewer_headers,
        params={"since": "12.25", "fromEnd": "3"},
    )

    assert response.status_code == 200
    assert response.json() == worker_payload
    assert captured == {"since": "12.25", "from_end": "3"}
