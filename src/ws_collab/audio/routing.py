"""Per-source, per-engine audio routing matrix (task section 11).

Every (source, STT engine) pair can be routed to a specific device with its own
gain, VAD, language hint, and eligibility flags (diagnostic / command / TTS
accuracy). Routes persist atomically with stable IDs and every change is audited.
The manager never silently substitutes an unrelated fallback microphone -- a
fallback is only applied when explicitly configured on the route.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import ValidationError


@dataclass
class Route:
    source: str
    engine: str
    device_id: str
    id: str = ""
    gain: float = 1.0
    vad: bool = True
    language_hint: str = "en"
    sample_rate: int = 16000
    channels: int = 1
    audio_format: str = "pcm_s16le"
    frame_ms: int = 20
    noise_reduction: bool = False
    echo_cancellation: bool = False
    diagnostic_eligible: bool = True
    command_eligible: bool = True
    tts_accuracy_eligible: bool = False
    fallback_device_id: str = ""
    fallback_policy: str = "none"  # none | explicit_device | fail

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.source}->{self.engine}"

    def public(self) -> dict[str, Any]:
        return asdict(self)


class RoutingManager:
    def __init__(self, directory: str | Path, audit_sink: Callable[[dict[str, Any]], None] | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "routing.json"
        self._lock = threading.RLock()
        self._audit = audit_sink
        self._routes: dict[str, Route] = {}
        self._load()

    @staticmethod
    def _key(source: str, engine: str) -> str:
        return f"{source}\x1f{engine}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in payload.get("routes", []):
            route = Route(**{k: entry[k] for k in entry if k in Route.__annotations__})
            self._routes[self._key(route.source, route.engine)] = route

    def _save(self) -> None:
        payload = {"routes": [route.public() for route in self._routes.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def matrix(self) -> list[dict[str, Any]]:
        with self._lock:
            return [route.public() for route in self._routes.values()]

    def get(self, source: str, engine: str) -> Route | None:
        with self._lock:
            return self._routes.get(self._key(source, engine))

    def set_route(self, source: str, engine: str, device_id: str, *, operator: str = "operator", **params: Any) -> Route:
        if not source or not engine or not device_id:
            raise ValidationError("source, engine, and device_id are required")
        policy = params.get("fallback_policy", "none")
        if policy not in {"none", "explicit_device", "fail"}:
            raise ValidationError(f"invalid fallback_policy: {policy!r}")
        with self._lock:
            route = Route(source=source, engine=engine, device_id=device_id)
            for key, value in params.items():
                if key in Route.__annotations__ and key not in {"source", "engine", "id"}:
                    setattr(route, key, value)
            self._routes[self._key(source, engine)] = route
            self._save()
        self._audit_event("ROUTE_SET", source=source, engine=engine, device_id=device_id, operator=operator)
        return route

    def delete_route(self, source: str, engine: str, *, operator: str = "operator") -> bool:
        with self._lock:
            removed = self._routes.pop(self._key(source, engine), None)
            if removed:
                self._save()
        if removed:
            self._audit_event("ROUTE_DELETED", source=source, engine=engine, operator=operator)
        return removed is not None

    def resolve(self, source: str, engine: str, available_device_ids: set[str]) -> dict[str, Any]:
        """Resolve the device for a route, applying only an explicit fallback."""

        with self._lock:
            route = self._routes.get(self._key(source, engine))
        if route is None:
            return {"device_id": None, "note": "no route configured", "fallback_applied": False}
        if route.device_id in available_device_ids:
            return {"device_id": route.device_id, "route": route.public(), "fallback_applied": False}
        if route.fallback_policy == "explicit_device" and route.fallback_device_id in available_device_ids:
            self._audit_event("ROUTE_FALLBACK", source=source, engine=engine, to=route.fallback_device_id)
            return {"device_id": route.fallback_device_id, "route": route.public(), "fallback_applied": True}
        if route.fallback_policy == "fail":
            from ..errors import ConflictError

            raise ConflictError("route device unavailable and fallback policy is 'fail'",
                                details={"source": source, "engine": engine})
        return {"device_id": None, "note": "route device unavailable; no fallback", "fallback_applied": False}

    def _audit_event(self, action: str, **fields: Any) -> None:
        if self._audit is not None:
            self._audit({"type": "ROUTING_AUDIT", "action": action, **fields})
