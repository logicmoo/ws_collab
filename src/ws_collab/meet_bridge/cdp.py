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
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from websocket import create_connection  # websocket-client

DEFAULT_CDP = "http://127.0.0.1:9222"
# Reuses the same on-disk cache root ws_collab already uses for downloaded STT
# models, so all of ws_collab's larger local state lives under one directory
# by default. Override with WS_COLLAB_MEET_PROFILE_DIR.
DEFAULT_PROFILE = Path(
    os.environ.get("WS_COLLAB_MEET_PROFILE_DIR")
    or (Path.home() / ".cache" / "ws_collab_models" / "meet_bridge_profile")
)

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local trusted CDP endpoint
        return json.loads(response.read().decode("utf-8"))


def _http_touch(url: str, *, method: str = "GET", timeout: float = 5.0) -> None:
    """Fire a request whose body we don't need (CDP's /json/new, /json/close)."""
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 - local trusted CDP endpoint
        pass


class CdpTab:
    def __init__(self, ws_url: str) -> None:
        # suppress_origin: Chrome rejects DevTools websocket handshakes that
        # carry a browser-style Origin header with HTTP 403.
        self.ws = create_connection(ws_url, timeout=10, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        self._id += 1
        wanted = self._id
        self.ws.send(json.dumps({"id": wanted, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = json.loads(self.ws.recv())
            if payload.get("id") == wanted:
                return payload.get("result")
        raise TimeoutError(f"CDP {method} timed out")

    def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 10.0) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        return ((result or {}).get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


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


def find_browser(explicit: str | None) -> str:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise SystemExit(f"--browser not found: {explicit}")
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("chrome", "msedge"):
        found = subprocess.run(["where.exe", name], capture_output=True, text=True, check=False)
        line = (found.stdout or "").strip().splitlines()
        if line:
            return line[0]
    raise SystemExit("No Chrome/Edge found -- pass --browser <path to chrome.exe>")


def open_url(cdp_endpoint: str, target: str) -> None:
    try:
        _http_touch(f"{cdp_endpoint}/json/new?{target}", method="PUT")
    except Exception:
        try:
            _http_touch(f"{cdp_endpoint}/json/new?{target}", method="GET")
        except Exception:
            pass


def close_tab(cdp_endpoint: str, tab_id: str) -> None:
    """Hang up a meeting tab by its CDP id (e.g. the previous meeting after
    /join or /new switches to a fresh one) -- best-effort, never raises."""
    try:
        _http_touch(f"{cdp_endpoint}/json/close/{tab_id}")
    except Exception:
        pass


def launch_browser(args: Any) -> str:
    """Pop up the bridge's own browser so the operator can pick their Google
    account (SSO); returns the CDP endpoint once it is answering."""
    port = args.port
    cdp = f"http://127.0.0.1:{port}"
    profile = Path(args.profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    if not cdp_alive(cdp):
        browser = find_browser(args.browser)
        url = args.meet or ("https://meet.google.com/new" if args.new else "https://accounts.google.com/")
        subprocess.Popen(
            [
                browser,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--new-window",
                url,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"[bridge] browser window opened ({Path(browser).name}, profile {profile})")
        print("[bridge] pick your Google account in that window (SSO persists for next time)...")
        deadline = time.time() + 60
        while time.time() < deadline and not cdp_alive(cdp):
            time.sleep(0.5)
        if not cdp_alive(cdp):
            raise SystemExit("The launched browser never opened its DevTools port -- is another instance using the profile?")
    elif args.meet or args.new:
        # Browser already up: only open a NEW tab if one isn't already
        # sitting on this exact meeting room. Otherwise every restart of
        # just the python process (Chrome left running) clones a duplicate
        # tab, and Meet -- seeing the same account twice -- offers a
        # "Switch the call here / Join here too" prompt on the new one
        # instead of just reattaching to the real, already-in-call tab.
        target = args.meet or "https://meet.google.com/new"
        existing = find_meet_tab(cdp)
        room = re.compile(r"meet\.google\.com/([a-z0-9-]+)", re.IGNORECASE)
        target_match = room.search(target)
        target_room = target_match.group(1) if target_match else None
        existing_match = room.search(str(existing.get("url") or "")) if existing else None
        existing_room = existing_match.group(1) if existing_match else None
        if not (existing and target_room and target_room == existing_room):
            open_url(cdp, target)
    return cdp


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
            print("[bridge] waiting for a meet.google.com tab -- sign in and open the meeting in the popped-up window...")
        time.sleep(1.5)
    raise SystemExit("Timed out waiting for a Google Meet tab (15 min).")
