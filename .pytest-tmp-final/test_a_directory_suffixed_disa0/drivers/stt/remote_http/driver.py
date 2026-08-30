"""Remote HTTP STT driver (opt-in only, task section 12).

Configured as ``remote:https://host/endpoint``. Audio is never sent off-device
unless ``WS_COLLAB_STT_ALLOW_REMOTE`` is set; otherwise the driver reports itself
unavailable with ``fallback=False`` so it is simply skipped rather than replaced.
"""

from __future__ import annotations

import time

from ws_collab.audio.segment import AudioSegment
from ws_collab.drivers import DriverUnavailable, SttDriverSpec
from ws_collab.stt.base import Hypothesis, PartialCallback, SttAdapter, normalize_text


class RemoteHttpAdapter(SttAdapter):
    is_remote = True

    def __init__(self, name: str, endpoint: str, model: str = "remote"):
        self.name = name
        self.model = model
        self.endpoint = endpoint

    async def transcribe(self, segment: AudioSegment, on_partial: PartialCallback | None = None) -> Hypothesis:
        start = time.perf_counter()
        try:
            import httpx
        except ImportError:
            return Hypothesis.failed(self.name, self.model, "httpx not installed for remote STT")
        if segment.samples is None:
            return Hypothesis.failed(self.name, self.model, "remote STT requires audio samples")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.endpoint, json={"sample_rate": segment.sample_rate})
                response.raise_for_status()
                body = response.json()
        except Exception as error:  # noqa: BLE001
            return Hypothesis.failed(self.name, self.model, f"remote error: {error}")
        text = str(body.get("text", ""))
        return Hypothesis(
            engine=self.name, model=self.model, raw_text=text, normalized_text=normalize_text(text),
            confidence=float(body.get("confidence", 0.5)), latency_ms=(time.perf_counter() - start) * 1000, is_remote=True,
        )


def _build(name: str, config) -> SttAdapter:
    if not getattr(config, "stt_allow_remote", False):
        raise DriverUnavailable("remote STT disabled; set WS_COLLAB_STT_ALLOW_REMOTE=1 to enable", fallback=False)
    endpoint = name.split(":", 1)[1] if ":" in name else ""
    if not endpoint:
        raise DriverUnavailable("remote STT requires an endpoint, e.g. 'remote:https://host/path'", fallback=False)
    return RemoteHttpAdapter(name, endpoint=endpoint)


def get_driver() -> SttDriverSpec:
    return SttDriverSpec(
        id="remote_http",
        aliases=["remote"],
        build=_build,
        description="Remote HTTP STT provider (opt-in). Configure as 'remote:https://host/path'.",
        is_remote=True,
    )
