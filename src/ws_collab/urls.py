"""Canonical URL paths shared by servers and internal clients."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

DEFAULT_ROUTE_PREFIX = "/ws_collab"


def normalize_route_prefix(value: str | None = None) -> str:
    """Return one leading-slash namespace with no trailing slash."""

    raw = str(value or DEFAULT_ROUTE_PREFIX).strip()
    segments: list[str] = []
    for segment in raw.replace("\\", "/").split("/"):
        if not segment:
            continue
        if segments and segment == segments[-1] and segment == "ws_collab":
            continue
        segments.append(segment)
    return "/" + "/".join(segments) if segments else DEFAULT_ROUTE_PREFIX


def join_path(prefix: str, *parts: str) -> str:
    """Compose URL path segments without repeating a boundary segment."""

    segments = [segment for segment in normalize_route_prefix(prefix).split("/") if segment]
    for part in parts:
        incoming = [segment for segment in str(part).replace("\\", "/").split("/") if segment]
        overlap = min(len(segments), len(incoming))
        while overlap and segments[-overlap:] != incoming[:overlap]:
            overlap -= 1
        segments.extend(incoming[overlap:])
    return "/" + "/".join(segments)


def rest_base(route_prefix: str = DEFAULT_ROUTE_PREFIX) -> str:
    return normalize_route_prefix(route_prefix)


def websocket_path(route_prefix: str = DEFAULT_ROUTE_PREFIX) -> str:
    return join_path(route_prefix, "ws")


def admin_base(route_prefix: str = DEFAULT_ROUTE_PREFIX) -> str:
    return join_path(route_prefix, "admin")


def openapi_base(route_prefix: str = DEFAULT_ROUTE_PREFIX) -> str:
    return join_path(route_prefix, "openapi")


MEET_BRIDGE_PREFIX = join_path(DEFAULT_ROUTE_PREFIX, "meet-bridge")
MEET_BRIDGE_HEALTH = join_path(MEET_BRIDGE_PREFIX, "health")
MEET_BRIDGE_CAPTIONS = join_path(MEET_BRIDGE_PREFIX, "captions")
MEET_BRIDGE_COMMAND = join_path(MEET_BRIDGE_PREFIX, "command")
MEET_BRIDGE_SPEECH = join_path(MEET_BRIDGE_PREFIX, "speech")
MEET_BRIDGE_SPEECH_CANCEL = join_path(MEET_BRIDGE_SPEECH, "cancel")
MEET_BRIDGE_SPEECH_STATUS = join_path(MEET_BRIDGE_SPEECH, "status")
MEET_BRIDGE_WIRE_AUDIO = join_path(MEET_BRIDGE_PREFIX, "wire-companion-audio")
MEET_BRIDGE_DISCONNECT_AUDIO = join_path(
    MEET_BRIDGE_PREFIX, "disconnect-companion-audio"
)
MEET_BRIDGE_HTTP_PATHS = (
    MEET_BRIDGE_HEALTH,
    MEET_BRIDGE_CAPTIONS,
    MEET_BRIDGE_COMMAND,
    MEET_BRIDGE_SPEECH,
    MEET_BRIDGE_SPEECH_CANCEL,
    MEET_BRIDGE_SPEECH_STATUS,
    MEET_BRIDGE_WIRE_AUDIO,
    MEET_BRIDGE_DISCONNECT_AUDIO,
)
MEET_BRIDGE_ORIGIN = "http://127.0.0.1:48699"
_MEET_BRIDGE_GET_PATHS = frozenset({MEET_BRIDGE_HEALTH, MEET_BRIDGE_CAPTIONS})
_MEET_BRIDGE_POST_PATHS = frozenset(MEET_BRIDGE_HTTP_PATHS) - _MEET_BRIDGE_GET_PATHS


def meet_bridge_route_allowed(method: str, path: str) -> bool:
    route = path.rstrip("/") or "/"
    verb = method.upper()
    if verb == "GET":
        return route in _MEET_BRIDGE_GET_PATHS
    if verb == "POST":
        return route in _MEET_BRIDGE_POST_PATHS
    if verb == "OPTIONS":
        return route in MEET_BRIDGE_HTTP_PATHS
    return False


def meet_bridge_url(path: str, origin: str = MEET_BRIDGE_ORIGIN) -> str:
    return f"{origin.rstrip('/')}{path}"


def with_url_path(base_url: str, path: str) -> str:
    """Replace an origin/base URL's path with a canonical absolute path."""

    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
