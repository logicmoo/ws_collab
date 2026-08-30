"""Fake TTS driver: a hardware-free simulator (default backend).

Playback is simulated as a short delay proportional to the utterance length, so
the queue, priorities, cancellation, and echo/accuracy flows all work and are
testable without any audio hardware.
"""

from __future__ import annotations

import asyncio

from ws_collab.drivers import TtsDriverSpec


class FakeBackend:
    name = "fake"

    async def play(self, item) -> float:
        duration = min(6.0, max(0.3, len(item.text) * 0.045 / max(0.25, item.rate)))
        await asyncio.sleep(duration)
        return duration


def _build(config) -> FakeBackend:
    return FakeBackend()


def get_driver() -> TtsDriverSpec:
    return TtsDriverSpec(id="fake", build=_build, description="Hardware-free TTS simulator (default).")
