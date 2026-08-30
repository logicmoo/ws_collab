"""Drop-in driver framework for STT and TTS.

Drivers live in their own directories under ``ws_collab/drivers/stt`` and
``ws_collab/drivers/tts``. Each driver directory contains a ``driver.py`` that
exposes ``get_driver()`` returning a :class:`SttDriverSpec` or
:class:`TtsDriverSpec`. At startup the drivers are enumerated:

* A directory whose name ends with ``_disabled`` (or ``.disabled``) is skipped,
  so an operator can disable a driver by renaming its folder or deleting it.
* A ``driver.json`` manifest with ``{"enabled": false}`` also disables it.
* Directories beginning with ``_`` or ``.`` are ignored.

Driver ``driver.py`` files use absolute imports (``from ws_collab...``) so they
load cleanly whether the package is run standalone or mounted as a plugin.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_DRIVERS_ROOT = Path(__file__).resolve().parent
_MODULE_CACHE: dict[str, Any] = {}


class DriverUnavailable(Exception):
    """Raised by a driver's ``build`` when it cannot construct its adapter.

    ``fallback=True`` means the caller may substitute a deterministic double
    (e.g. an optional model/library is missing); ``fallback=False`` means the
    driver should simply be skipped (e.g. a remote driver disabled by policy).
    """

    def __init__(self, message: str, *, fallback: bool = True):
        super().__init__(message)
        self.fallback = fallback


@dataclass
class SttDriverSpec:
    id: str
    aliases: list[str]
    build: Callable[[str, Any], Any]  # (engine_name, config) -> SttAdapter | raises DriverUnavailable
    description: str = ""
    is_remote: bool = False
    directory: str = ""


@dataclass
class TtsDriverSpec:
    id: str
    build: Callable[[Any], Any]  # (config) -> backend | raises DriverUnavailable
    description: str = ""
    directory: str = ""


def pcm_to_float32(samples: Any, sample_rate: int) -> Any:
    """Coerce PCM samples (numpy/int16/bytes) to mono float32 in [-1, 1]."""

    import numpy as np

    if isinstance(samples, (bytes, bytearray)):
        array = np.frombuffer(bytes(samples), dtype=np.int16).astype(np.float32) / 32768.0
    else:
        array = np.asarray(samples)
        if array.dtype.kind in "iu":
            array = array.astype(np.float32) / 32768.0
        else:
            array = array.astype(np.float32)
    if array.ndim > 1:
        array = array.mean(axis=1)
    return array


def logprob_to_confidence(avg_logprob: float) -> float:
    import math

    return max(0.0, min(1.0, math.exp(avg_logprob)))


def _is_disabled_dir(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith("_disabled") or lowered.endswith(".disabled")


def _load_driver_module(driver_py: Path, module_name: str) -> Any:
    cached = _MODULE_CACHE.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, driver_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load driver module: {driver_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE_CACHE[module_name] = module
    return module


def _discover(kind: str) -> tuple[list[Any], list[str]]:
    base = _DRIVERS_ROOT / kind
    specs: list[Any] = []
    notes: list[str] = []
    if not base.is_dir():
        return specs, [f"no {kind} drivers directory at {base}"]
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_") or name.startswith("."):
            continue
        if _is_disabled_dir(name):
            notes.append(f"skipped disabled {kind} driver directory: {name}")
            continue
        manifest = child / "driver.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("enabled") is False:
                    notes.append(f"skipped {kind} driver {name}: driver.json enabled=false")
                    continue
            except (OSError, json.JSONDecodeError) as error:
                notes.append(f"invalid driver.json for {kind} driver {name}: {error}")
        driver_py = child / "driver.py"
        if not driver_py.is_file():
            continue
        try:
            module = _load_driver_module(driver_py, f"ws_collab_driver_{kind}_{name}")
            spec = module.get_driver()
            spec.directory = str(child)
            specs.append(spec)
        except Exception as error:  # noqa: BLE001 - a bad driver must not crash startup
            notes.append(f"failed to load {kind} driver {name}: {error}")
    return specs, notes


def discover_stt_drivers() -> tuple[list[SttDriverSpec], list[str]]:
    return _discover("stt")


def discover_tts_drivers() -> tuple[list[TtsDriverSpec], list[str]]:
    return _discover("tts")
