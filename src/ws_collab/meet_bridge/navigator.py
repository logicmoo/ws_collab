"""Single chokepoint for Chrome page opens, navigations, and browser launch.

Every code path that can open or navigate a Chrome page must call this module
with explicit intent metadata. The module logs the intent before attempting the
action and logs the final outcome afterward.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request

_INSTANCE_ID = f"process:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_DEFAULT_LOG_COMPONENT = "navigator"
_DEFAULT_LOG_ROLE = "unknown"
_BROWSER_NAV_LOGGER: Callable[[dict[str, Any]], None] | None = None
_BROWSER_PROFILE_CACHE: dict[str, dict[str, str]] = {}
_LOGGING_LOCAL = threading.local()
_BACKEND_OVERRIDE: BrowserBackend | None = None
_CONSENT_PROVIDER: ConsentProvider | Callable[["ConsentRequest"], Any] | None = None
_CONSENT_REQUIRED_PROVIDER: Callable[[], bool] | None = None
_CONSENT_REQUIRED_LAST: bool | None = None
_CONSENT_APPROVALS: dict[tuple[str, str, str, str], float] = {}
_CONSENT_LOCK = threading.Lock()
_CONSENT_SCOPE_TTL_SECONDS = 120.0
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "id_token",
    "refresh_token",
    "token",
    "code",
    "client_secret",
    "password",
    "passcode",
    "samlresponse",
    "assertion",
    "ticket",
    "session",
    "sid",
    "state",
    "cookie",
    "set-cookie",
    "authorization",
}


class SsoIntent(str, Enum):
    PREFLIGHT_SCAN = "preflight-scan"
    ADD_ACCOUNT = "add-account"
    FOREGROUND_EXISTING = "foreground-existing"
    SETUP_LANDING = "setup-landing"
    OPERATOR_REQUEST = "operator-request"


class IdentityMode(str, Enum):
    AMBIENT = "ambient"
    SELECTED = "selected"
    ANONYMOUS = "anonymous"


class UrlKind(str, Enum):
    GOOGLE_AUTH = "google-auth"
    GOOGLE_PROVIDER = "google-provider"
    DISCORD_AUTH = "discord-auth"
    NEUTRAL = "neutral"


class NavigationBlockedError(RuntimeError):
    """Raised before a browser action that violates explicit identity policy."""


class UnexpectedAuthLandingError(NavigationBlockedError):
    """Raised when a non-auth navigation commits to an authentication surface."""


class ConsentDecision(str, Enum):
    ALLOW_ONCE = "allow-once"
    ALLOW_OPERATION = "allow-operation"
    DENY = "deny"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ConsentRequest:
    provider: str
    url: str
    reason: str
    detail: str
    component: str
    role: str
    chrome_profile: dict[str, str]
    intended_identity: str | None
    sso_intent: str
    origin: str
    caller: str
    operation_id: str | None = None
    allow_operation_scope: bool = False


@runtime_checkable
class ConsentProvider(Protocol):
    def request_consent(self, request: ConsentRequest) -> ConsentDecision | str | bool: ...


class TkConsentProvider:
    """Lazy native consent dialog. Importing this module never initializes a GUI."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))

    def request_consent(self, request: ConsentRequest) -> ConsentDecision:
        if os.name != "nt" or os.environ.get("PYTEST_CURRENT_TEST"):
            return ConsentDecision.UNAVAILABLE
        if str(os.environ.get("SESSIONNAME") or "").lower() == "services":
            return ConsentDecision.UNAVAILABLE
        try:
            import tkinter as tk
        except Exception:
            return ConsentDecision.UNAVAILABLE

        decision = ConsentDecision.DENY
        try:
            root = tk.Tk()
            root.withdraw()
            dialog = tk.Toplevel(root)
            dialog.title("WS Collab authentication consent")
            dialog.resizable(False, False)
            dialog.attributes("-topmost", True)
            profile = request.chrome_profile
            lines = [
                f"Provider: {request.provider}",
                f"URL: {request.url}",
                f"Reason: {request.reason}",
                f"Detail: {request.detail}",
                f"Caller: {request.component} / {request.role}",
                f"Runtime: {request.caller}",
                f"Chrome profile: {profile.get('slug', 'unresolved')}",
                f"Profile path: {profile.get('path') or 'unresolved'}",
                f"Intended identity: {request.intended_identity or 'not specified'}",
                f"Typed auth intent: {request.sso_intent}",
            ]
            tk.Label(
                dialog,
                text="\n".join(lines),
                justify="left",
                anchor="w",
                padx=18,
                pady=16,
                wraplength=680,
            ).pack(fill="both")
            buttons = tk.Frame(dialog, padx=18, pady=12)
            buttons.pack(fill="x")

            def finish(value: ConsentDecision) -> None:
                nonlocal decision
                decision = value
                dialog.destroy()

            allow_label = (
                "Allow this scan"
                if request.allow_operation_scope and request.sso_intent == SsoIntent.PREFLIGHT_SCAN.value
                else "Allow this exact operation"
                if request.allow_operation_scope
                else "Allow once"
            )
            allow_value = (
                ConsentDecision.ALLOW_OPERATION
                if request.allow_operation_scope
                else ConsentDecision.ALLOW_ONCE
            )
            tk.Button(buttons, text=allow_label, command=lambda: finish(allow_value)).pack(
                side="left", padx=(0, 10)
            )
            tk.Button(
                buttons, text="Deny", command=lambda: finish(ConsentDecision.DENY), default="active"
            ).pack(side="left")
            dialog.protocol("WM_DELETE_WINDOW", lambda: finish(ConsentDecision.DENY))
            dialog.after(int(self.timeout_seconds * 1000), lambda: finish(ConsentDecision.DENY))
            dialog.grab_set()
            dialog.focus_force()
            root.wait_window(dialog)
            root.destroy()
            return decision
        except Exception:
            try:
                root.destroy()
            except Exception:
                pass
            return ConsentDecision.UNAVAILABLE


@dataclass(frozen=True)
class BrowserTarget:
    id: str
    url: str
    websocket_url: str = ""
    title: str = ""
    type: str = "page"
    _legacy: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_legacy(cls, value: dict[str, Any]) -> "BrowserTarget":
        return cls(
            id=str(value.get("id") or ""),
            url=str(value.get("url") or ""),
            websocket_url=str(value.get("webSocketDebuggerUrl") or ""),
            title=str(value.get("title") or ""),
            type=str(value.get("type") or "page"),
            _legacy=value,
        )

    def to_legacy(self) -> dict[str, Any]:
        if self._legacy is not None and str(self._legacy.get("id") or "") == self.id and str(self._legacy.get("url") or "") == self.url:
            return self._legacy
        value: dict[str, Any] = {"id": self.id, "url": self.url}
        if self.websocket_url:
            value["webSocketDebuggerUrl"] = self.websocket_url
        if self.title:
            value["title"] = self.title
        if self.type and self.type != "page":
            value["type"] = self.type
        return value


@dataclass(frozen=True)
class BrowserProfile:
    slug: str
    path: Path
    display_name: str
    intended_default_account: str | None = None


def profile_path_for_slug(repository_root: Path, slug: str, *, create: bool = False) -> Path:
    """Resolve a safe repo-local profile and create it only on explicit use."""
    normalized = str(slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", normalized):
        raise ValueError("profile slug must contain only letters, numbers, dot, underscore, or dash")
    path = Path(repository_root).resolve() / "chrome_profiles" / normalized
    if os.name == "nt" and len(str(path)) > 220:
        raise ValueError("profile path is too long for reliable Chrome operation on Windows")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


@runtime_checkable
class BrowserBackend(Protocol):
    """Neutral browser operations used by :class:`BrowserNavigator`."""

    name: str

    def open_tab(self, endpoint: str, url: str) -> tuple[BrowserTarget | None, str | None, str]: ...
    def list_tabs(self, endpoint: str) -> list[BrowserTarget]: ...
    def attach(self, target: BrowserTarget) -> Any: ...
    def navigate(self, page: Any, url: str) -> Any: ...
    def foreground(self, page: Any) -> Any: ...
    def evaluate_navigation(self, page: Any, script: str) -> Any: ...
    def current_url(self, page: Any) -> str | None: ...
    def prepare_navigation(self, page: Any) -> None: ...
    def wait_for_final_url(self, page: Any, requested_url: str, timeout: float = 5.0) -> str | None: ...
    def stop(self, page: Any) -> None: ...
    def close_connection(self, page: Any) -> None: ...
    def launch(self, argv: list[str]) -> Any: ...


class CdpBrowserBackend:
    """Real Chrome backend. Raw CDP names and responses stay in this module."""

    name = "cdp"

    def __init__(
        self,
        *,
        http_json: Callable[..., Any] | None = None,
        tab_factory: Callable[[str], Any] | None = None,
        list_tabs: Callable[[str], list[dict[str, Any]]] | None = None,
        open_tab: Callable[[str, str], dict[str, Any] | None] | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self._http_json = http_json or _http_json
        self._tab_factory = tab_factory
        self._list_tabs = list_tabs
        self._open_tab_func = open_tab
        self._popen = popen

    def open_tab(self, endpoint: str, url: str) -> tuple[BrowserTarget | None, str | None, str]:
        if self._open_tab_func is not None:
            raw = self._open_tab_func(endpoint, url)
            return (BrowserTarget.from_legacy(raw) if isinstance(raw, dict) else None), None, "open-tab"
        try:
            raw = self._http_json(f"{endpoint}/json/new?{url}", method="PUT")
            target = BrowserTarget.from_legacy(raw) if isinstance(raw, dict) else None
            return target, None, "open-tab"
        except Exception as first_error:
            try:
                raw = self._http_json(f"{endpoint}/json/new?{url}", method="GET")
                target = BrowserTarget.from_legacy(raw) if isinstance(raw, dict) else None
                return target, None, "open-tab-fallback"
            except Exception as error:
                return None, f"{first_error}; fallback: {error}", "open-tab"

    def list_tabs(self, endpoint: str) -> list[BrowserTarget]:
        raw = self._list_tabs(endpoint) if self._list_tabs is not None else self._http_json(f"{endpoint}/json")
        return [BrowserTarget.from_legacy(item) for item in raw if isinstance(item, dict)]

    def attach(self, target: BrowserTarget) -> Any:
        if self._tab_factory is not None:
            return self._tab_factory(target.websocket_url)
        from .cdp import CdpTab
        return CdpTab(target.websocket_url)

    def navigate(self, page: Any, url: str) -> Any:
        return page.call("Page.navigate", {"url": url})

    def foreground(self, page: Any) -> Any:
        return page.bring_to_front()

    def evaluate_navigation(self, page: Any, script: str) -> Any:
        return page.evaluate(script)

    def current_url(self, page: Any) -> str | None:
        value = page.evaluate("location.href")
        return str(value) if value else None

    def prepare_navigation(self, page: Any) -> None:
        page.call("Page.enable")
        drain = getattr(page, "drain_events", None)
        if callable(drain):
            drain()

    def wait_for_final_url(self, page: Any, requested_url: str, timeout: float = 5.0) -> str | None:
        waiter = getattr(page, "wait_for_navigation_settled", None)
        if callable(waiter):
            page.call("Page.enable")
            ready_state = page.evaluate("document.readyState")
            waiter(timeout=timeout, require_event=str(ready_state) != "complete")
        return self.current_url(page)

    def stop(self, page: Any) -> None:
        page.call("Page.stopLoading")

    def close_connection(self, page: Any) -> None:
        page.close()

    def launch(self, argv: list[str]) -> Any:
        launcher = self._popen or subprocess.Popen
        return launcher(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class InMemoryBrowserBackend:
    """Deterministic backend for tests and future browser-independent callers."""

    name = "memory"

    def __init__(self) -> None:
        self.targets: dict[str, BrowserTarget] = {}
        self.actions: list[tuple[str, str]] = []
        self.fail_next: Exception | None = None

    def _fail(self) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    def open_tab(self, endpoint: str, url: str) -> tuple[BrowserTarget | None, str | None, str]:
        self._fail()
        target = BrowserTarget(uuid.uuid4().hex, url, "memory://page")
        self.targets[target.id] = target
        self.actions.append(("open", url))
        return target, None, "open-tab"

    def list_tabs(self, endpoint: str) -> list[BrowserTarget]:
        return list(self.targets.values())

    def attach(self, target: BrowserTarget) -> BrowserTarget:
        return target

    def navigate(self, page: BrowserTarget, url: str) -> None:
        self._fail()
        updated = BrowserTarget(page.id, url, page.websocket_url, page.title, page.type)
        self.targets[page.id] = updated
        object.__setattr__(page, "url", url)
        self.actions.append(("navigate", url))

    def foreground(self, page: BrowserTarget) -> None:
        self._fail()
        self.actions.append(("foreground", page.id))

    def evaluate_navigation(self, page: BrowserTarget, script: str) -> None:
        self._fail()
        match = re.search(r"location\.href\s*=\s*(\"(?:[^\"\\]|\\.)*\")", script)
        if match:
            self.navigate(page, json.loads(match.group(1)))
        else:
            self.actions.append(("script", script))

    def current_url(self, page: BrowserTarget) -> str | None:
        return page.url

    def prepare_navigation(self, page: BrowserTarget) -> None:
        return None

    def wait_for_final_url(
        self, page: BrowserTarget, requested_url: str, timeout: float = 5.0
    ) -> str | None:
        return self.current_url(page)

    def stop(self, page: BrowserTarget) -> None:
        self.actions.append(("stop", page.id))

    def close_connection(self, page: BrowserTarget) -> None:
        self.actions.append(("detach", page.id))

    def close_tab(self, target_id: str) -> None:
        self.targets.pop(target_id, None)
        self.actions.append(("close", target_id))

    def find(self, predicate: Callable[[BrowserTarget], bool]) -> BrowserTarget | None:
        return next((target for target in self.targets.values() if predicate(target)), None)

    def launch(self, argv: list[str]) -> Any:
        self._fail()
        self.actions.append(("launch", " ".join(argv)))
        return type("MemoryProcess", (), {"pid": 1})()


def set_browser_backend(backend: BrowserBackend | None) -> None:
    global _BACKEND_OVERRIDE
    _BACKEND_OVERRIDE = backend


def set_consent_provider(
    provider: ConsentProvider | Callable[[ConsentRequest], Any] | None,
) -> None:
    """Inject the process consent provider; ``None`` restores the native provider."""
    global _CONSENT_PROVIDER
    _CONSENT_PROVIDER = provider
    clear_consent_approvals()


def set_consent_required_provider(provider: Callable[[], bool] | None) -> None:
    """Set the live global consent opt-in reader; ``None`` means disabled."""
    global _CONSENT_REQUIRED_PROVIDER, _CONSENT_REQUIRED_LAST
    _CONSENT_REQUIRED_PROVIDER = provider
    _CONSENT_REQUIRED_LAST = None
    clear_consent_approvals()


def consent_confirmation_required() -> bool:
    """Read the opt-in at navigation time so persisted changes apply live."""
    global _CONSENT_REQUIRED_LAST
    provider = _CONSENT_REQUIRED_PROVIDER
    try:
        required = provider() is True if provider is not None else False
    except Exception:
        required = False
    if _CONSENT_REQUIRED_LAST is not None and required != _CONSENT_REQUIRED_LAST:
        clear_consent_approvals()
    _CONSENT_REQUIRED_LAST = required
    return required


def clear_consent_approvals() -> None:
    with _CONSENT_LOCK:
        _CONSENT_APPROVALS.clear()


def get_browser_backend(name: str | None = None) -> BrowserBackend:
    if _BACKEND_OVERRIDE is not None and name in (None, "", _BACKEND_OVERRIDE.name):
        return _BACKEND_OVERRIDE
    selected = (name or os.environ.get("WS_COLLAB_BROWSER_NAV_BACKEND") or "cdp").strip().lower()
    if selected in {"cdp", "chrome", "windows"}:
        return CdpBrowserBackend()
    if selected in {"memory", "fake"}:
        return InMemoryBrowserBackend()
    if selected in {"wsl", "wsl-x11"}:
        raise NotImplementedError("WSL/X11 browser navigation backend is intentionally not implemented")
    raise ValueError(f"unknown browser navigation backend: {selected}")


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local trusted CDP endpoint
        return json.loads(response.read().decode("utf-8"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _profile_record(profile: str | Path | dict[str, Any] | None) -> dict[str, str] | None:
    if profile is None:
        return None
    if isinstance(profile, dict):
        path = str(profile.get("path") or "").strip()
        display = str(profile.get("display") or profile.get("name") or "").strip()
        if not path and not display:
            return None
        slug = str(profile.get("slug") or "").strip() or (Path(path).name if path else display)
        return {
            "path": path,
            "slug": slug or "unknown",
            "display": display or slug or "unknown",
        }
    path = str(profile).strip()
    if not path:
        return None
    slug = Path(path).name or path
    return {"path": path, "slug": slug, "display": slug}


def unresolved_profile() -> dict[str, str]:
    return {"path": "", "slug": "unresolved", "display": "unresolved"}


def set_browser_nav_profile(cdp_endpoint: str | None, profile: str | Path | dict[str, Any] | None) -> None:
    if not cdp_endpoint:
        return
    record = _profile_record(profile)
    if record is not None:
        _BROWSER_PROFILE_CACHE[str(cdp_endpoint).rstrip("/")] = record


def configure_browser_nav_logging(
    logger: Callable[[dict[str, Any]], None] | None,
    *,
    instance: str | None = None,
    component: str | None = None,
    role: str | None = None,
    cdp_endpoint: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
) -> None:
    global _BROWSER_NAV_LOGGER, _INSTANCE_ID, _DEFAULT_LOG_COMPONENT, _DEFAULT_LOG_ROLE
    _BROWSER_NAV_LOGGER = logger
    if instance:
        _INSTANCE_ID = str(instance)
    if component:
        _DEFAULT_LOG_COMPONENT = str(component)
    if role:
        _DEFAULT_LOG_ROLE = str(role)
    set_browser_nav_profile(cdp_endpoint, chrome_profile)


def redact_url(raw_url: str) -> str:
    text = str(raw_url or "")
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    query_pairs: list[tuple[str, str]] = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        risky = lower in _SENSITIVE_QUERY_KEYS or (
            lower != "authuser"
            and any(marker in lower for marker in ("token", "secret", "password", "passcode", "assertion", "ticket"))
        )
        if risky:
            query_pairs.append((key, "***redacted***"))
            changed = True
        else:
            safe_value = redact_url(value) if value.lower().startswith(("http://", "https://")) else value
            query_pairs.append((key, safe_value))
            changed = changed or safe_value != value
    fragment = parts.fragment
    if fragment and any(marker in fragment.lower() for marker in ("token=", "code=", "secret=", "password=", "access_token=")):
        fragment = "***redacted***"
        changed = True
    netloc = parts.netloc
    if parts.username is not None or parts.password is not None:
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parts.port}" if parts.port else hostname
        changed = True
    if not changed:
        return text
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query_pairs, doseq=True), fragment))


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie)\s*[:=]\s*(?:bearer\s+)?[^\s;,]+",
        lambda match: f"{match.group(1)}=***redacted***",
        text,
    )
    text = re.sub(
        r"(?i)\b(access_token|id_token|refresh_token|token|client_secret|password|passcode|code|session|state)"
        r"\s*[:=]\s*([^\s&,;]+)",
        lambda match: f"{match.group(1)}=***redacted***",
        text,
    )
    return re.sub(
        r"https?://[^\s<>'\"]+",
        lambda match: redact_url(match.group(0)),
        text,
    )


# Backwards-compatible private name for callers/tests from the partial implementation.
_redact_url = redact_url


def classify_url(raw_url: str) -> UrlKind:
    try:
        parts = urlsplit(str(raw_url or ""))
    except Exception:
        return UrlKind.NEUTRAL
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.lower()
    if host in {"accounts.google.com", "myaccount.google.com"}:
        return UrlKind.GOOGLE_AUTH
    if host == "discord.com" and (path == "/login" or path.startswith("/login/") or path.startswith("/oauth2/authorize")):
        return UrlKind.DISCORD_AUTH
    if host == "meet.google.com":
        return UrlKind.GOOGLE_PROVIDER
    return UrlKind.NEUTRAL


def _provider_for(kind: UrlKind) -> str | None:
    if kind in {UrlKind.GOOGLE_AUTH, UrlKind.GOOGLE_PROVIDER}:
        return "google"
    if kind == UrlKind.DISCORD_AUTH:
        return "discord"
    return None


def _resolved_profile(
    cdp_endpoint: str | None,
    chrome_profile: str | Path | dict[str, Any] | None,
) -> dict[str, str]:
    endpoint_key = str(cdp_endpoint or "").rstrip("/")
    return (
        _profile_record(chrome_profile)
        or _BROWSER_PROFILE_CACHE.get(endpoint_key)
        or unresolved_profile()
    )


def _consent_scope_key(
    provider: str,
    profile: dict[str, str],
    sso_intent: str,
    operation_id: str,
) -> tuple[str, str, str, str]:
    profile_scope = str(profile.get("path") or profile.get("slug") or "unresolved").lower()
    return provider, profile_scope, sso_intent, operation_id


def _coerce_consent_decision(value: Any) -> ConsentDecision:
    if isinstance(value, ConsentDecision):
        return value
    if value is True:
        return ConsentDecision.ALLOW_ONCE
    if value is False or value is None:
        return ConsentDecision.DENY
    try:
        return ConsentDecision(str(value))
    except ValueError:
        return ConsentDecision.DENY


def _request_operator_consent(request: ConsentRequest) -> tuple[ConsentDecision, str | None]:
    provider = _CONSENT_PROVIDER or TkConsentProvider()
    try:
        if callable(provider) and not hasattr(provider, "request_consent"):
            result = provider(request)
        else:
            result = provider.request_consent(request)  # type: ignore[union-attr]
        return _coerce_consent_decision(result), None
    except Exception as error:
        return ConsentDecision.UNAVAILABLE, redact_sensitive_text(error)


def _identity_metadata(
    url: str,
    *,
    sso_intent: SsoIntent | str | None,
    identity_mode: IdentityMode | str | None,
    intended_identity: str | None,
    effective_identity: str | None,
    origin: str,
) -> dict[str, Any]:
    kind = classify_url(url)
    try:
        mode = IdentityMode(identity_mode or (IdentityMode.SELECTED if intended_identity else IdentityMode.AMBIENT))
    except ValueError:
        mode = IdentityMode.AMBIENT
    try:
        intent = SsoIntent(sso_intent).value if sso_intent is not None else None
    except ValueError:
        intent = None
    intended = str(intended_identity or "").strip().lower() or None
    effective = str(effective_identity or "").strip().lower() or None
    mismatch = bool(intended and effective and intended != effective)
    provider = _provider_for(kind)
    return {
        "url_kind": kind.value,
        "identity_provider": provider,
        "identity_mode": mode.value,
        "intended_identity": intended,
        "effective_identity": effective,
        "ambient_identity": bool(provider and not intended and mode != IdentityMode.ANONYMOUS),
        "identity_mismatch": mismatch,
        "sso_intent": intent,
        "origin": "operator" if str(origin).lower() == "operator" else "machine",
    }


def _blocked_reason(metadata: dict[str, Any]) -> str | None:
    is_auth = metadata["url_kind"] in {UrlKind.GOOGLE_AUTH.value, UrlKind.DISCORD_AUTH.value}
    if not is_auth:
        return None
    if metadata["identity_mode"] == IdentityMode.ANONYMOUS.value:
        return "anonymous navigation must not reach an authentication page"
    if not metadata["sso_intent"]:
        return "authentication target requires explicit SSO intent"
    return None


_INTENT_FIELDS = {
    "nav_id", "ts", "ts_epoch", "pid", "instance", "component", "role", "url",
    "chrome_profile", "cdp_endpoint", "resolved_endpoint", "reason", "detail",
    "caller", "outcome", "phase", "backend", "tab_id", "target_id", "action",
    "error", "url_kind", "identity_provider", "identity_mode",
    "intended_identity", "effective_identity", "ambient_identity",
    "identity_mismatch", "sso_intent", "origin",
    "consent_operation_id", "consent_scope",
}


def sanitize_intent_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Whitelist and redact an untrusted cross-process intent payload."""
    source = payload if isinstance(payload, dict) else {}
    clean = {key: source[key] for key in _INTENT_FIELDS if key in source}
    clean["url"] = redact_url(str(clean.get("url") or ""))[:4096]
    for key in ("detail", "caller", "error"):
        if key in clean:
            clean[key] = redact_sensitive_text(clean[key])[:4096]
    profile = _profile_record(clean.get("chrome_profile"))
    clean["chrome_profile"] = profile or unresolved_profile()
    for key in ("intended_identity", "effective_identity"):
        if clean.get(key):
            clean[key] = str(clean[key]).strip().lower()
    clean["ambient_identity"] = bool(clean.get("ambient_identity"))
    clean["identity_mismatch"] = bool(clean.get("identity_mismatch"))
    try:
        clean["pid"] = int(clean.get("pid") or 0)
    except (TypeError, ValueError):
        clean["pid"] = 0
    try:
        clean["ts_epoch"] = float(clean.get("ts_epoch") or time.time())
    except (TypeError, ValueError):
        clean["ts_epoch"] = time.time()
    for key in _INTENT_FIELDS - {"chrome_profile", "ambient_identity", "identity_mismatch", "pid", "ts_epoch"}:
        if key in clean and clean[key] is not None and not isinstance(clean[key], str):
            clean[key] = str(clean[key])
    return clean


def _browser_nav_caller() -> str:
    skip_names = {
        "_browser_nav_caller",
        "log_browser_nav_intent",
        "_log_browser_nav_outcome",
        "open_url",
        "navigate",
        "reuse_or_open",
        "evaluate_navigation",
        "launch",
        "_authorize_auth_navigation",
        "_request_operator_consent",
    }
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            name = frame.f_code.co_name
            module = frame.f_globals.get("__name__", "")
            if name not in skip_names:
                return f"{module}:{name}:{frame.f_lineno}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def log_browser_nav_intent(
    cdp_endpoint: str | None,
    url: str,
    *,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    outcome: str = "intent-recorded",
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    nav_id: str | None = None,
    error: str | None = None,
    caller: str | None = None,
    phase: str = "intent",
    backend: str | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    consent_operation_id: str | None = None,
    consent_scope: str | None = None,
) -> str:
    nav_id = nav_id or uuid.uuid4().hex
    endpoint_key = str(cdp_endpoint or "").rstrip("/")
    explicit_profile = _profile_record(chrome_profile)
    if explicit_profile is not None and endpoint_key and explicit_profile.get("display") != "unresolved":
        _BROWSER_PROFILE_CACHE[endpoint_key] = explicit_profile
    profile = explicit_profile or _BROWSER_PROFILE_CACHE.get(endpoint_key) or unresolved_profile()
    payload: dict[str, Any] = {
        "nav_id": nav_id,
        "ts": _utc_now_iso(),
        "ts_epoch": time.time(),
        "pid": os.getpid(),
        "instance": _INSTANCE_ID,
        "component": component or _DEFAULT_LOG_COMPONENT,
        "role": role,
        "url": redact_url(url),
        "chrome_profile": profile,
        "cdp_endpoint": endpoint_key,
        "resolved_endpoint": endpoint_key,
        "reason": str(reason),
        "detail": redact_sensitive_text(detail),
        "caller": redact_sensitive_text(caller or _browser_nav_caller()),
        "outcome": str(outcome or "intent-recorded"),
        "phase": phase,
        "backend": backend or (_BACKEND_OVERRIDE.name if _BACKEND_OVERRIDE is not None else "cdp"),
    }
    payload.update(
        _identity_metadata(
            url,
            sso_intent=sso_intent,
            identity_mode=identity_mode,
            intended_identity=intended_identity,
            effective_identity=effective_identity,
            origin=origin,
        )
    )
    if tab_id:
        payload["tab_id"] = str(tab_id)
    if target_id:
        payload["target_id"] = str(target_id)
    if action:
        payload["action"] = str(action)
    if consent_operation_id:
        payload["consent_operation_id"] = str(consent_operation_id)
    if consent_scope:
        payload["consent_scope"] = str(consent_scope)
    if error:
        payload["error"] = redact_sensitive_text(error)
    logger = _BROWSER_NAV_LOGGER
    if logger is None or getattr(_LOGGING_LOCAL, "active", False):
        return nav_id
    try:
        _LOGGING_LOCAL.active = True
        logger(payload)
    except Exception:
        pass
    finally:
        _LOGGING_LOCAL.active = False
    return nav_id


def _log_browser_nav_outcome(
    nav_id: str,
    cdp_endpoint: str | None,
    url: str,
    *,
    reason: str,
    detail: str,
    role: str,
    component: str | None,
    outcome: str,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    info: dict[str, Any] | None = None,
    target_id: str | None = None,
    error: str | None = None,
    action: str | None = None,
    backend: str | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    consent_operation_id: str | None = None,
    consent_scope: str | None = None,
) -> None:
    tab_id = str((info or {}).get("id") or "") or None
    target_id = target_id or str((info or {}).get("targetId") or (info or {}).get("id") or "") or None
    log_browser_nav_intent(
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        outcome=outcome,
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        target_id=target_id,
        action=action,
        nav_id=nav_id,
        error=error,
        phase="outcome",
        backend=backend,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        consent_operation_id=consent_operation_id,
        consent_scope=consent_scope,
    )


def _authorize_auth_navigation(
    nav_id: str,
    cdp_endpoint: str | None,
    url: str,
    *,
    reason: str,
    detail: str,
    role: str,
    component: str | None,
    chrome_profile: str | Path | dict[str, Any] | None,
    action: str,
    backend: str,
    sso_intent: SsoIntent | str | None,
    identity_mode: IdentityMode | str | None,
    intended_identity: str | None,
    effective_identity: str | None,
    origin: str,
    consent_operation_id: str | None,
    allow_operation_scope: bool,
) -> None:
    metadata = _identity_metadata(
        url,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    blocked = _blocked_reason(metadata)
    if blocked:
        _log_browser_nav_outcome(
            nav_id, cdp_endpoint, url, reason=reason, detail=detail, role=role,
            component=component, outcome="blocked", chrome_profile=chrome_profile,
            error=blocked, action=action, backend=backend, sso_intent=sso_intent,
            identity_mode=identity_mode, intended_identity=intended_identity,
            effective_identity=effective_identity, origin=origin,
            consent_operation_id=consent_operation_id,
            consent_scope="exact-operation" if allow_operation_scope else "once",
        )
        raise NavigationBlockedError(blocked)
    kind = UrlKind(metadata["url_kind"])
    if kind not in {UrlKind.GOOGLE_AUTH, UrlKind.DISCORD_AUTH}:
        return

    provider = str(metadata["identity_provider"])
    typed_intent = str(metadata["sso_intent"])
    profile = _resolved_profile(cdp_endpoint, chrome_profile)
    consent_fields = dict(
        cdp_endpoint=cdp_endpoint,
        url=url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        action=action,
        nav_id=nav_id,
        backend=backend,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        consent_operation_id=consent_operation_id,
        consent_scope="exact-operation" if allow_operation_scope else "once",
    )
    if not consent_confirmation_required():
        log_browser_nav_intent(
            **{**consent_fields, "consent_scope": "not-required"},
            outcome="consent-disabled",
            phase="consent",
        )
        return

    scope_key: tuple[str, str, str, str] | None = None
    if consent_operation_id and allow_operation_scope:
        scope_key = _consent_scope_key(provider, profile, typed_intent, consent_operation_id)
        now = time.monotonic()
        with _CONSENT_LOCK:
            expired = [key for key, expiry in _CONSENT_APPROVALS.items() if expiry <= now]
            for key in expired:
                _CONSENT_APPROVALS.pop(key, None)
            if _CONSENT_APPROVALS.get(scope_key, 0.0) > now:
                return

    log_browser_nav_intent(
        **consent_fields,
        outcome="awaiting-consent",
        phase="consent",
    )
    caller = _browser_nav_caller()
    request = ConsentRequest(
        provider=provider,
        url=redact_url(url),
        reason=redact_sensitive_text(reason),
        detail=redact_sensitive_text(detail),
        component=component or _DEFAULT_LOG_COMPONENT,
        role=role,
        chrome_profile=profile,
        intended_identity=metadata["intended_identity"],
        sso_intent=typed_intent,
        origin=metadata["origin"],
        caller=caller,
        operation_id=consent_operation_id,
        allow_operation_scope=bool(consent_operation_id and allow_operation_scope),
    )
    decision, consent_error = _request_operator_consent(request)
    if decision in {ConsentDecision.ALLOW_ONCE, ConsentDecision.ALLOW_OPERATION}:
        if decision == ConsentDecision.ALLOW_OPERATION and scope_key is not None:
            with _CONSENT_LOCK:
                _CONSENT_APPROVALS[scope_key] = time.monotonic() + _CONSENT_SCOPE_TTL_SECONDS
        log_browser_nav_intent(
            **consent_fields,
            outcome="approved",
            phase="consent",
        )
        return

    unavailable = decision == ConsentDecision.UNAVAILABLE
    outcome = "awaiting-consent" if unavailable else "denied"
    error = consent_error or (
        "native consent dialog unavailable; navigation was not attempted"
        if unavailable
        else "operator denied or closed authentication consent"
    )
    log_browser_nav_intent(
        **consent_fields,
        outcome=outcome,
        phase="consent",
        error=error,
    )
    _log_browser_nav_outcome(
        nav_id, cdp_endpoint, url, reason=reason, detail=detail, role=role,
        component=component, outcome=outcome if unavailable else "blocked",
        chrome_profile=chrome_profile, error=error, action=action, backend=backend,
        sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        consent_scope="exact-operation" if allow_operation_scope else "once",
    )
    raise NavigationBlockedError(error)


def _check_final_landing(
    nav_id: str,
    page: Any,
    requested_url: str,
    *,
    selected: BrowserBackend,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None,
    chrome_profile: str | Path | dict[str, Any] | None,
    info: dict[str, Any] | None,
    action: str,
    sso_intent: SsoIntent | str | None,
    identity_mode: IdentityMode | str | None,
    intended_identity: str | None,
    effective_identity: str | None,
    origin: str,
) -> str | None:
    try:
        landed_url = selected.wait_for_final_url(page, requested_url, timeout=5.0)
    except Exception as error:
        _log_browser_nav_outcome(
            nav_id, cdp_endpoint, requested_url, reason=reason, detail=detail, role=role,
            component=component, outcome="failed", chrome_profile=chrome_profile, info=info,
            error=f"navigation completion check failed: {error}", action=action,
            backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
            intended_identity=intended_identity, effective_identity=effective_identity,
            origin=origin,
        )
        raise RuntimeError(f"navigation completion check failed: {error}") from error
    requested_kind = classify_url(requested_url)
    landed_kind = classify_url(str(landed_url or ""))
    if (
        requested_kind in {UrlKind.GOOGLE_AUTH, UrlKind.DISCORD_AUTH}
        or landed_kind not in {UrlKind.GOOGLE_AUTH, UrlKind.DISCORD_AUTH}
    ):
        return str(landed_url) if landed_url else None
    try:
        selected.stop(page)
    except Exception:
        pass
    _log_browser_nav_outcome(
        nav_id,
        cdp_endpoint,
        str(landed_url),
        reason=reason,
        detail=f"{detail}; unexpected authentication redirect stopped; operator attention required",
        role=role,
        component=component,
        outcome="unexpected-auth-landing",
        chrome_profile=chrome_profile,
        info=info,
        error="unexpected authentication landing after non-auth navigation",
        action=action,
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    raise UnexpectedAuthLandingError(
        "unexpected authentication landing after non-auth navigation"
    )


def _prepare_navigation(selected: BrowserBackend, page: Any) -> None:
    selected.prepare_navigation(page)


def _open_url_raw(
    cdp_endpoint: str,
    target: str,
    *,
    backend: BrowserBackend,
) -> tuple[dict[str, Any] | None, str | None, str]:
    try:
        info, error, action = backend.open_tab(cdp_endpoint, target)
        return (info.to_legacy() if info is not None else None), error, action
    except Exception as error:
        return None, str(error), "open-tab"


def open_url(
    cdp_endpoint: str,
    target: str,
    *,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    log_nav_intent: bool = True,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> dict[str, Any] | None:
    selected = backend or get_browser_backend()
    if not log_nav_intent:
        _authorize_auth_navigation(
            uuid.uuid4().hex, cdp_endpoint, target, reason=reason, detail=detail, role=role,
            component=component, chrome_profile=chrome_profile, action="open-tab",
            backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
            intended_identity=intended_identity, effective_identity=effective_identity,
            origin=origin, consent_operation_id=consent_operation_id,
            allow_operation_scope=allow_operation_scope,
        )
        info, error, action = _open_url_raw(cdp_endpoint, target, backend=selected)
        setattr(open_url, "_last_error", error)
        setattr(open_url, "_last_action", action)
        setattr(open_url, "_last_error", error)
        setattr(open_url, "_last_action", action)
        return info
    nav_id = log_browser_nav_intent(
        cdp_endpoint,
        target,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        action="open-tab",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    _authorize_auth_navigation(
        nav_id, cdp_endpoint, target, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=chrome_profile, action="open-tab",
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
    )
    info, error, action = _open_url_raw(cdp_endpoint, target, backend=selected)
    if info and info.get("webSocketDebuggerUrl"):
        page = selected.attach(BrowserTarget.from_legacy(info))
        try:
            final_url = _check_final_landing(
                nav_id, page, target, selected=selected, cdp_endpoint=cdp_endpoint,
                reason=reason, detail=detail, role=role, component=component,
                chrome_profile=chrome_profile, info=info, action=action,
                sso_intent=sso_intent, identity_mode=identity_mode,
                intended_identity=intended_identity, effective_identity=effective_identity,
                origin=origin,
            )
            if final_url:
                info["url"] = final_url
        finally:
            selected.close_connection(page)
    _log_browser_nav_outcome(
        nav_id, cdp_endpoint, target, reason=reason, detail=detail, role=role,
        component=component, outcome="opened" if info is not None else "failed",
        chrome_profile=chrome_profile, info=info, error=error, action=action,
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        consent_scope="exact-operation" if allow_operation_scope else "once",
    )
    return info


def navigate(
    tab: Any,
    url: str,
    *,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_info: dict[str, Any] | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> Any:
    selected = backend or get_browser_backend()
    nav_id = log_browser_nav_intent(
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        tab_id=str((tab_info or {}).get("id") or "") or None,
        target_id=str((tab_info or {}).get("id") or "") or None,
        action="navigate",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    _authorize_auth_navigation(
        nav_id, cdp_endpoint, url, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=chrome_profile, action="navigate",
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
    )
    try:
        _prepare_navigation(selected, tab)
        result = selected.navigate(tab, url)
        final_url = _check_final_landing(
            nav_id, tab, url, selected=selected, cdp_endpoint=cdp_endpoint,
            reason=reason, detail=detail, role=role, component=component,
            chrome_profile=chrome_profile, info=tab_info, action="navigate",
            sso_intent=sso_intent, identity_mode=identity_mode,
            intended_identity=intended_identity, effective_identity=effective_identity,
            origin=origin,
        )
        if tab_info is not None and final_url:
            tab_info["url"] = final_url
    except UnexpectedAuthLandingError:
        raise
    except Exception as error:
        _log_browser_nav_outcome(
            nav_id,
            cdp_endpoint,
            url,
            reason=reason,
            detail=detail,
            role=role,
            component=component,
            outcome="failed",
            chrome_profile=chrome_profile,
            info=tab_info,
            error=str(error),
            action="navigate",
            backend=selected.name,
            sso_intent=sso_intent,
            identity_mode=identity_mode,
            intended_identity=intended_identity,
            effective_identity=effective_identity,
            origin=origin,
            consent_operation_id=consent_operation_id,
            consent_scope="exact-operation" if allow_operation_scope else "once",
        )
        raise
    _log_browser_nav_outcome(
        nav_id,
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        outcome="navigated",
        chrome_profile=chrome_profile,
        info=tab_info,
        action="navigate",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    return result


def reuse_or_open(
    cdp_endpoint: str,
    target: str,
    *,
    existing_in_scope: dict[str, Any] | None,
    navigate_existing: bool,
    reason: str,
    detail: str,
    role: str,
    tab_factory: Callable[[str], Any] | None = None,
    list_tabs_func: Callable[[str], list[dict[str, Any]]] | None = None,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    selected = backend or get_browser_backend()
    if backend is None and isinstance(selected, CdpBrowserBackend) and (tab_factory is not None or list_tabs_func is not None):
        selected = CdpBrowserBackend(
            tab_factory=tab_factory,
            list_tabs=list_tabs_func,
        )
    nav_id = log_browser_nav_intent(
        cdp_endpoint,
        target,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        action="navigate" if navigate_existing else "reuse-or-open-tab",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    _authorize_auth_navigation(
        nav_id, cdp_endpoint, target, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=chrome_profile,
        action="navigate" if navigate_existing else "reuse-or-open-tab",
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
    )
    controlled = existing_in_scope
    if existing_in_scope and not existing_in_scope.get("webSocketDebuggerUrl") and existing_in_scope.get("id"):
        listed = [item.to_legacy() for item in selected.list_tabs(cdp_endpoint)]
        controlled = next(
            (
                tab
                for tab in listed
                if str(tab.get("id") or "") == str(existing_in_scope.get("id") or "")
            ),
            existing_in_scope,
        )
    if controlled and controlled.get("webSocketDebuggerUrl"):
        tab = selected.attach(BrowserTarget.from_legacy(controlled))
        try:
            if navigate_existing:
                _prepare_navigation(selected, tab)
                selected.navigate(tab, target)
            final_url = _check_final_landing(
                nav_id, tab, target, selected=selected, cdp_endpoint=cdp_endpoint,
                reason=reason, detail=detail, role=role, component=component,
                chrome_profile=chrome_profile, info=controlled,
                action="navigate" if navigate_existing else "reuse-existing-tab",
                sso_intent=sso_intent, identity_mode=identity_mode,
                intended_identity=intended_identity, effective_identity=effective_identity,
                origin=origin,
            )
            if final_url:
                controlled["url"] = final_url
            selected.foreground(tab)
            _log_browser_nav_outcome(
                nav_id,
                cdp_endpoint,
                target,
                reason=reason,
                detail=detail,
                role=role,
                component=component,
                outcome="navigated" if navigate_existing else "reused-existing-tab",
                chrome_profile=chrome_profile,
                info=controlled,
                action="navigate" if navigate_existing else "reuse-existing-tab",
                backend=selected.name,
                sso_intent=sso_intent,
                identity_mode=identity_mode,
                intended_identity=intended_identity,
                effective_identity=effective_identity,
                origin=origin,
                consent_operation_id=consent_operation_id,
                consent_scope="exact-operation" if allow_operation_scope else "once",
            )
        except UnexpectedAuthLandingError:
            raise
        except Exception as error:
            _log_browser_nav_outcome(
                nav_id,
                cdp_endpoint,
                target,
                reason=reason,
                detail=detail,
                role=role,
                component=component,
                outcome="failed",
                chrome_profile=chrome_profile,
                info=controlled,
                error=str(error),
                action="navigate" if navigate_existing else "reuse-existing-tab",
                backend=selected.name,
                sso_intent=sso_intent,
                identity_mode=identity_mode,
                intended_identity=intended_identity,
                effective_identity=effective_identity,
                origin=origin,
                consent_operation_id=consent_operation_id,
                consent_scope="exact-operation" if allow_operation_scope else "once",
            )
            raise
        finally:
            selected.close_connection(tab)
        result = dict(controlled)
        if navigate_existing:
            result["url"] = target
        return result, True

    info, open_error, open_action = _open_url_raw(cdp_endpoint, target, backend=selected)
    if info and info.get("webSocketDebuggerUrl"):
        tab = selected.attach(BrowserTarget.from_legacy(info))
        try:
            final_url = _check_final_landing(
                nav_id, tab, target, selected=selected, cdp_endpoint=cdp_endpoint,
                reason=reason, detail=detail, role=role, component=component,
                chrome_profile=chrome_profile, info=info, action=open_action,
                sso_intent=sso_intent, identity_mode=identity_mode,
                intended_identity=intended_identity, effective_identity=effective_identity,
                origin=origin,
            )
            if final_url:
                info["url"] = final_url
            selected.foreground(tab)
        finally:
            selected.close_connection(tab)
    _log_browser_nav_outcome(
        nav_id, cdp_endpoint, target, reason=reason, detail=detail, role=role,
        component=component, outcome="opened" if info is not None else "failed",
        chrome_profile=chrome_profile, info=info, error=open_error, action=open_action,
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin,
    )
    return info, False


def evaluate_navigation(
    tab: Any,
    js: str,
    *,
    url: str,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    action: str = "js-navigation",
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> Any:
    selected = backend or get_browser_backend()
    nav_id = log_browser_nav_intent(
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        target_id=tab_id,
        action=action,
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    _authorize_auth_navigation(
        nav_id, cdp_endpoint, url, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=chrome_profile, action=action,
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=effective_identity,
        origin=origin, consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
    )
    try:
        _prepare_navigation(selected, tab)
        result = selected.evaluate_navigation(tab, js)
        _check_final_landing(
            nav_id, tab, url, selected=selected, cdp_endpoint=cdp_endpoint,
            reason=reason, detail=detail, role=role, component=component,
            chrome_profile=chrome_profile, info={"id": tab_id} if tab_id else None,
            action=action, sso_intent=sso_intent, identity_mode=identity_mode,
            intended_identity=intended_identity, effective_identity=effective_identity,
            origin=origin,
        )
    except UnexpectedAuthLandingError:
        raise
    except Exception as error:
        log_browser_nav_intent(
            cdp_endpoint,
            url,
            reason=reason,
            detail=detail,
            role=role,
            component=component,
            outcome="failed",
            chrome_profile=chrome_profile,
            tab_id=tab_id,
            target_id=tab_id,
            action=action,
            nav_id=nav_id,
            error=str(error),
            phase="outcome",
            backend=selected.name,
            sso_intent=sso_intent,
            identity_mode=identity_mode,
            intended_identity=intended_identity,
            effective_identity=effective_identity,
            origin=origin,
        )
        raise
    log_browser_nav_intent(
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        outcome="navigated",
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        target_id=tab_id,
        action=action,
        nav_id=nav_id,
        phase="outcome",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
    )
    return result


def evaluate_location_href(
    tab: Any,
    target_url: str,
    *,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
) -> Any:
    return evaluate_navigation(
        tab,
        "location.href = %s" % json.dumps(target_url),
        url=target_url,
        cdp_endpoint=cdp_endpoint,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        action="js-location-href",
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        backend=backend,
    )


def evaluate_location_replace(
    tab: Any,
    target_url: str,
    *,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
) -> Any:
    return evaluate_navigation(
        tab,
        "location.replace(%s)" % json.dumps(target_url),
        url=target_url,
        cdp_endpoint=cdp_endpoint,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        action="js-location-replace",
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        backend=backend,
    )


def evaluate_location_reload(
    tab: Any,
    target_url: str,
    *,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
) -> Any:
    return evaluate_navigation(
        tab,
        "location.reload()",
        url=target_url,
        cdp_endpoint=cdp_endpoint,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        tab_id=tab_id,
        action="js-location-reload",
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        backend=backend,
    )


def foreground(
    tab: Any,
    target_url: str,
    *,
    cdp_endpoint: str,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    tab_id: str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
) -> Any:
    selected = backend or get_browser_backend()
    nav_id = log_browser_nav_intent(
        cdp_endpoint, target_url, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=chrome_profile, tab_id=tab_id,
        target_id=tab_id, action="foreground", backend=selected.name,
        intended_identity=intended_identity, effective_identity=effective_identity, origin=origin,
    )
    try:
        result = selected.foreground(tab)
    except Exception as error:
        _log_browser_nav_outcome(
            nav_id, cdp_endpoint, target_url, reason=reason, detail=detail, role=role,
            component=component, outcome="failed", chrome_profile=chrome_profile,
            target_id=tab_id, error=str(error), action="foreground", backend=selected.name,
            intended_identity=intended_identity, effective_identity=effective_identity, origin=origin,
        )
        raise
    _log_browser_nav_outcome(
        nav_id, cdp_endpoint, target_url, reason=reason, detail=detail, role=role,
        component=component, outcome="foregrounded", chrome_profile=chrome_profile,
        target_id=tab_id, action="foreground", backend=selected.name,
        intended_identity=intended_identity, effective_identity=effective_identity, origin=origin,
    )
    return result


def launch(
    argv: list[str] | Callable[[], list[str]],
    *,
    cdp_endpoint: str,
    url: str,
    profile: str | Path,
    reason: str,
    detail: str,
    role: str,
    component: str | None = None,
    wait_until_ready: Callable[[], bool] | None = None,
    ready_timeout: float = 0.0,
    sso_intent: SsoIntent | str | None = None,
    identity_mode: IdentityMode | str | None = None,
    intended_identity: str | None = None,
    origin: str = "machine",
    backend: BrowserBackend | None = None,
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> subprocess.Popen[bytes]:
    selected = backend or get_browser_backend()
    set_browser_nav_profile(cdp_endpoint, profile)
    try:
        tabs_before_launch = {target.id for target in selected.list_tabs(cdp_endpoint)}
    except Exception:
        tabs_before_launch = set()
    nav_id = log_browser_nav_intent(
        cdp_endpoint,
        url,
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=profile,
        action="launch-browser",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        origin=origin,
    )
    _authorize_auth_navigation(
        nav_id, cdp_endpoint, url, reason=reason, detail=detail, role=role,
        component=component, chrome_profile=profile, action="launch-browser",
        backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
        intended_identity=intended_identity, effective_identity=None, origin=origin,
        consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
    )
    try:
        launch_argv = argv() if callable(argv) else argv
        process = selected.launch(launch_argv)
    except (Exception, SystemExit) as error:
        _log_browser_nav_outcome(
            nav_id,
            cdp_endpoint,
            url,
            reason=reason,
            detail=detail,
            role=role,
            component=component,
            outcome="failed",
            chrome_profile=profile,
            error=str(error),
            action="launch-browser",
            backend=selected.name,
            sso_intent=sso_intent,
            identity_mode=identity_mode,
            intended_identity=intended_identity,
            origin=origin,
        )
        raise
    if wait_until_ready is not None:
        deadline = time.time() + max(0.0, ready_timeout)
        while time.time() < deadline and not wait_until_ready():
            time.sleep(0.5)
        if not wait_until_ready():
            _log_browser_nav_outcome(
                nav_id,
                cdp_endpoint,
                url,
                reason=reason,
                detail=f"{detail}; spawned pid {process.pid} but DevTools port did not answer",
                role=role,
                component=component,
                outcome="failed",
                chrome_profile=profile,
                target_id=str(process.pid),
                error="DevTools port did not answer",
                action="launch-browser",
                backend=selected.name,
                sso_intent=sso_intent,
                identity_mode=identity_mode,
                intended_identity=intended_identity,
                origin=origin,
            )
            raise RuntimeError("DevTools port did not answer")
        target: BrowserTarget | None = None
        target_deadline = time.monotonic() + 5.0
        while time.monotonic() < target_deadline:
            try:
                tabs = selected.list_tabs(cdp_endpoint)
            except Exception:
                tabs = []
            new_tabs = [candidate for candidate in tabs if candidate.id not in tabs_before_launch]
            target = next((candidate for candidate in new_tabs if candidate.url == url), None)
            if target is None and len(new_tabs) == 1:
                target = new_tabs[0]
            if target is None:
                target = next((candidate for candidate in tabs if candidate.url == url), None)
            if target is not None and target.websocket_url:
                break
            time.sleep(0.1)
        if target is None or not target.websocket_url:
            error = "launched browser target was not exposed by DevTools"
            _log_browser_nav_outcome(
                nav_id, cdp_endpoint, url, reason=reason,
                detail=f"{detail}; spawned pid {process.pid} but launch target was unavailable",
                role=role, component=component, outcome="failed", chrome_profile=profile,
                target_id=str(process.pid), error=error, action="launch-browser",
                backend=selected.name, sso_intent=sso_intent, identity_mode=identity_mode,
                intended_identity=intended_identity, origin=origin,
            )
            raise RuntimeError(error)
        page = selected.attach(target)
        try:
            final_url = _check_final_landing(
                nav_id, page, url, selected=selected, cdp_endpoint=cdp_endpoint,
                reason=reason, detail=detail, role=role, component=component,
                chrome_profile=profile, info=target.to_legacy(), action="launch-browser",
                sso_intent=sso_intent, identity_mode=identity_mode,
                intended_identity=intended_identity, effective_identity=None, origin=origin,
            )
            if final_url:
                target = BrowserTarget(
                    target.id, final_url, target.websocket_url, target.title, target.type
                )
        finally:
            selected.close_connection(page)
    _log_browser_nav_outcome(
        nav_id,
        cdp_endpoint,
        url,
        reason=reason,
        detail=f"{detail}; spawned pid {process.pid}",
        role=role,
        component=component,
        outcome="opened",
        chrome_profile=profile,
        target_id=target.id if wait_until_ready is not None and target is not None else str(process.pid),
        action="launch-browser",
        backend=selected.name,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        origin=origin,
    )
    return process
