"""STT engine assembly and the concurrent runner.

Engines are provided by drop-in drivers discovered under
``ws_collab/drivers/stt`` (see :mod:`ws_collab.drivers`). ``build_engines`` maps
the configured engine names in ``WS_COLLAB_STT_ENGINES`` onto those drivers,
falling back to a deterministic double when an optional driver's model/library is
missing. ``run_stt`` executes every engine concurrently with per-engine timeouts
so one slow or failing engine never cancels the others (task section 12).
"""

from __future__ import annotations

import asyncio
import time

from ..audio.segment import AudioSegment
from ..config import Config
from ..drivers import DriverUnavailable, SttDriverSpec, discover_stt_drivers
from .base import Hypothesis, PartialCallback, SttAdapter


def _match(name: str, specs: list[SttDriverSpec]) -> SttDriverSpec | None:
    lowered = name.lower()
    for spec in specs:
        for alias in spec.aliases:
            alias = alias.lower()
            if lowered == alias or lowered.startswith(alias + ":"):
                return spec
    return None


def build_engines(config: Config) -> tuple[list[SttAdapter], list[str]]:
    """Construct the configured STT engines from discovered drivers."""

    specs, warnings = discover_stt_drivers()
    warnings = list(warnings)
    deterministic = next((spec for spec in specs if spec.id == "deterministic"), None)

    engines: list[SttAdapter] = []
    for name in config.stt_engines:
        spec = _match(name, specs) or deterministic
        if spec is None:
            warnings.append(f"no driver matched STT engine {name!r} and no deterministic driver is available")
            continue
        try:
            engines.append(spec.build(name, config))
        except DriverUnavailable as error:
            if error.fallback and deterministic is not None and spec is not deterministic:
                warnings.append(f"STT engine {name!r}: {error}; using a deterministic double")
                engines.append(deterministic.build(name, config))
            else:
                warnings.append(f"STT engine {name!r}: {error} (skipped)")
        except Exception as error:  # noqa: BLE001 - a bad driver must not crash startup
            warnings.append(f"STT engine {name!r} failed to build: {error}")

    if not engines and deterministic is not None:
        warnings.append("no STT engines configured; adding a single deterministic double")
        engines.append(deterministic.build("fallback_alpha", config))
    return engines, warnings


async def run_stt(
    engines: list[SttAdapter],
    segment: AudioSegment,
    *,
    timeout_ms: int,
    concurrency: int,
    on_partial: PartialCallback | None = None,
) -> list[Hypothesis]:
    """Run every engine concurrently with per-engine timeouts and isolation."""

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(engine: SttAdapter) -> Hypothesis:
        async with semaphore:
            start = time.perf_counter()
            try:
                return await asyncio.wait_for(engine.transcribe(segment, on_partial), timeout=timeout_ms / 1000.0)
            except asyncio.TimeoutError:
                return Hypothesis.failed(engine.name, engine.model, "timeout", (time.perf_counter() - start) * 1000)
            except Exception as error:  # noqa: BLE001 - one engine must not fail the batch
                return Hypothesis.failed(engine.name, engine.model, str(error), (time.perf_counter() - start) * 1000)

    return list(await asyncio.gather(*(_one(engine) for engine in engines)))
