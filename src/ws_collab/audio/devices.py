"""Audio device enumeration with stable identifiers (task section 11).

Devices come from a real backend when one is available (``sounddevice``/PortAudio
on this platform) and from a hardware-free catalog otherwise, so routing,
selection, meters, and hot-plug flows work identically with or without hardware.

Identifiers are stable -- derived from backend, host API, name and direction --
never from PortAudio's positional indexes, which shift when devices appear or
disappear. The volatile index is carried separately as ``backend_index`` for the
capture layer to open a stream with, and is re-resolved on every refresh.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config

DIRECTION_INPUT = "input"
DIRECTION_OUTPUT = "output"
DIRECTION_LOOPBACK = "loopback"
DIRECTION_VIRTUAL = "virtual"

# Names that indicate a device captures system output rather than a microphone.
_LOOPBACK_HINTS = ("stereo mix", "what u hear", "wave out", "loopback", "monitor of")
_VIRTUAL_HINTS = ("virtual", "vb-audio", "cable", "voicemeeter", "sound mapper")


def _stable_id(backend: str, host_api: str, name: str, direction: str) -> str:
    digest = hashlib.sha1(f"{backend}:{host_api}:{name}:{direction}".encode("utf-8")).hexdigest()[:12]
    return f"{backend}-{direction}-{digest}"


@dataclass
class Device:
    id: str
    name: str
    direction: str
    host_api: str = "fake"
    channels: int = 1
    sample_rates: list[int] = field(default_factory=lambda: [16000, 44100, 48000])
    formats: list[str] = field(default_factory=lambda: ["pcm_s16le", "pcm_f32le"])
    is_default_input: bool = False
    is_default_output: bool = False
    is_default_comm: bool = False
    is_default_multimedia: bool = False
    available: bool = True
    latency_ms: float = 20.0
    error: str | None = None
    backend: str = "fake"
    backend_index: int | None = None
    # Raw capture/playback capability, independent of ``direction``.
    # ``direction`` classifies a device into ONE bucket (e.g. any device
    # whose name matches a virtual-cable hint collapses to "virtual"
    # regardless of which side of the cable it actually is), which hides
    # whether a given "virtual" entry is the recording side or the
    # playback side. These two booleans always reflect the raw
    # max_input_channels/max_output_channels seen from the audio backend,
    # so the UI can show real "In"/"Out" capability columns that stay
    # correct even for devices whose ``direction`` is a collapsed category.
    supports_input: bool = False
    supports_output: bool = False
    # Independent, possibly multi-valued classification — NOT the same as
    # picking one bucket. A device can genuinely be more than one class at
    # once (e.g. a real Realtek "Stereo Mix" is both physical hardware AND
    # functions as a loopback monitor), which a single ``direction``/class
    # string can never represent. Always a non-empty subset of
    # {"physical", "loopback", "virtual"}; "physical" and "virtual" are
    # mutually exclusive by construction (a device is either real hardware
    # or a software endpoint), but either can combine with "loopback".
    classes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _classify(name: str, max_in: int, max_out: int) -> str:
    lowered = name.lower()
    if max_in > 0 and any(hint in lowered for hint in _LOOPBACK_HINTS):
        return DIRECTION_LOOPBACK
    if any(hint in lowered for hint in _VIRTUAL_HINTS):
        return DIRECTION_VIRTUAL
    return DIRECTION_INPUT if max_in > 0 else DIRECTION_OUTPUT


def _device_classes(name: str, max_in: int) -> list[str]:
    """Independent classification tags — a device may carry more than one.

    Unlike ``_classify`` (which folds direction and class into one bucket
    with a fixed priority order), this returns every applicable tag: a
    device is "virtual" if its name matches a known virtual-cable/mixer
    hint, otherwise "physical" (real hardware) — those two never combine —
    and separately gets "loopback" too whenever its name also matches a
    loopback hint AND it can capture (``max_in > 0``), since a device that
    only plays audio cannot itself be a loopback source.
    """
    lowered = name.lower()
    is_virtual = any(hint in lowered for hint in _VIRTUAL_HINTS)
    tags = ["virtual"] if is_virtual else ["physical"]
    if max_in > 0 and any(hint in lowered for hint in _LOOPBACK_HINTS):
        tags.append("loopback")
    return tags


def _fake_catalog() -> list[Device]:
    backend = "fake"

    def make(name: str, direction: str, *, supports_input: bool, supports_output: bool, **kw: Any) -> Device:
        return Device(
            id=_stable_id(backend, "fake", name, direction),
            name=name, direction=direction, host_api="fake", backend=backend,
            supports_input=supports_input, supports_output=supports_output,
            classes=_device_classes(name, max_in=1 if supports_input else 0),
            **kw,
        )

    return [
        make("Primary Microphone", DIRECTION_INPUT, supports_input=True, supports_output=False,
             channels=1, is_default_input=True, is_default_comm=True),
        make("Conference Array", DIRECTION_INPUT, supports_input=True, supports_output=False, channels=4),
        make("Primary Speakers", DIRECTION_OUTPUT, supports_input=False, supports_output=True,
             channels=2, is_default_output=True, is_default_multimedia=True),
        make("System Loopback", DIRECTION_LOOPBACK, supports_input=True, supports_output=False, channels=2),
        # A virtual cable is really a mirrored PAIR of devices (one plays,
        # the other records what was played) — modeled as two separate
        # entries here, matching how a real cable driver (e.g. VB-CABLE)
        # always registers on Windows, so the fake catalog exercises the
        # same "which side is this" ambiguity the UI needs to resolve.
        make("Virtual Cable (playback side)", DIRECTION_VIRTUAL, supports_input=False, supports_output=True, channels=2),
        make("Virtual Cable (recording side)", DIRECTION_VIRTUAL, supports_input=True, supports_output=False, channels=2),
        make("Virtual Cable B (playback side)", DIRECTION_VIRTUAL, supports_input=False, supports_output=True, channels=2),
        make("Virtual Cable B (recording side)", DIRECTION_VIRTUAL, supports_input=True, supports_output=False, channels=2),
    ]


def _sounddevice_catalog() -> tuple[list[Device], str | None]:
    """Enumerate real PortAudio devices. Returns ``(devices, error)``."""

    try:
        import sounddevice as sd
    except Exception as error:  # pragma: no cover - depends on the platform
        return [], f"sounddevice unavailable: {error}"

    try:
        raw_devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        default_input, default_output = sd.default.device
    except Exception as error:  # pragma: no cover - depends on hardware
        return [], f"device enumeration failed: {error}"

    # Per-host-API defaults mark the communications/multimedia devices.
    comm_inputs = set()
    multimedia_outputs = set()
    for api in host_apis:
        if api.get("default_input_device", -1) >= 0:
            comm_inputs.add(api["default_input_device"])
        if api.get("default_output_device", -1) >= 0:
            multimedia_outputs.add(api["default_output_device"])

    devices: list[Device] = []
    for index, entry in enumerate(raw_devices):
        name = str(entry.get("name", "unknown")).strip()
        max_in = int(entry.get("max_input_channels", 0))
        max_out = int(entry.get("max_output_channels", 0))
        if max_in == 0 and max_out == 0:
            continue
        api_index = int(entry.get("hostapi", 0))
        host_api = str(host_apis[api_index]["name"]) if api_index < len(host_apis) else str(api_index)
        direction = _classify(name, max_in, max_out)
        is_input_side = direction in (DIRECTION_INPUT, DIRECTION_LOOPBACK) or max_in > 0
        default_rate = int(entry.get("default_samplerate", 44100) or 44100)
        latency_key = "default_low_input_latency" if is_input_side else "default_low_output_latency"
        devices.append(
            Device(
                id=_stable_id("sounddevice", host_api, name, direction),
                name=name,
                direction=direction,
                host_api=host_api,
                channels=max(max_in, max_out) or 1,
                sample_rates=sorted({16000, default_rate}),
                formats=["pcm_s16le", "pcm_f32le"],
                is_default_input=(index == default_input),
                is_default_output=(index == default_output),
                is_default_comm=(index in comm_inputs),
                is_default_multimedia=(index in multimedia_outputs),
                available=True,
                latency_ms=round(float(entry.get(latency_key, 0.02) or 0.02) * 1000, 2),
                backend="sounddevice",
                backend_index=index,
                supports_input=max_in > 0,
                supports_output=max_out > 0,
                classes=_device_classes(name, max_in),
            )
        )
    return devices, None


def enumerate_devices(config: Config) -> tuple[list[Device], list[str]]:
    """Return ``(devices, notes)`` for the configured backend.

    ``auto`` (the default) prefers real hardware and falls back to the
    hardware-free catalog, reporting why, so a machine without a working audio
    stack still runs the whole pipeline.
    """

    backend = (config.audio_backend or "auto").lower()
    notes: list[str] = []

    if backend == "fake":
        return _fake_catalog(), notes

    devices, error = _sounddevice_catalog()
    if devices:
        return devices, notes

    if backend == "sounddevice":
        notes.append(f"real audio backend requested but unavailable: {error}; using the fake catalog")
    else:
        notes.append(f"no real audio devices ({error}); using the fake catalog")
    return _fake_catalog(), notes


class DeviceRegistry:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self.generation = 0
        self.notes: list[str] = []
        self.refresh()

    def refresh(self) -> list[Device]:
        """Re-enumerate. Stable ids survive; volatile indexes are re-resolved."""

        with self._lock:
            devices, notes = enumerate_devices(self.config)
            self._devices = {device.id: device for device in devices}
            self.notes = notes
            self.generation += 1
            return list(self._devices.values())

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [device.public() for device in self._devices.values()]

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def inputs(self) -> list[Device]:
        with self._lock:
            return [
                device for device in self._devices.values()
                if device.direction in (DIRECTION_INPUT, DIRECTION_LOOPBACK, DIRECTION_VIRTUAL)
            ]

    def default_input(self) -> Device | None:
        with self._lock:
            for device in self._devices.values():
                if device.is_default_input:
                    return device
            for device in self._devices.values():
                if device.direction == DIRECTION_INPUT:
                    return device
            return None

    def default_output(self) -> Device | None:
        with self._lock:
            for device in self._devices.values():
                if device.is_default_output:
                    return device
            return None

    @property
    def backend(self) -> str:
        with self._lock:
            for device in self._devices.values():
                return device.backend
            return "none"

    def simulate_hotplug(self, device: Device, *, removed: bool = False) -> None:
        """Test/diagnostic hook to add or remove a device and bump the generation."""

        with self._lock:
            if removed:
                self._devices.pop(device.id, None)
            else:
                self._devices[device.id] = device
            self.generation += 1
