from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from ws_collab.meet_bridge import cdp
from ws_collab.meet_bridge import navigator


@pytest.fixture(autouse=True)
def approve_native_consent_for_test():
    navigator.set_consent_required_provider(lambda: True)
    navigator.set_consent_provider(lambda _request: navigator.ConsentDecision.ALLOW_OPERATION)
    yield
    navigator.set_consent_provider(None)
    navigator.set_consent_required_provider(None)


def test_reuse_or_open_tab_navigates_and_focuses_without_opening(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeTab:
        def __init__(self, ws_url: str) -> None:
            calls.append(("connect", ws_url))
            self.url = existing["url"]

        def call(self, method: str, params=None, timeout: float = 10.0):
            calls.append((method, params))
            if method == "Page.navigate":
                self.url = params["url"]

        def evaluate(self, _expression):
            return self.url

        def bring_to_front(self) -> None:
            calls.append(("focus", None))

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(
        cdp,
        "open_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an existing connector tab must not open a second tab")
        ),
    )
    existing = {
        "id": "host-tab",
        "url": "https://meet.google.com/old-room-url?authuser=7",
    }
    controlled = {
        **existing,
        "webSocketDebuggerUrl": "ws://host-tab",
    }
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: [controlled])

    info, reused = cdp.reuse_or_open_tab(
        "http://127.0.0.1:9223",
        "https://meet.google.com/new-room-url?authuser=7",
        existing_in_scope=existing,
        navigate_existing=True,
    )

    assert reused is True
    assert info == {
        **controlled,
        "url": "https://meet.google.com/new-room-url?authuser=7",
    }
    assert ("Page.navigate", {"url": "https://meet.google.com/new-room-url?authuser=7"}) in calls
    assert ("focus", None) in calls
    assert calls[-1] == ("close", None)


def test_reuse_or_open_tab_does_not_consume_a_tab_from_another_scope(monkeypatch) -> None:
    opened = {
        "id": "new-scope",
        "url": "https://accounts.google.com/?authuser=7",
        "webSocketDebuggerUrl": "ws://new-scope",
    }

    class FakeTab:
        def __init__(self, ws_url: str) -> None:
            assert ws_url == "ws://new-scope"
            self.url = opened["url"]

        def evaluate(self, _expression):
            return self.url

        def bring_to_front(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(cdp, "open_url", lambda _endpoint, _target: opened)
    monkeypatch.setattr(
        cdp,
        "list_tabs",
        lambda _endpoint: (_ for _ in ()).throw(
            AssertionError("the reuse helper must not globally choose a similar-looking tab")
        ),
    )

    info, reused = cdp.reuse_or_open_tab(
        "http://127.0.0.1:9223",
        opened["url"],
        existing_in_scope=None,
        sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
    )

    assert reused is False
    assert info is opened


def test_reuse_or_open_new_tab_waits_for_delayed_auth_redirect(monkeypatch) -> None:
    redirected = threading.Event()
    opened = {
        "id": "new-neutral",
        "url": "https://meet.google.com/abc-defg-hij",
        "webSocketDebuggerUrl": "ws://new-neutral",
    }

    class FakeTab:
        def __init__(self, _ws_url: str) -> None:
            self.url = opened["url"]
            threading.Timer(0.03, self._redirect).start()

        def _redirect(self):
            self.url = "https://accounts.google.com/AccountChooser?code=secret"
            redirected.set()

        def call(self, method: str, params=None, timeout: float = 10.0):
            assert method in {"Page.enable", "Page.stopLoading"}

        def drain_events(self):
            return []

        def wait_for_navigation_settled(self, *, timeout=5.0, **_kwargs):
            assert redirected.wait(timeout)

        def evaluate(self, expression):
            if expression == "document.readyState":
                return "loading"
            return self.url

        def bring_to_front(self):
            raise AssertionError("unexpected auth tab must not be foregrounded as success")

        def close(self):
            return None

    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(cdp, "open_url", lambda *_args, **_kwargs: dict(opened))

    with pytest.raises(navigator.UnexpectedAuthLandingError):
        cdp.reuse_or_open_tab(
            "http://127.0.0.1:9223",
            opened["url"],
            existing_in_scope=None,
        )


def test_sso_discovery_excludes_same_identity_in_other_connectors(monkeypatch) -> None:
    tabs = [
        {
            "id": "add-account-connector",
            "type": "page",
            "url": "https://accounts.google.com/AccountChooser?continue=x",
            "webSocketDebuggerUrl": "ws://chooser",
        },
        {
            "id": "meet-connector",
            "type": "page",
            "url": "https://meet.google.com/abc-defg-hij?authuser=2",
            "webSocketDebuggerUrl": "ws://meet",
        },
        {
            "id": "sso-connector",
            "type": "page",
            "url": "https://myaccount.google.com/?authuser=2",
            "webSocketDebuggerUrl": "ws://sso",
        },
    ]

    class FakeTab:
        def __init__(self, ws_url: str) -> None:
            self.ws_url = ws_url

        def close(self) -> None:
            return None

    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(
        cdp,
        "read_google_account",
        lambda tab: {"email": "same@example.test", "signedIn": True},
    )
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: tabs)

    found = cdp.find_sso_connector_tab("http://127.0.0.1:9223", "same@example.test")

    assert found["id"] == "sso-connector"
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: [tabs[1]])
    assert cdp.find_sso_connector_tab("http://127.0.0.1:9223", "same@example.test") is None
    assert cdp.find_sso_tab("http://127.0.0.1:9223", "same@example.test")["id"] == "meet-connector"


def test_add_account_discovery_preserves_account_specific_sso_tabs(monkeypatch) -> None:
    tabs = [
        {
            "id": "assigned-sso",
            "type": "page",
            "url": "https://accounts.google.com/?authuser=2",
            "webSocketDebuggerUrl": "ws://assigned",
        },
        {
            "id": "add-account",
            "type": "page",
            "url": "https://accounts.google.com/v3/signin/accountchooser?continue=x",
            "webSocketDebuggerUrl": "ws://chooser",
        },
    ]
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: tabs)

    found = cdp.find_add_account_tab("http://127.0.0.1:9223")

    assert found["id"] == "add-account"


def test_repeated_account_chooser_setup_reuses_one_scoped_tab(monkeypatch) -> None:
    chooser = {
        "id": "add-account",
        "type": "page",
        "url": "https://accounts.google.com/v3/signin/accountchooser?continue=x",
        "webSocketDebuggerUrl": "ws://chooser",
    }
    calls: list[str] = []

    class FakeTab:
        def __init__(self, ws_url: str) -> None:
            assert ws_url == "ws://chooser"
            self.url = chooser["url"]

        def call(self, method: str, params=None, timeout: float = 10.0):
            calls.append(method)
            if method == "Page.navigate":
                self.url = params["url"]

        def evaluate(self, _expression):
            return self.url

        def bring_to_front(self) -> None:
            calls.append("Page.bringToFront")

        def close(self) -> None:
            return None

    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: [chooser])
    monkeypatch.setattr(
        cdp,
        "open_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("repeated setup must reuse the scoped chooser")
        ),
    )

    for _ in range(16):
        existing = cdp.find_add_account_tab("http://127.0.0.1:9223")
        info, reused = cdp.reuse_or_open_tab(
            "http://127.0.0.1:9223",
            "https://accounts.google.com/AccountChooser?continue=x",
            existing_in_scope=existing,
            navigate_existing=True,
            sso_intent=navigator.SsoIntent.ADD_ACCOUNT,
        )
        assert reused is True
        assert info["id"] == "add-account"

    assert calls.count("Page.navigate") == 16
    assert calls.count("Page.enable") == 16
    assert calls.count("Page.bringToFront") == 16


def test_launch_browser_defers_existing_window_tab_choice_to_scoped_orchestration(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cdp, "cdp_alive", lambda _endpoint: True)
    monkeypatch.setattr(
        cdp,
        "open_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("role-aware orchestration owns tabs in an existing window")
        ),
    )
    args = SimpleNamespace(
        port=9223,
        profile=tmp_path / "profile",
        meet="https://meet.google.com/abc-defg-hij",
        new=False,
        browser=None,
        browser_backend="windows",
        wsl_distro=None,
        launch_url="https://accounts.google.com/",
    )

    endpoint, process = cdp.launch_browser(args)

    assert endpoint == "http://127.0.0.1:9223"
    assert process is None
