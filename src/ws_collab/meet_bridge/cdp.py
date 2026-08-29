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
import time
import urllib.request
from pathlib import Path
from typing import Any

from websocket import create_connection  # websocket-client

DEFAULT_CDP = "http://127.0.0.1:9222"
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_OLD_DEFAULT_PROFILE = Path.home() / ".cache" / "ws_collab_models" / "meet_bridge_profile"
DEFAULT_PROFILE = Path(
    os.environ.get("WS_COLLAB_MEET_PROFILE_DIR")
    or (_PLUGIN_ROOT / "collab_state" / "meet_bridge_profile")
)

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
WSL_BROWSER_CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]


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
        print(f"[bridge] migrated existing Chrome profile from {old} to {target}")
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
) -> list[str]:
    extra_args = list(extra_args or [])
    if backend == "windows":
        return [
            find_browser(browser),
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

    def bring_to_front(self) -> None:
        """Activate this tab AND raise its browser window -- the CDP method
        built for exactly this (unlike Runtime.evaluate tricks, which can't
        reliably raise a window the OS considers unfocused)."""
        self.call("Page.bringToFront")

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


def open_url(cdp_endpoint: str, target: str) -> dict[str, Any] | None:
    try:
        return _http_json(f"{cdp_endpoint}/json/new?{target}", method="PUT")
    except Exception:
        try:
            return _http_json(f"{cdp_endpoint}/json/new?{target}", method="GET")
        except Exception:
            return None


def close_tab(cdp_endpoint: str, tab_id: str) -> None:
    """Hang up a meeting tab by its CDP id (e.g. the previous meeting after
    /join or /new switches to a fresh one) -- best-effort, never raises."""
    try:
        _http_touch(f"{cdp_endpoint}/json/close/{tab_id}")
    except Exception:
        pass


def launch_browser(args: Any) -> tuple[str, subprocess.Popen[bytes] | None]:
    """Pop up the bridge's own browser so the operator can pick their Google
    account (SSO); returns the CDP endpoint once it is answering."""
    port = args.port
    cdp = f"http://127.0.0.1:{port}"
    profile = Path(args.profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    if not cdp_alive(cdp):
        url = getattr(args, "launch_url", None) or args.meet or ("https://meet.google.com/new" if args.new else "https://accounts.google.com/")
        argv = build_launch(
            getattr(args, "browser_backend", "windows"),
            port,
            profile,
            url,
            browser=args.browser,
            wsl_distro=getattr(args, "wsl_distro", None),
            extra_args=["--autoplay-policy=no-user-gesture-required", "--new-window"],
        )
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        label = Path(find_browser(args.browser)).name if getattr(args, "browser_backend", "windows") == "windows" else "WSL Chrome/Chromium"
        print(f"[bridge] browser window opened ({label}, profile {profile})")
        print("[bridge] pick your Google account in that window (SSO persists for next time)...")
        deadline = time.time() + 60
        while time.time() < deadline and not cdp_alive(cdp):
            time.sleep(0.5)
        if not cdp_alive(cdp):
            raise SystemExit("The launched browser never opened its DevTools port -- is another instance using the profile?")
        return cdp, process
    elif args.meet or args.new:
        # Browser already up: only open a NEW tab if one isn't already
        # sitting on this exact meeting room. Otherwise every restart of
        # just the python process (Chrome left running) clones a duplicate
        # tab, and Meet -- seeing the same account twice -- offers a
        # "Switch the call here / Join here too" prompt on the new one
        # instead of just reattaching to the real, already-in-call tab.
        target = getattr(args, "launch_url", None) or args.meet or "https://meet.google.com/new"
        existing = find_meet_tab(cdp)
        room = re.compile(r"meet\.google\.com/([a-z0-9-]+)", re.IGNORECASE)
        target_match = room.search(target)
        target_room = target_match.group(1) if target_match else None
        existing_match = room.search(str(existing.get("url") or "")) if existing else None
        existing_room = existing_match.group(1) if existing_match else None
        if not (existing and target_room and target_room == existing_room):
            open_url(cdp, target)
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
            print("[bridge] waiting for a meet.google.com tab -- sign in and open the meeting in the popped-up window...")
        time.sleep(1.5)
    raise SystemExit("Timed out waiting for a Google Meet tab (15 min).")
