from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ws_collab.meet_bridge import cdp, navigator
from ws_collab.meet_bridge.mailbox_client import BrowserNavIntentPoster
from ws_collab.meet_browser_settings import MeetBrowserSettings


ENDPOINT = "http://127.0.0.1:9223"


@pytest.fixture
def nav_records(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(navigator, "_BROWSER_NAV_LOGGER", records.append)
    monkeypatch.setattr(navigator, "_INSTANCE_ID", "test-instance")
    monkeypatch.setattr(navigator, "_DEFAULT_LOG_COMPONENT", "test")
    monkeypatch.setattr(navigator, "_DEFAULT_LOG_ROLE", "test-role")
    monkeypatch.setattr(navigator, "_BROWSER_PROFILE_CACHE", {})
    monkeypatch.setattr(navigator, "_BACKEND_OVERRIDE", None)
    navigator.set_consent_required_provider(lambda: True)
    navigator.set_consent_provider(lambda _request: navigator.ConsentDecision.ALLOW_OPERATION)
    yield records
    navigator.set_consent_provider(None)
    navigator.set_consent_required_provider(None)


def outcomes(records: list[dict]) -> list[dict]:
    return [row for row in records if row["phase"] == "outcome"]


def test_fake_backend_primitives_emit_intent_and_outcome(nav_records, tmp_path) -> None:
    backend = navigator.InMemoryBrowserBackend()
    profile = {"slug": "account-a", "path": str(tmp_path / "account-a"), "display": "Account A"}
    opened = navigator.open_url(
        ENDPOINT, "https://meet.google.com/abc-defg-hij?authuser=0",
        reason="join", detail="open room", role="host", chrome_profile=profile,
        intended_identity="first@example.test", backend=backend,
    )
    page = backend.attach(navigator.BrowserTarget.from_legacy(opened))
    navigator.navigate(
        page, "https://meet.google.com/new-room?authuser=0",
        cdp_endpoint=ENDPOINT, reason="switch", detail="switch room", role="host",
        tab_info=opened, intended_identity="first@example.test", backend=backend,
    )
    reused, was_reused = navigator.reuse_or_open(
        ENDPOINT, page.url, existing_in_scope=page.to_legacy(), navigate_existing=False,
        reason="reuse", detail="reuse room", role="host", backend=backend,
    )
    navigator.evaluate_location_href(
        page, "https://meet.google.com/href-room", cdp_endpoint=ENDPOINT,
        reason="href", detail="JS href", role="host", backend=backend,
    )
    navigator.evaluate_location_replace(
        page, "https://meet.google.com/replace-room", cdp_endpoint=ENDPOINT,
        reason="replace", detail="JS replace", role="host", backend=backend,
    )
    navigator.evaluate_location_reload(
        page, page.url, cdp_endpoint=ENDPOINT,
        reason="reload", detail="JS reload", role="host", backend=backend,
    )
    navigator.foreground(
        page, page.url, cdp_endpoint=ENDPOINT,
        reason="foreground", detail="raise tab", role="host", backend=backend,
    )
    process = navigator.launch(
        ["fake-browser"], cdp_endpoint=ENDPOINT, url="about:blank", profile=tmp_path / "profile",
        reason="launch", detail="launch fake browser", role="host", backend=backend,
    )

    assert opened and reused and was_reused and process.pid == 1
    assert {row["outcome"] for row in outcomes(nav_records)} >= {
        "opened", "navigated", "reused-existing-tab", "foregrounded",
    }
    assert all(row["backend"] == "memory" for row in nav_records)
    assert any(row["chrome_profile"]["slug"] == "account-a" for row in nav_records)
    assert len([row for row in nav_records if row["phase"] == "intent"]) == len(outcomes(nav_records))


def test_open_failure_is_logged_without_credentials(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    backend.fail_next = RuntimeError("access_token=secret-value Authorization: Bearer hidden")
    assert navigator.open_url(
        ENDPOINT, "https://example.test/?code=oauth-code&authuser=7",
        reason="test", detail="token=detail-secret", role="host", backend=backend,
    ) is None
    serialized = repr(nav_records)
    assert "secret-value" not in serialized
    assert "oauth-code" not in serialized
    assert "detail-secret" not in serialized
    assert "authuser=7" in serialized
    assert outcomes(nav_records)[-1]["outcome"] == "failed"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://accounts.google.com/AccountChooser", navigator.UrlKind.GOOGLE_AUTH),
        ("https://myaccount.google.com/?authuser=1", navigator.UrlKind.GOOGLE_AUTH),
        ("https://meet.google.com/abc-defg-hij?authuser=1", navigator.UrlKind.GOOGLE_PROVIDER),
        ("https://discord.com/login", navigator.UrlKind.DISCORD_AUTH),
        ("https://discord.com/oauth2/authorize?client_id=1", navigator.UrlKind.DISCORD_AUTH),
        ("https://accounts.google.com.evil.test/login", navigator.UrlKind.NEUTRAL),
        ("https://discord.com.evil.test/login", navigator.UrlKind.NEUTRAL),
        ("https://example.test/?authuser=1", navigator.UrlKind.NEUTRAL),
    ],
)
def test_provider_classifier_exact_hosts(url, kind) -> None:
    assert navigator.classify_url(url) == kind


def test_auth_requires_explicit_sso_but_meet_is_ungated(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://accounts.google.com/",
            reason="implicit", detail="must be denied", role="host", backend=backend,
        )
    assert outcomes(nav_records)[-1]["outcome"] == "blocked"
    assert backend.actions == []

    navigator.open_url(
        ENDPOINT, "https://accounts.google.com/",
        reason="setup", detail="explicit operator setup", role="host",
        sso_intent=navigator.SsoIntent.SETUP_LANDING, origin="operator", backend=backend,
    )
    navigator.open_url(
        ENDPOINT, "https://meet.google.com/abc-defg-hij",
        reason="join", detail="normal Meet room", role="host", backend=backend,
    )
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://discord.com/login",
            reason="anonymous", detail="must not authenticate", role="guest",
            identity_mode=navigator.IdentityMode.ANONYMOUS,
            sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
            backend=backend,
        )


def test_auth_without_typed_intent_never_calls_consent_provider(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    requests = []
    navigator.set_consent_provider(requests.append)

    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://accounts.google.com/?code=secret",
            reason="caller prose says login", detail="please authenticate", role="host",
            backend=backend,
        )

    assert requests == []
    assert backend.actions == []
    assert outcomes(nav_records)[-1]["outcome"] == "blocked"
    assert "secret" not in repr(nav_records)


@pytest.mark.parametrize("configured", (None, False), ids=("missing-default", "explicit-false"))
def test_consent_defaults_off_and_typed_auth_logs_not_required(
    nav_records, configured
) -> None:
    backend = navigator.InMemoryBrowserBackend()
    requests = []
    navigator.set_consent_required_provider(
        None if configured is None else lambda: configured
    )
    navigator.set_consent_provider(requests.append)

    opened = navigator.open_url(
        ENDPOINT,
        "https://accounts.google.com/AccountChooser?authuser=1",
        reason="operator-request",
        detail="open selected identity",
        role="host",
        sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
        origin="operator",
        backend=backend,
    )

    assert opened is not None
    assert requests == []
    disabled = [row for row in nav_records if row["outcome"] == "consent-disabled"]
    assert len(disabled) == 1
    assert disabled[0]["consent_scope"] == "not-required"
    assert outcomes(nav_records)[-1]["outcome"] == "opened"


def test_consent_off_does_not_bypass_typed_intent_gate(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    requests = []
    navigator.set_consent_required_provider(lambda: False)
    navigator.set_consent_provider(requests.append)

    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT,
            "https://accounts.google.com/",
            reason="implicit",
            detail="no typed intent",
            role="host",
            backend=backend,
        )

    assert requests == []
    assert backend.actions == []
    assert outcomes(nav_records)[-1]["outcome"] == "blocked"


def test_explicit_auth_consent_precedes_backend_and_logs_lifecycle(nav_records, tmp_path) -> None:
    events: list[str] = []
    backend = navigator.InMemoryBrowserBackend()

    def consent(request):
        events.append("consent")
        assert request.provider == "google"
        assert request.chrome_profile["slug"] == "account-a"
        assert request.intended_identity == "person@example.test"
        return navigator.ConsentDecision.ALLOW_ONCE

    navigator.set_consent_provider(consent)
    original_open = backend.open_tab

    def open_after_consent(endpoint, url):
        events.append("backend")
        return original_open(endpoint, url)

    backend.open_tab = open_after_consent
    navigator.open_url(
        ENDPOINT, "https://accounts.google.com/?authuser=1&code=secret&cookie=cookie-secret",
        reason="operator-request", detail="open selected identity; token=hidden",
        role="host", component="test-component",
        chrome_profile={"slug": "account-a", "path": str(tmp_path / "account-a")},
        intended_identity="person@example.test",
        sso_intent=navigator.SsoIntent.OPERATOR_REQUEST,
        origin="operator", backend=backend,
    )

    assert events == ["consent", "backend"]
    assert [row["outcome"] for row in nav_records] == [
        "intent-recorded", "awaiting-consent", "approved", "opened",
    ]
    serialized = repr(nav_records)
    assert "secret" not in serialized and "hidden" not in serialized


@pytest.mark.parametrize(
    ("decision", "final_outcome"),
    [
        (navigator.ConsentDecision.DENY, "blocked"),
        (None, "blocked"),
    ],
)
def test_denied_or_closed_consent_never_navigates(nav_records, decision, final_outcome) -> None:
    backend = navigator.InMemoryBrowserBackend()
    navigator.set_consent_provider(lambda _request: decision)
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://discord.com/login?token=secret",
            reason="operator-request", detail="Discord login", role="host",
            sso_intent=navigator.SsoIntent.OPERATOR_REQUEST, origin="operator",
            backend=backend,
        )
    assert backend.actions == []
    assert outcomes(nav_records)[-1]["outcome"] == final_outcome
    assert any(row["outcome"] == "denied" for row in nav_records)
    assert "secret" not in repr(nav_records)


def test_consent_exception_and_headless_default_fail_closed(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()

    def broken(_request):
        raise RuntimeError("cookie=session-secret")

    navigator.set_consent_provider(broken)
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://accounts.google.com/",
            reason="setup", detail="setup", role="host",
            sso_intent=navigator.SsoIntent.SETUP_LANDING, backend=backend,
        )
    assert outcomes(nav_records)[-1]["outcome"] == "awaiting-consent"
    assert "session-secret" not in repr(nav_records)

    nav_records.clear()
    navigator.set_consent_provider(None)
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://accounts.google.com/",
            reason="setup", detail="headless test", role="host",
            sso_intent=navigator.SsoIntent.SETUP_LANDING, backend=backend,
        )
    assert backend.actions == []
    assert outcomes(nav_records)[-1]["outcome"] == "awaiting-consent"


def test_non_auth_surfaces_never_prompt(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    navigator.set_consent_provider(
        lambda _request: (_ for _ in ()).throw(AssertionError("must not prompt"))
    )
    navigator.open_url(
        ENDPOINT, "https://meet.google.com/abc-defg-hij",
        reason="join", detail="room", role="host", backend=backend,
    )
    navigator.open_url(
        ENDPOINT, "chrome://version",
        reason="profile-probe", detail="profile", role="probe", backend=backend,
    )
    assert [action for action, _ in backend.actions if action == "open"] == ["open", "open"]


def test_operation_consent_scope_is_exact(nav_records, tmp_path) -> None:
    backend = navigator.InMemoryBrowserBackend()
    requests = []

    def consent(request):
        requests.append(request)
        return (
            navigator.ConsentDecision.ALLOW_OPERATION
            if len(requests) == 1
            else navigator.ConsentDecision.DENY
        )

    navigator.set_consent_provider(consent)
    common = dict(
        reason="scan", detail="exact scan", role="probe",
        sso_intent=navigator.SsoIntent.PREFLIGHT_SCAN,
        consent_operation_id="scan-1", allow_operation_scope=True, backend=backend,
    )
    profile_a = {"slug": "a", "path": str(tmp_path / "a")}
    navigator.open_url(
        ENDPOINT, "https://myaccount.google.com/?authuser=0",
        chrome_profile=profile_a, **common,
    )
    navigator.open_url(
        ENDPOINT, "https://myaccount.google.com/?authuser=1",
        chrome_profile=profile_a, **common,
    )
    assert len(requests) == 1

    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://myaccount.google.com/?authuser=2",
            chrome_profile={"slug": "b", "path": str(tmp_path / "b")}, **common,
        )
    with pytest.raises(navigator.NavigationBlockedError):
        navigator.open_url(
            ENDPOINT, "https://accounts.google.com/",
            chrome_profile=profile_a, reason="setup", detail="different intent", role="probe",
            sso_intent=navigator.SsoIntent.SETUP_LANDING,
            consent_operation_id="scan-1", allow_operation_scope=True, backend=backend,
        )
    assert len(requests) == 3


def test_disabled_scan_never_prompts_but_enabled_scan_keeps_batching(
    monkeypatch, nav_records
) -> None:
    backend = navigator.InMemoryBrowserBackend()
    requests = []
    enabled = False
    navigator.set_consent_required_provider(lambda: enabled)
    navigator.set_consent_provider(
        lambda request: (
            requests.append(request),
            navigator.ConsentDecision.ALLOW_OPERATION,
        )[1]
    )
    common = dict(
        reason="scan",
        detail="bounded scan",
        role="probe",
        sso_intent=navigator.SsoIntent.PREFLIGHT_SCAN,
        consent_operation_id="scan-live",
        allow_operation_scope=True,
        backend=backend,
    )

    navigator.open_url(ENDPOINT, "https://myaccount.google.com/?authuser=0", **common)
    navigator.open_url(ENDPOINT, "https://myaccount.google.com/?authuser=1", **common)
    assert requests == []
    assert len([row for row in nav_records if row["outcome"] == "consent-disabled"]) == 2

    enabled = True
    navigator.open_url(ENDPOINT, "https://myaccount.google.com/?authuser=2", **common)
    navigator.open_url(ENDPOINT, "https://myaccount.google.com/?authuser=3", **common)
    assert len(requests) == 1


def test_unexpected_auth_redirect_is_stopped_and_needs_attention(nav_records) -> None:
    class RedirectBackend(navigator.InMemoryBrowserBackend):
        def open_tab(self, endpoint, url):
            target, error, action = super().open_tab(endpoint, url)
            redirected = navigator.BrowserTarget(
                target.id, "https://accounts.google.com/?code=redirect-secret",
                target.websocket_url,
            )
            self.targets[target.id] = redirected
            return redirected, error, action

    backend = RedirectBackend()
    with pytest.raises(navigator.UnexpectedAuthLandingError):
        navigator.open_url(
            ENDPOINT, "https://meet.google.com/abc-defg-hij",
            reason="join", detail="room", role="host", backend=backend,
        )
    assert any(action == "stop" for action, _ in backend.actions)
    assert outcomes(nav_records)[-1]["outcome"] == "unexpected-auth-landing"
    assert "redirect-secret" not in repr(nav_records)


def test_identity_mismatch_is_visible_even_with_same_display_name(nav_records) -> None:
    backend = navigator.InMemoryBrowserBackend()
    navigator.open_url(
        ENDPOINT, "https://meet.google.com/abc-defg-hij",
        reason="join", detail="both accounts display as Douglas Miles", role="host",
        intended_identity="first@example.test", effective_identity="second@example.test",
        backend=backend,
    )
    record = outcomes(nav_records)[-1]
    assert record["identity_mismatch"] is True
    assert record["intended_identity"] == "first@example.test"
    assert record["effective_identity"] == "second@example.test"


def test_fake_backend_find_and_close() -> None:
    backend = navigator.InMemoryBrowserBackend()
    target, _, _ = backend.open_tab(ENDPOINT, "https://example.test/")
    assert target is not None
    assert backend.find(lambda row: row.url == "https://example.test/") == target
    backend.close_tab(target.id)
    assert backend.find(lambda _row: True) is None
    assert isinstance(backend, navigator.BrowserBackend)
    assert navigator.get_browser_backend("fake").name == "memory"
    with pytest.raises(NotImplementedError):
        navigator.get_browser_backend("wsl-x11")


def test_cdp_compatibility_shim_uses_selected_backend(nav_records, monkeypatch) -> None:
    backend = navigator.InMemoryBrowserBackend()
    monkeypatch.setattr(navigator, "_BACKEND_OVERRIDE", backend)
    info = cdp.open_url(
        ENDPOINT, "https://meet.google.com/abc-defg-hij",
        reason="shim", detail="factory seam", role="host",
    )
    assert info and info["url"].startswith("https://meet.google.com/")
    assert backend.actions == [("open", info["url"]), ("detach", info["id"])]
    assert outcomes(nav_records)[-1]["backend"] == "memory"


def test_profile_slug_resolution_is_lazy_and_checks_windows_risk(tmp_path, monkeypatch) -> None:
    path = navigator.profile_path_for_slug(tmp_path, "account-a")
    assert path.name == "account-a"
    assert not path.exists()
    assert navigator.profile_path_for_slug(tmp_path, "account-a", create=True).is_dir()
    with pytest.raises(ValueError):
        navigator.profile_path_for_slug(tmp_path, "..\\escape")
    monkeypatch.setattr(navigator.os, "name", "nt")
    with pytest.raises(ValueError, match="too long"):
        navigator.profile_path_for_slug(tmp_path / ("x" * 230), "account")


def test_profile_registry_is_account_centric_not_role_centric(tmp_path) -> None:
    settings = MeetBrowserSettings(tmp_path)
    first = settings.register_profile(
        "account-a", tmp_path / "chrome_profiles" / "account-a",
        display_name="Douglas Miles", intended_default_account="first@example.test",
    )
    second = settings.register_profile(
        "account-b", tmp_path / "chrome_profiles" / "account-b",
        display_name="Douglas Miles", intended_default_account="second@example.test",
    )
    assert first["slug"] != second["slug"]
    assert settings.profile_registry()["account-a"]["intended_default_account"] == "first@example.test"
    assert not (tmp_path / "chrome_profiles").exists()


def test_scan_logs_one_logical_outcome_per_authuser_slot(monkeypatch, nav_records) -> None:
    opened: list[str] = []
    consent_requests = []
    navigator.set_consent_provider(
        lambda request: (
            consent_requests.append(request),
            navigator.ConsentDecision.ALLOW_OPERATION,
        )[1]
    )

    def fake_http(url: str, *, method: str = "GET", timeout: float = 5.0):
        opened.append(url)
        return {"id": "probe-tab", "webSocketDebuggerUrl": "ws://probe-tab", "url": url}

    class FakeTab:
        def __init__(self, _ws_url: str) -> None:
            self.url = "https://myaccount.google.com/?authuser=0"

        def call(self, method: str, params=None, timeout: float = 10.0):
            if method == "Page.navigate":
                self.url = params["url"]
            else:
                assert method == "Page.enable"

        def evaluate(self, _expression):
            return self.url

        def close(self) -> None:
            return None

    monkeypatch.setattr(cdp, "_http_json", fake_http)
    monkeypatch.setattr(cdp, "CdpTab", FakeTab)
    monkeypatch.setattr(cdp, "read_google_account", lambda tab: {
        "signedIn": True,
        "email": f"user{tab.url.rsplit('=', 1)[1]}@example.test",
    })
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: [])
    monkeypatch.setattr(cdp, "close_tab", lambda *_args: None)

    accounts = cdp.scan_signed_in_sso_accounts(
        ENDPOINT, authusers=[0, 1], timeout=0.01, sso_satisfied=True,
    )
    rows = [row for row in outcomes(nav_records) if row["reason"] == "account-scan"]
    assert [row["authuser"] for row in accounts] == [0, 1]
    assert len(rows) == 2
    assert all(row["sso_intent"] == "preflight-scan" for row in rows)
    assert all("ssoSatisfied=true" in row["detail"] for row in rows)
    assert len(consent_requests) == 1
    assert consent_requests[0].allow_operation_scope is True


def test_denied_account_scan_emits_zero_probes(monkeypatch, nav_records) -> None:
    probes: list[str] = []
    navigator.set_consent_provider(lambda _request: navigator.ConsentDecision.DENY)
    monkeypatch.setattr(cdp, "list_tabs", lambda _endpoint: [])
    monkeypatch.setattr(
        cdp, "_http_json",
        lambda url, **_kwargs: probes.append(url) or {
            "id": "must-not-open", "webSocketDebuggerUrl": "ws://must-not-open", "url": url,
        },
    )

    with pytest.raises(navigator.NavigationBlockedError):
        cdp.scan_signed_in_sso_accounts(ENDPOINT, authusers=[0, 1], timeout=0.01)

    assert probes == []
    assert outcomes(nav_records)[-1]["outcome"] == "blocked"


@pytest.mark.parametrize("path", ("open", "navigate", "reuse"))
def test_delayed_unexpected_auth_redirect_fails_actual_navigation_paths(
    nav_records, path
) -> None:
    class DelayedRedirectBackend(navigator.InMemoryBrowserBackend):
        def __init__(self):
            super().__init__()
            self.committed = threading.Event()

        def _redirect(self, target_id):
            current = self.targets[target_id]
            self.targets[target_id] = navigator.BrowserTarget(
                current.id,
                "https://accounts.google.com/AccountChooser?code=delayed-secret",
                current.websocket_url,
            )
            self.committed.set()

        def open_tab(self, endpoint, url):
            target, error, action = super().open_tab(endpoint, url)
            threading.Timer(0.03, self._redirect, args=(target.id,)).start()
            return target, error, action

        def navigate(self, page, url):
            super().navigate(page, url)
            threading.Timer(0.03, self._redirect, args=(page.id,)).start()

        def wait_for_final_url(self, page, requested_url, timeout=5.0):
            assert self.committed.wait(timeout)
            return self.targets[page.id].url

    backend = DelayedRedirectBackend()
    kwargs = dict(
        cdp_endpoint=ENDPOINT,
        reason="join",
        detail="neutral URL asynchronously redirects",
        role="host",
        backend=backend,
    )
    with pytest.raises(navigator.UnexpectedAuthLandingError):
        if path == "open":
            navigator.open_url(target="https://meet.google.com/abc-defg-hij", **kwargs)
        else:
            initial, _, _ = navigator.InMemoryBrowserBackend.open_tab(
                backend, ENDPOINT, "https://meet.google.com/old-room"
            )
            if path == "navigate":
                navigator.navigate(
                    initial,
                    "https://meet.google.com/abc-defg-hij",
                    tab_info=initial.to_legacy(),
                    **kwargs,
                )
            else:
                navigator.reuse_or_open(
                    target="https://meet.google.com/abc-defg-hij",
                    existing_in_scope=initial.to_legacy(),
                    navigate_existing=True,
                    **kwargs,
                )

    final = outcomes(nav_records)[-1]
    assert final["outcome"] == "unexpected-auth-landing"
    assert "delayed-secret" not in repr(nav_records)
    assert not any(
        row["outcome"] in {"opened", "navigated"}
        for row in outcomes(nav_records)
    )


def test_launch_waits_for_delayed_redirect_and_rejects_unexpected_auth(
    nav_records, tmp_path
) -> None:
    class DelayedLaunchRedirectBackend(navigator.InMemoryBrowserBackend):
        def __init__(self):
            super().__init__()
            self.committed = threading.Event()

        def launch(self, argv):
            process = super().launch(argv)
            target, _, _ = self.open_tab(ENDPOINT, "https://meet.google.com/abc-defg-hij")

            def redirect():
                self.targets[target.id] = navigator.BrowserTarget(
                    target.id,
                    "https://accounts.google.com/AccountChooser?code=launch-secret",
                    target.websocket_url,
                )
                self.committed.set()

            threading.Timer(0.03, redirect).start()
            return process

        def wait_for_final_url(self, page, requested_url, timeout=5.0):
            assert self.committed.wait(timeout)
            return self.targets[page.id].url

    backend = DelayedLaunchRedirectBackend()
    with pytest.raises(navigator.UnexpectedAuthLandingError):
        navigator.launch(
            ["fake-browser"],
            cdp_endpoint=ENDPOINT,
            url="https://meet.google.com/abc-defg-hij",
            profile=tmp_path / "profile",
            reason="browser-launch",
            detail="launch neutral Meet target",
            role="host",
            wait_until_ready=lambda: True,
            ready_timeout=1.0,
            backend=backend,
        )

    assert any(action == "stop" for action, _ in backend.actions)
    assert outcomes(nav_records)[-1]["outcome"] == "unexpected-auth-landing"
    assert "launch-secret" not in repr(nav_records)
    assert not any(row["outcome"] == "opened" for row in outcomes(nav_records))


def test_launch_allows_typed_approved_auth_landing(nav_records, tmp_path) -> None:
    class AuthLaunchBackend(navigator.InMemoryBrowserBackend):
        def launch(self, argv):
            process = super().launch(argv)
            self.open_tab(ENDPOINT, "https://accounts.google.com/AccountChooser")
            return process

    backend = AuthLaunchBackend()
    process = navigator.launch(
        ["fake-browser"],
        cdp_endpoint=ENDPOINT,
        url="https://accounts.google.com/AccountChooser",
        profile=tmp_path / "profile",
        reason="add-account",
        detail="operator approved account chooser",
        role="host",
        sso_intent=navigator.SsoIntent.ADD_ACCOUNT,
        wait_until_ready=lambda: True,
        ready_timeout=1.0,
        backend=backend,
    )

    assert process.pid == 1
    assert outcomes(nav_records)[-1]["outcome"] == "opened"


def test_build_launch_discovers_injected_native_browser_candidate(tmp_path) -> None:
    browser = tmp_path / "chrome.exe"
    browser.write_bytes(b"test browser")

    argv = cdp.build_launch(
        "windows",
        9223,
        tmp_path / "profile",
        "https://meet.google.com/new",
        browser_candidates=[tmp_path / "missing.exe", browser],
        path_lookup=lambda _name: None,
    )

    assert argv[0] == str(browser)
    assert "--remote-debugging-port=9223" in argv
    assert argv[-1] == "https://meet.google.com/new"


def test_missing_native_browser_is_logged_as_failed_launch(
    nav_records, monkeypatch, tmp_path
) -> None:
    backend = navigator.InMemoryBrowserBackend()
    monkeypatch.setattr(navigator, "_BACKEND_OVERRIDE", backend)
    monkeypatch.setattr(cdp, "windows_browser_candidates", lambda: ())
    monkeypatch.setattr(cdp.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cdp, "cdp_alive", lambda _endpoint: False)
    args = SimpleNamespace(
        port=9223,
        profile=tmp_path / "profile",
        meet="https://meet.google.com/new",
        new=False,
        browser=None,
        browser_backend="windows",
        wsl_distro=None,
        launch_url=None,
    )

    with pytest.raises(SystemExit, match="Install Chrome/Edge or pass --browser"):
        cdp.launch_browser(args)

    assert outcomes(nav_records)[-1]["outcome"] == "failed"
    assert outcomes(nav_records)[-1]["action"] == "launch-browser"
    assert "No native Windows Chrome or Edge executable" in outcomes(nav_records)[-1]["error"]


def test_worker_launch_command_reaches_navigator_backend(
    nav_records, monkeypatch, tmp_path
) -> None:
    browser = tmp_path / "chrome.exe"
    browser.write_bytes(b"test browser")
    target = "https://meet.google.com/abc-defg-hij"

    class WorkerBackend(navigator.InMemoryBrowserBackend):
        def launch(self, argv):
            process = super().launch(argv)
            self.open_tab(ENDPOINT, target)
            return process

    backend = WorkerBackend()
    monkeypatch.setattr(navigator, "_BACKEND_OVERRIDE", backend)
    alive_checks = iter([False])
    monkeypatch.setattr(cdp, "cdp_alive", lambda _endpoint: next(alive_checks, True))
    args = SimpleNamespace(
        port=9223,
        profile=tmp_path / "profile",
        meet=target,
        new=False,
        browser=str(browser),
        browser_backend="windows",
        wsl_distro=None,
        launch_url=None,
    )

    endpoint, process = cdp.launch_browser(args)

    assert endpoint == ENDPOINT
    assert process is not None and process.pid == 1
    launch_action = next(value for action, value in backend.actions if action == "launch")
    assert launch_action.startswith(str(browser))
    assert "--remote-debugging-port=9223" in launch_action
    assert outcomes(nav_records)[-1]["outcome"] == "opened"


def test_rest_ingest_redacts_and_lists_merged_records(client, worker_headers, viewer_headers) -> None:
    nav_id = "nav-test-1"
    base = {
        "nav_id": nav_id,
        "ts": "2026-08-31T00:00:00.000Z",
        "pid": 42,
        "instance": "bridge:42:test",
        "component": "meet_bridge",
        "role": "host",
        "backend": "fake",
        "url": "https://example.test/?access_token=do-not-store&authuser=2",
        "reason": "test",
        "detail": "Authorization: Bearer do-not-store",
        "phase": "intent",
        "outcome": "awaiting-consent",
    }
    first = client.post("/ws_collab/v1/browser/nav-intents", headers=worker_headers, json=base)
    assert first.status_code == 200
    second = client.post(
        "/ws_collab/v1/browser/nav-intents",
        headers=worker_headers,
        json={**base, "phase": "outcome", "outcome": "opened", "tab_id": "tab-1"},
    )
    assert second.status_code == 200
    response = client.get(
        "/ws_collab/v1/browser/nav-intents?limit=10", headers=viewer_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 2
    assert len(body["records"]) == 1
    record = body["records"][0]
    assert record["outcome"] == "opened" and record["tab_id"] == "tab-1"
    assert "do-not-store" not in repr(body)
    assert "authuser=2" in record["url"]


def test_worker_poster_is_non_blocking_and_drops_oldest_while_offline() -> None:
    class OfflineClient:
        def post_browser_nav_intent(self, _payload):
            raise ConnectionError("offline")

    poster = BrowserNavIntentPoster(OfflineClient(), max_buffer=2, retry_seconds=10.0)
    started = time.perf_counter()
    for value in range(4):
        poster.submit({"nav_id": str(value)})
    assert time.perf_counter() - started < 0.2
    time.sleep(0.03)
    with poster._lock:
        queued = list(poster._queue)
    assert len(queued) <= 2
    assert all(row["nav_id"] != "0" for row in queued)


def test_browser_navigation_source_enforcement() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    forbidden = (
        "/json/new",
        "Page.navigate",
        "location.href =",
        "location.replace(",
        "location.reload(",
        "window.open(",
        "webbrowser.open(",
    )
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "navigator.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                violations.append(f"{path.relative_to(root)}: {needle}")
    assert violations == []


def test_profiles_directory_is_ignored_and_untracked() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = subprocess.run(
        ["git", "check-ignore", "chrome_profiles", "chrome_profiles/example"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "chrome_profiles"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    assert ignored.returncode == 0
    assert tracked.stdout.strip() == ""


def test_normal_startup_does_not_force_account_chooser() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/ws_collab/meet_bridge/bridge.py").read_text(encoding="utf-8")
    assert "args.launch_url = SSO_SETUP_URL" not in source


def test_admin_browser_feed_wires_required_identity_fields() -> None:
    root = Path(__file__).resolve().parents[1] / "src/ws_collab/admin"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    assert "Global browser navigation intents" in html
    assert "Intended / Effective" in html and "Endpoint / Profile" in html
    assert "identity_mismatch" in js and "unexpected-auth-landing" in js
    assert "browser-nav-danger" in css and "browser-nav-mismatch" in css
