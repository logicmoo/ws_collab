"""Validation and stable selectors for two-cable companion audio wiring."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from .errors import ValidationError

WIRING_KEY = "companion_cable_wiring"
ENDPOINTS = (
    "receive_playback_sink",
    "receive_capture_input",
    "transmit_tts_output",
    "transmit_companion_mic",
)
_OUTPUT_ENDPOINTS = {"receive_playback_sink", "transmit_tts_output"}
_INPUT_ENDPOINTS = {"receive_capture_input", "transmit_companion_mic"}


def normalize_device_label(value: Any) -> str:
    """Normalize a persisted label without weakening exact browser matching."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def resolve_exact_normalized_label(
    devices: Iterable[dict[str, Any]], selector: str, *, kind: str
) -> dict[str, Any]:
    """Resolve exactly one browser device by normalized label, never by default."""

    normalized = normalize_device_label(selector)
    matches = [
        device
        for device in devices
        if device.get("kind") == kind
        and normalize_device_label(device.get("label")) == normalized
    ]
    if not normalized:
        raise ValueError(f"{kind} normalized label selector is empty")
    if not matches:
        raise ValueError(f"no {kind} device exactly matches normalized label {normalized!r}")
    if len(matches) != 1:
        raise ValueError(
            f"{len(matches)} {kind} devices match normalized label {normalized!r}; "
            "choose a label that is unique after browser permission"
        )
    return matches[0]


def _pair_identity(label: str) -> str:
    """Derive only identities exposed by known virtual-cable naming schemes."""

    value = re.sub(r"[^a-z0-9]+", " ", normalize_device_label(label))
    value = " ".join(value.split())
    if "voicemeeter" in value:
        if re.search(r"\b(?:vaio\s*3|vaio3)\b", value):
            return "voicemeeter:vaio3"
        if re.search(r"\baux\b", value):
            return "voicemeeter:aux"
        if re.search(r"\bvaio\b", value):
            return "voicemeeter:vaio"
        return ""

    cable_matches = list(
        re.finditer(
            r"\b(?:vb\s+audio\s+|vb\s+|audio\s+)?(?:virtual\s+)?"
            r"cable(?:\s+(?P<bus>[a-d]|\d+))?\b",
            value,
        )
    )
    if cable_matches:
        buses = {match.group("bus") for match in cable_matches if match.group("bus")}
        if len(buses) > 1:
            return ""
        return f"vb-cable:{next(iter(buses), 'default')}"
    return ""


def _endpoint_from_device(name: str, device: dict[str, Any]) -> dict[str, Any]:
    label = str(device.get("name") or "").strip()
    return {
        "serverDeviceId": str(device.get("id") or ""),
        "serverIndex": device.get("backend_index"),
        "label": label,
        "normalizedLabel": normalize_device_label(label),
        "hostApi": str(device.get("host_api") or ""),
        "direction": "browser audio output / server playback"
        if name in _OUTPUT_ENDPOINTS
        else "browser audio input / server recording",
        "pairIdentity": _pair_identity(label),
    }


def build_wiring_config(
    payload: dict[str, Any], devices: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Build a machine-wide config from four exact stable server device IDs."""

    if not isinstance(payload, dict):
        raise ValidationError("companion cable wiring must be an object")
    catalog = {str(device.get("id") or ""): device for device in devices}
    result: dict[str, Any] = {"mode": "cable", "version": 1}
    unknown = sorted(set(payload) - set(ENDPOINTS) - {"mode", "version"})
    if unknown:
        raise ValidationError(f"unknown companion cable wiring field: {unknown[0]}")
    for name in ENDPOINTS:
        raw = payload.get(name)
        device_id = (
            str(raw.get("serverDeviceId") or raw.get("device_id") or "")
            if isinstance(raw, dict)
            else str(raw or "")
        ).strip()
        if not device_id:
            raise ValidationError(f"{name} is required")
        device = catalog.get(device_id)
        if device is None:
            raise ValidationError(f"{name} device {device_id!r} is unavailable; refresh devices")
        capability = "supports_output" if name in _OUTPUT_ENDPOINTS else "supports_input"
        if device.get(capability) is not True:
            raise ValidationError(f"{name} must support {'playback' if name in _OUTPUT_ENDPOINTS else 'recording'}")
        if "virtual" not in (device.get("classes") or []):
            raise ValidationError(f"{name} must be an explicitly enumerated virtual audio endpoint")
        result[name] = _endpoint_from_device(name, device)
    validation = validate_wiring_config(result)
    if not validation["valid"]:
        raise ValidationError("; ".join(validation["errors"]))
    return result


def validate_wiring_config(config: dict[str, Any] | None) -> dict[str, Any]:
    errors: list[str] = []
    config = config if isinstance(config, dict) else {}
    for name in ENDPOINTS:
        endpoint = config.get(name)
        if not isinstance(endpoint, dict):
            errors.append(f"{name} is not configured")
            continue
        if not str(endpoint.get("serverDeviceId") or "").strip():
            errors.append(f"{name} has no exact server device ID")
        if not normalize_device_label(endpoint.get("normalizedLabel") or endpoint.get("label")):
            errors.append(f"{name} has no normalized browser label selector")

    if not errors:
        identities = {
            name: _pair_identity(
                str(config[name].get("normalizedLabel") or config[name].get("label") or "")
            )
            for name in ENDPOINTS
        }
        ambiguous = [name for name, identity in identities.items() if not identity]
        if ambiguous:
            errors.append(
                "could not establish a physical VB-CABLE/Voicemeeter pair for "
                f"{', '.join(ambiguous)}; select endpoints whose labels include the same "
                "explicit cable/bus name (for example CABLE A Input/Output or "
                "Voicemeeter AUX Input/Output)"
            )
        else:
            receive = (
                identities["receive_playback_sink"],
                identities["receive_capture_input"],
            )
            transmit = (
                identities["transmit_tts_output"],
                identities["transmit_companion_mic"],
            )
            if receive[0] != receive[1]:
                errors.append(
                    "RECEIVE playback and recording endpoints are crossed; select both "
                    "halves of the same physical cable pair"
                )
            if transmit[0] != transmit[1]:
                errors.append(
                    "TRANSMIT TTS output and companion mic endpoints are crossed; select "
                    "both halves of the same physical cable pair"
                )
            if receive[0] == receive[1] == transmit[0] == transmit[1]:
                errors.append("RECEIVE and TRANSMIT resolve to the same virtual cable pair")
    return {"valid": not errors, "errors": errors}
