"""Minimal Chrome DevTools Protocol (CDP) client -- no Playwright/Selenium.

Talks to a real Chrome/Edge over its ``--remote-debugging-port`` HTTP+WS API:
list/find tabs, evaluate JavaScript in a tab, and (re)launch a dedicated
browser window with a persistent profile so a Google SSO login survives
between runs. HTTP calls use stdlib ``urllib`` (no new dependency); the raw
per-tab WebSocket connection needs a real WebSocket client, which stdlib does
not provide -- that is the one new dependency this subpackage requires
(``websocket-client``, the ``meet`` extra).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from websocket import create_connection  # websocket-client
from websocket import WebSocketTimeoutException

from . import navigator
from .navigator import configure_browser_nav_logging, set_browser_nav_profile

DEFAULT_CDP = "http://127.0.0.1:9222"
DEFAULT_POPUP_PORT = 9223
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_OLD_DEFAULT_PROFILE = Path.home() / ".cache" / "ws_collab_models" / "meet_bridge_profile"
DEFAULT_PROFILE = Path(
    os.environ.get("WS_COLLAB_MEET_PROFILE_DIR")
    or (_PLUGIN_ROOT / "collab_state" / "meet_bridge_profile")
)
DEFAULT_SSO_AUTHUSER_PROBE_SLOTS = (0, 1)
WSL_BROWSER_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium")


def windows_browser_candidates(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return native browser locations owned by the Windows CDP launcher."""
    values = os.environ if environ is None else environ
    program_files = values.get("PROGRAMFILES") or r"C:\Program Files"
    program_files_x86 = values.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    local_app_data = values.get("LOCALAPPDATA")
    local_root = Path(local_app_data) if local_app_data else (home or Path.home()) / "AppData" / "Local"
    return (
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        local_root / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    )


def _decode_wsl_text(raw: bytes) -> str:
    cleaned = raw.replace(b"\x00", b"")
    try:
        return cleaned.decode("utf-8")
    except UnicodeDecodeError:
        return cleaned.decode("utf-8", errors="ignore")


def _first_wsl_distro(explicit: str | None) -> str:
    if explicit:
        return explicit
    wsl = shutil.which("wsl.exe")
    if not wsl:
        raise SystemExit("--browser-backend wsl requires wsl.exe on PATH")
    found = subprocess.run([wsl, "-l", "-q"], capture_output=True, check=False)
    lines = [line.strip().lstrip("\ufeff") for line in _decode_wsl_text(found.stdout).splitlines()]
    distro = next((line for line in lines if line), "")
    if not distro:
        raise SystemExit("--browser-backend wsl found no installed WSL distros; install one or pass --wsl-distro")
    return distro


def _windows_to_wsl_path(path: Path) -> str:
    text = str(path)
    match = re.match(r"^([A-Za-z]):\\(.*)$", text)
    if not match:
        raise SystemExit(f"WSL browser backend needs a drive-backed Windows profile path, got: {text}")
    drive, rest = match.groups()
    return f"/mnt/{drive.lower()}/{rest.replace('\\', '/')}"


def _find_wsl_browser(distro: str) -> str:
    wsl = shutil.which("wsl.exe")
    if not wsl:
        raise SystemExit("--browser-backend wsl requires wsl.exe on PATH")
    probe = " || ".join([f"command -v {name}" for name in WSL_BROWSER_CANDIDATES]) + " || true"
    found = subprocess.run([wsl, "-d", distro, "--", "bash", "-lc", probe], capture_output=True, check=False)
    lines = [line.strip() for line in _decode_wsl_text(found.stdout).splitlines() if line.strip()]
    if lines:
        return lines[0]
    raise SystemExit(
        "--browser-backend wsl could not find Chrome/Chromium inside WSL "
        f"distro {distro!r}; install one of: {', '.join(WSL_BROWSER_CANDIDATES)}"
    )


def ensure_default_profile_migrated(profile: Path | None = None) -> Path:
    target = Path(profile or DEFAULT_PROFILE).expanduser()
    if os.environ.get("WS_COLLAB_MEET_PROFILE_DIR"):
        return target
    old = _OLD_DEFAULT_PROFILE.expanduser()
    if target.exists() or not old.exists():
        return target
    try:
        shutil.copytree(old, target)
        print(f"[bridge] migrated existing Chrome profile from {old} to {target}", flush=True)
    except Exception:
        pass
    return target


def build_launch(
    backend: str,
    port: int,
    profile: Path,
    url: str,
    *,
    browser: str | None = None,
    wsl_distro: str | None = None,
    extra_args: list[str] | None = None,
    browser_candidates: Iterable[str | Path] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> list[str]:
    extra_args = list(extra_args or [])
    if backend == "windows":
        return [
            find_browser(browser, candidates=browser_candidates, path_lookup=path_lookup),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--use-fake-ui-for-media-stream",
            # This process gets terminated abruptly (Stop-Process/taskkill,
            # not a normal window close) whenever the bridge itself is
            # restarted or an operator hits Disconnect/Kill-process -- that
            # marks the profile's exit_type as "Crashed", and the NEXT
            # launch of that same profile pops a "Restore pages?" bubble
            # that covers part of the Meet UI (annoying, and it's chrome's
            # own native UI, not something CDP Runtime.evaluate can see or
            # dismiss). Suppress it outright rather than treating every
            # planned restart as if it were an unexpected crash.
            "--disable-session-crashed-bubble",
            *extra_args,
            url,
        ]
    if backend != "wsl":
        raise SystemExit(f"Unknown browser backend: {backend}")
    distro = _first_wsl_distro(wsl_distro)
    browser_name = _find_wsl_browser(distro)
    display_num = 90 + (port % 100)
    wsl_profile = _windows_to_wsl_path(profile)
    chrome_args = [
        browser_name,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=0.0.0.0",
        f"--user-data-dir={wsl_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--use-fake-ui-for-media-stream",
        "--no-sandbox",
        "--disable-session-crashed-bubble",
        *extra_args,
        url,
    ]
    chrome_cmd = " ".join(subprocess.list2cmdline([arg]) for arg in chrome_args)
    script = (
        f"mkdir -p {subprocess.list2cmdline([wsl_profile])} && "
        f"(Xvfb :{display_num} -screen 0 1920x1080x24 >/tmp/xvfb-{display_num}.log 2>&1 &) ; "
        f"export DISPLAY=:{display_num}; exec {chrome_cmd}"
    )
    return [shutil.which("wsl.exe") or "wsl.exe", "-d", distro, "--", "bash", "-lc", script]


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local trusted CDP endpoint
        return json.loads(response.read().decode("utf-8"))


def _http_touch(url: str, *, method: str = "GET", timeout: float = 5.0) -> None:
    """Fire a request whose body we don't need (for example, CDP tab close)."""
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 - local trusted CDP endpoint
        pass


def _navigation_backend(
    *,
    open_tab: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> navigator.BrowserBackend:
    selected = navigator.get_browser_backend()
    if not isinstance(selected, navigator.CdpBrowserBackend):
        return selected
    return navigator.CdpBrowserBackend(
        http_json=_http_json,
        tab_factory=CdpTab,
        list_tabs=list_tabs,
        open_tab=open_tab,
        popen=subprocess.Popen,
    )


class CdpTab:
    def __init__(
        self,
        ws_url: str,
        event_handler: Callable[[str, dict[str, Any]], None] | None = None,
        error_handler: Callable[[str], None] | None = None,
    ) -> None:
        # suppress_origin: Chrome rejects DevTools websocket handshakes that
        # carry a browser-style Origin header with HTTP 403.
        self.ws = create_connection(ws_url, timeout=10, suppress_origin=True, enable_multithread=True)
        self._id = 0
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_handler = event_handler
        self._error_handler = error_handler
        self._logged_recv_problems: set[str] = set()
        self._closed = False
        self._recv_error: Exception | None = None
        self._reader = threading.Thread(target=self._recv_loop, daemon=True)
        self._reader.start()

    def _log_recv_problem(self, key: str, message: str) -> None:
        with self._condition:
            if key in self._logged_recv_problems:
                return
            self._logged_recv_problems.add(key)
        handler = self._error_handler
        try:
            if handler is not None:
                handler(message)
            else:
                print(f"[cdp] {message}", file=sys.stderr, flush=True)
        except Exception:
            pass

    @staticmethod
    def _text_frame(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="replace")
        if isinstance(raw, str):
            return raw
        return str(raw)

    def _recv_loop(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
            try:
                raw = self.ws.recv()
            except WebSocketTimeoutException:
                self._log_recv_problem("timeout", "DevTools receive timed out once; continuing")
                continue
            except Exception as error:  # noqa: BLE001 - connection lifecycle is best-effort.
                with self._condition:
                    if self._closed:
                        return
                    self._recv_error = error
                    self._condition.notify_all()
                self._log_recv_problem("socket", f"DevTools receive stopped: {error}")
                return
            text = self._text_frame(raw).strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception as error:
                self._log_recv_problem("json", f"DevTools sent an invalid JSON frame; ignoring ({error})")
                continue
            if not isinstance(payload, dict):
                self._log_recv_problem("shape", "DevTools sent a non-object frame; ignoring")
                continue
            payload_id = payload.get("id")
            if payload_id is not None:
                try:
                    response_id = int(payload_id)
                except (TypeError, ValueError):
                    continue
                with self._condition:
                    self._responses[response_id] = payload
                    self._condition.notify_all()
                continue
            handler = self._event_handler
            with self._condition:
                self._events.append(payload)
                del self._events[:-1000]
                self._condition.notify_all()
            if handler is not None:
                try:
                    handler(str(payload.get("method") or ""), dict(payload.get("params") or {}))
                except Exception:
                    pass

    def set_event_handler(self, handler: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._event_handler = handler

    def set_error_handler(self, handler: Callable[[str], None] | None) -> None:
        self._error_handler = handler

    def drain_events(self) -> list[dict[str, Any]]:
        with self._condition:
            events = list(self._events)
            self._events.clear()
            return events

    def wait_for_navigation_settled(
        self,
        *,
        timeout: float = 5.0,
        settle_seconds: float = 0.5,
        require_event: bool = False,
    ) -> None:
        """Wait for a committed main-frame load and a short event-quiet period."""
        deadline = time.monotonic() + max(0.05, timeout)
        quiet_deadline = time.monotonic() + settle_seconds
        with self._condition:
            while True:
                now = time.monotonic()
                events = list(self._events)
                self._events.clear()
                relevant = any(
                    str(event.get("method") or "") in {
                        "Page.frameNavigated",
                        "Page.loadEventFired",
                        "Page.lifecycleEvent",
                        "Page." "navigatedWithinDocument",
                    }
                    for event in events
                )
                if relevant:
                    quiet_deadline = now + settle_seconds
                if now >= quiet_deadline and (relevant or not require_event):
                    return
                if relevant:
                    require_event = False
                if now >= deadline:
                    return
                wake_at = min(deadline, quiet_deadline if not require_event else deadline)
                self._condition.wait(max(0.0, wake_at - now))

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        with self._send_lock:
            with self._condition:
                if self._closed:
                    raise RuntimeError(f"CDP connection is closed before {method}")
            self._id += 1
            wanted = self._id
            try:
                self.ws.send(json.dumps({"id": wanted, "method": method, "params": params or {}}))
            except Exception as error:  # noqa: BLE001
                self._log_recv_problem("send", f"DevTools send failed: {error}")
                raise
        deadline = time.time() + timeout
        with self._condition:
            while time.time() < deadline:
                payload = self._responses.pop(wanted, None)
                if payload is not None:
                    if "error" in payload:
                        raise RuntimeError(f"CDP {method} failed: {payload.get('error')}")
                    return payload.get("result")
                if self._recv_error is not None:
                    raise RuntimeError(f"CDP connection closed while waiting for {method}: {self._recv_error}")
                if self._closed:
                    raise RuntimeError(f"CDP connection closed while waiting for {method}")
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        raise TimeoutError(f"CDP {method} timed out")

    def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 10.0) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        return ((result or {}).get("result") or {}).get("value")

    def bring_to_front(self) -> None:
        """Activate this tab AND raise its browser window -- the CDP method
        built for exactly this (unlike Runtime.evaluate tricks, which can't
        reliably raise a window the OS considers unfocused)."""
        self.call("Page.bringToFront")

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        try:
            self.ws.close()
        except Exception:
            pass
        if self._reader.is_alive():
            self._reader.join(timeout=1.0)


def list_tabs(cdp: str) -> list[dict[str, Any]]:
    return _http_json(f"{cdp}/json")


def cdp_alive(cdp: str) -> bool:
    try:
        _http_json(f"{cdp}/json/version", timeout=2)
        return True
    except Exception:
        return False


def find_meet_tab(cdp: str) -> dict[str, Any] | None:
    for tab in list_tabs(cdp):
        if tab.get("type") == "page" and "meet.google.com" in str(tab.get("url", "")):
            return tab
    return None


def find_browser(
    explicit: str | None,
    *,
    candidates: Iterable[str | Path] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> str:
    """Find a native Windows Chrome/Edge executable for the CDP launcher."""
    if explicit:
        if Path(explicit).is_file():
            return str(Path(explicit))
        raise SystemExit(
            f"Browser executable not found: {explicit}. "
            "Correct --browser or install Chrome/Edge."
        )
    native_candidates = windows_browser_candidates() if candidates is None else candidates
    for candidate in native_candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path)
    lookup = path_lookup or shutil.which
    for name in ("chrome", "msedge"):
        found = lookup(name)
        if found:
            return found
    raise SystemExit(
        "No native Windows Chrome or Edge executable was found. "
        "Install Chrome/Edge or pass --browser <path-to-chrome.exe>."
    )


def open_url(
    cdp_endpoint: str,
    target: str,
    *,
    reason: str = "compatibility-open-url",
    detail: str = "legacy CDP open_url compatibility shim",
    role: str | None = None,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    _log_nav_intent: bool = True,
    sso_intent: navigator.SsoIntent | str | None = None,
    identity_mode: navigator.IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> dict[str, Any] | None:
    info = navigator.open_url(
        cdp_endpoint,
        target,
        reason=reason,
        detail=detail,
        role=role or "unknown",
        component=component or "cdp",
        chrome_profile=chrome_profile,
        log_nav_intent=_log_nav_intent,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        consent_operation_id=consent_operation_id,
        allow_operation_scope=allow_operation_scope,
        backend=_navigation_backend(),
    )
    setattr(open_url, "_last_error", getattr(navigator.open_url, "_last_error", None))
    setattr(open_url, "_last_action", getattr(navigator.open_url, "_last_action", None))
    return info


def reuse_or_open_tab(
    cdp_endpoint: str,
    target: str,
    *,
    existing_in_scope: dict[str, Any] | None = None,
    navigate_existing: bool = False,
    reason: str = "compatibility-reuse-or-open",
    detail: str = "legacy CDP reuse_or_open_tab compatibility shim",
    role: str | None = None,
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
    sso_intent: navigator.SsoIntent | str | None = None,
    identity_mode: navigator.IdentityMode | str | None = None,
    intended_identity: str | None = None,
    effective_identity: str | None = None,
    origin: str = "machine",
    consent_operation_id: str | None = None,
    allow_operation_scope: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    """Reuse only a caller-selected connector+SSO tab, never globally dedupe."""
    auth_target = navigator.classify_url(target) in {
        navigator.UrlKind.GOOGLE_AUTH,
        navigator.UrlKind.DISCORD_AUTH,
    }
    operation_id = consent_operation_id or (
        f"browser-operation:{uuid.uuid4().hex}" if auth_target else None
    )
    scoped_operation = allow_operation_scope or auth_target

    def open_for_reuse(endpoint: str, url: str) -> dict[str, Any] | None:
        try:
            return open_url(
                endpoint,
                url,
                reason=reason,
                detail=detail,
                role=role,
                component=component,
                chrome_profile=chrome_profile,
                _log_nav_intent=False,
                sso_intent=sso_intent,
                identity_mode=identity_mode,
                intended_identity=intended_identity,
                effective_identity=effective_identity,
                origin=origin,
                consent_operation_id=operation_id,
                allow_operation_scope=scoped_operation,
            )
        except TypeError:
            # Supports old embedders that monkeypatch the compatibility shim.
            return open_url(endpoint, url)

    return navigator.reuse_or_open(
        cdp_endpoint,
        target,
        existing_in_scope=existing_in_scope,
        navigate_existing=navigate_existing,
        reason=reason,
        detail=detail,
        role=role or "unknown",
        component=component or "cdp",
        chrome_profile=chrome_profile,
        tab_factory=CdpTab,
        list_tabs_func=list_tabs,
        sso_intent=sso_intent,
        identity_mode=identity_mode,
        intended_identity=intended_identity,
        effective_identity=effective_identity,
        origin=origin,
        consent_operation_id=operation_id,
        allow_operation_scope=scoped_operation,
        backend=_navigation_backend(open_tab=open_for_reuse),
    )


def close_tab(cdp_endpoint: str, tab_id: str) -> None:
    """Hang up a meeting tab by its CDP id (e.g. the previous meeting after
    /join or /new switches to a fresh one) -- best-effort, never raises."""
    try:
        _http_touch(f"{cdp_endpoint}/json/close/{tab_id}")
    except Exception:
        pass


def read_google_account(tab: CdpTab | None) -> dict[str, Any] | None:
    """Read the live Google session represented by an account page."""
    if tab is None:
        return None
    try:
        raw = tab.evaluate(
            r"""
(() => {
  const ready = document.readyState;
  const url = location.href;
  const label = [...document.querySelectorAll('[aria-label*="Google Account"]')]
    .map((el) => (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim())
    .find((value) => /\([^()]+@[^()]+\)/.test(value));
  if (!label) return JSON.stringify({ signedIn: false, ready, url });
  const match = label.match(/\(([^()]+@[^()]+)\)/);
  return JSON.stringify({ signedIn: true, ready, url, label, email: match ? match[1] : null });
})()
"""
        )
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def scan_signed_in_sso_accounts(
    cdp_endpoint: str,
    *,
    authusers: list[int] | None = None,
    timeout: float = 5.0,
    reason: str = "account-scan",
    detail: str = "probe signed-in Google account slots without automating sign-in",
    role: str | None = "probe",
    component: str | None = "cdp",
    chrome_profile: str | Path | dict[str, Any] | None = None,
    sso_satisfied: bool | None = None,
    consent_operation_id: str | None = None,
    origin: str = "machine",
) -> list[dict[str, Any]]:
    """Probe Google slots under one exact, short-lived provider/profile scan consent."""
    accounts: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    automatic_slots = authusers is None
    slots = list(authusers) if authusers is not None else list(DEFAULT_SSO_AUTHUSER_PROBE_SLOTS)
    sso_state = "unknown" if sso_satisfied is None else str(bool(sso_satisfied)).lower()
    operation_id = consent_operation_id or f"account-scan:{uuid.uuid4().hex}"
    try:
        existing_ids = {str(tab.get("id")) for tab in list_tabs(cdp_endpoint) if tab.get("id")}
    except Exception:
        existing_ids = set()
    probe: CdpTab | None = None
    probe_info: dict[str, Any] | None = None
    try:
        for authuser in slots:
            target = f"https://myaccount.google.com/?authuser={authuser}"
            slot_detail = f"{detail}; authuser={authuser}; slots={slots}; ssoSatisfied={sso_state}"
            if probe is None:
                try:
                    probe_info = open_url(
                        cdp_endpoint,
                        target,
                        reason=reason,
                        detail=slot_detail,
                        role=role or "probe",
                        component=component or "cdp",
                        chrome_profile=chrome_profile,
                        sso_intent=navigator.SsoIntent.PREFLIGHT_SCAN,
                        consent_operation_id=operation_id,
                        allow_operation_scope=True,
                        origin=origin,
                    )
                except TypeError:
                    # Supports old embedders that monkeypatch the compatibility shim.
                    probe_info = open_url(cdp_endpoint, target)
                if not probe_info or not probe_info.get("webSocketDebuggerUrl"):
                    continue
                probe = CdpTab(probe_info["webSocketDebuggerUrl"])
            else:
                try:
                    navigator.navigate(
                        probe,
                        target,
                        cdp_endpoint=cdp_endpoint,
                        reason=reason,
                        detail=slot_detail,
                        role=role or "probe",
                        component=component or "cdp",
                        chrome_profile=chrome_profile,
                        tab_info=probe_info,
                        sso_intent=navigator.SsoIntent.PREFLIGHT_SCAN,
                        consent_operation_id=operation_id,
                        allow_operation_scope=True,
                        origin=origin,
                        backend=_navigation_backend(),
                    )
                except Exception:
                    try:
                        probe.close()
                    finally:
                        probe = None
                        probe_info = None
                    continue
            account: dict[str, Any] | None = None
            deadline = time.time() + timeout
            while time.time() < deadline:
                candidate = read_google_account(probe)
                email = str((candidate or {}).get("email") or "").strip().lower()
                if candidate and candidate.get("signedIn") is True and email:
                    account = dict(candidate)
                    account["email"] = email
                    break
                time.sleep(0.25)
            if account is None:
                if automatic_slots:
                    break
                continue
            email = str(account["email"])
            if email in seen_emails:
                if automatic_slots:
                    break
                continue
            seen_emails.add(email)
            account["authuser"] = authuser
            accounts.append(account)
    finally:
        tab_id = str((probe_info or {}).get("id") or "")
        try:
            if probe is not None:
                probe.close()
        finally:
            if tab_id and tab_id not in existing_ids:
                close_tab(cdp_endpoint, tab_id)
    return accounts


def browser_profile_root(
    cdp_endpoint: str,
    *,
    timeout: float = 3.0,
    reason: str = "profile-probe",
    detail: str = "open chrome://version once to resolve the live Chrome user-data directory",
    role: str | None = "probe",
    component: str | None = "cdp",
    chrome_profile: str | Path | dict[str, Any] | None = None,
) -> Path | None:
    """Return the user-data directory of the Chrome instance on a CDP port."""
    try:
        existing_ids = {str(tab.get("id")) for tab in list_tabs(cdp_endpoint) if tab.get("id")}
    except Exception:
        return None
    info = open_url(
        cdp_endpoint,
        "chrome://version/",
        reason=reason,
        detail=detail,
        role=role,
        component=component,
        chrome_profile=chrome_profile,
    )
    if not info or not info.get("webSocketDebuggerUrl"):
        return None
    tab = CdpTab(info["webSocketDebuggerUrl"])
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = str(tab.evaluate("document.body?.innerText || ''") or "")
            for line in text.splitlines():
                label, separator, value = line.partition("\t")
                if separator and label.strip() == "Profile Path" and value.strip():
                    root = Path(value.strip()).parent
                    set_browser_nav_profile(cdp_endpoint, root)
                    return root
            time.sleep(0.1)
    finally:
        tab.close()
        tab_id = str(info.get("id") or "")
        if tab_id and tab_id not in existing_ids:
            close_tab(cdp_endpoint, tab_id)
    return None


def _find_account_tab(
    cdp_endpoint: str,
    email: str,
    *,
    sso_connector_only: bool,
) -> dict[str, Any] | None:
    wanted_email = str(email or "").strip().lower()
    if not wanted_email:
        return None
    for info in list_tabs(cdp_endpoint):
        if info.get("type") != "page" or not info.get("webSocketDebuggerUrl"):
            continue
        url = str(info.get("url") or "")
        if sso_connector_only:
            if not re.match(r"https?://(?:accounts|myaccount)\.google\.com(?:[/:?#]|$)", url, re.IGNORECASE):
                continue
            if "accountchooser" in url.lower():
                continue
        tab = CdpTab(info["webSocketDebuggerUrl"])
        try:
            account = read_google_account(tab)
            if str((account or {}).get("email") or "").strip().lower() != wanted_email:
                continue
            return {
                "id": info.get("id"),
                "title": info.get("title"),
                "url": info.get("url"),
                "email": wanted_email,
            }
        except Exception:
            continue
        finally:
            tab.close()
    return None


def find_sso_tab(cdp_endpoint: str, email: str) -> dict[str, Any] | None:
    """Find any existing page for an account without creating or focusing one."""
    return _find_account_tab(cdp_endpoint, email, sso_connector_only=False)


def find_sso_connector_tab(cdp_endpoint: str, email: str) -> dict[str, Any] | None:
    """Find this identity's SSO connector without consuming another connector."""
    return _find_account_tab(cdp_endpoint, email, sso_connector_only=True)


def find_add_account_tab(cdp_endpoint: str) -> dict[str, Any] | None:
    """Find the unassigned add-account connector without consuming an SSO tab."""
    for info in list_tabs(cdp_endpoint):
        if info.get("type") != "page" or not info.get("webSocketDebuggerUrl"):
            continue
        url = str(info.get("url") or "")
        if re.match(r"https?://accounts\.google\.com(?:[/:?#]|$)", url, re.IGNORECASE) and "accountchooser" in url.lower():
            return info
    return None


def foreground_sso_tab(
    cdp_endpoint: str,
    email: str,
    *,
    reason: str = "foreground-sso",
    detail: str = "",
    role: str | None = "server",
    component: str | None = None,
    chrome_profile: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Foreground an existing account page without creating a tab."""
    found = find_sso_tab(cdp_endpoint, email)
    if found is None:
        return None
    reuse_or_open_tab(
        cdp_endpoint,
        str(found.get("url") or ""),
        existing_in_scope=found,
        reason=reason,
        detail=detail or f"foreground existing SSO tab for {email}",
        role=role,
        component=component,
        chrome_profile=chrome_profile,
        sso_intent=navigator.SsoIntent.FOREGROUND_EXISTING,
        intended_identity=email,
    )
    return found


def launch_browser(args: Any) -> tuple[str, subprocess.Popen[bytes] | None]:
    """Pop up the bridge's own browser so the operator can pick their Google
    account (SSO); returns the CDP endpoint once it is answering."""
    port = args.port
    cdp = f"http://127.0.0.1:{port}"
    profile = Path(args.profile).expanduser()
    set_browser_nav_profile(cdp, profile)
    profile.mkdir(parents=True, exist_ok=True)
    if not cdp_alive(cdp):
        url = getattr(args, "launch_url", None) or args.meet or ("https://meet.google.com/new" if args.new else "https://accounts.google.com/")
        detail = (
            "launch dedicated Meet browser; "
            f"remote-debugging-port={port}; user-data-dir={profile}; profile={profile}"
        )
        argv: list[str] = []

        def launch_command() -> list[str]:
            argv.extend(
                build_launch(
                    getattr(args, "browser_backend", "windows"),
                    port,
                    profile,
                    url,
                    browser=args.browser,
                    wsl_distro=getattr(args, "wsl_distro", None),
                    extra_args=["--autoplay-policy=no-user-gesture-required", "--new-window"],
                )
            )
            return argv

        process = navigator.launch(
            launch_command,
            cdp_endpoint=cdp,
            url=url,
            profile=profile,
            reason="browser-launch",
            detail=detail,
            role="host",
            component="meet_bridge",
            wait_until_ready=lambda: cdp_alive(cdp),
            ready_timeout=60,
            backend=_navigation_backend(),
        )
        label = Path(argv[0]).name if getattr(args, "browser_backend", "windows") == "windows" else "WSL Chrome/Chromium"
        print(f"[bridge] browser window opened ({label}, profile {profile})", flush=True)
        print("[bridge] pick your Google account in that window (SSO persists for next time)...", flush=True)
        if not cdp_alive(cdp):
            raise SystemExit("The launched browser never opened its DevTools port -- is another instance using the profile?")
        return cdp, process
    # An already-running profile already owns the shared browser window.
    # Role/account-aware orchestration in bridge.main selects and focuses the
    # correct connector tab after SSO preflight; opening here would race that
    # selection and can create a duplicate tab for the same connector.
    return cdp, None


def wait_for_meet_tab(cdp: str, timeout: float = 900.0, require_room: bool = False) -> dict[str, Any]:
    """Wait while the operator signs in and joins the call.

    With require_room, keep waiting until the tab has left transitional pages
    (meet.google.com/new, the landing page) and shows a real room URL like
    meet.google.com/xxx-yyyy-zzz.
    """
    room = re.compile(r"meet\.google\.com/[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}(\?|$|/)", re.IGNORECASE)
    told = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tab = find_meet_tab(cdp)
        except Exception:
            tab = None
        if tab and (not require_room or room.search(str(tab.get("url") or ""))):
            return tab
        if not told:
            told = True
            print("[bridge] waiting for a meet.google.com tab -- sign in and open the meeting in the popped-up window...", flush=True)
        time.sleep(1.5)
    raise SystemExit("Timed out waiting for a Google Meet tab (15 min).")
