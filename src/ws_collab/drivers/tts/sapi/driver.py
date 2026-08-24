"""Windows SAPI TTS driver (task section 10).

Speaks through the real OS voice selected in the agent's profile. Playback runs
on a worker thread with its own COM apartment, and the requested voice token is
matched by name so an agent never speaks with the wrong voice. If pywin32 or
SAPI is unavailable the driver reports itself unavailable with ``fallback=True``
so the simulated backend is used instead.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time

from ws_collab.drivers import DriverUnavailable, TtsDriverSpec


class SapiBackend:  # pragma: no cover - requires Windows + pywin32
    name = "sapi"

    async def play(self, item) -> float:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._speak_blocking, item)

    def _speak_blocking(self, item) -> float:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        # Each executor thread needs its own COM apartment.
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

        start = time.perf_counter()
        engine = win32com.client.Dispatch("SAPI.SpVoice")

        # Select the requested voice; ids are "sapi:<Name>".
        wanted = (item.voice_id or "").split(":", 1)[-1].strip().lower()
        if wanted:
            for token in engine.GetVoices():
                try:
                    name = str(token.GetAttribute("Name"))
                except Exception:
                    name = str(token.GetDescription())
                description = str(token.GetDescription())
                if name.strip().lower() == wanted or wanted in description.lower():
                    engine.Voice = token
                    break

        engine.Rate = int(max(-10, min(10, round((item.rate - 1.0) * 10))))
        engine.Volume = int(max(0, min(100, round(item.volume * 100))))
        engine.Speak(item.text)
        return time.perf_counter() - start


def _build(config):
    if importlib.util.find_spec("win32com") is None:
        raise DriverUnavailable("pywin32 (win32com) not installed; SAPI TTS unavailable", fallback=True)
    return SapiBackend()


def get_driver() -> TtsDriverSpec:
    return TtsDriverSpec(id="sapi", build=_build, description="Windows SAPI speech output (requires pywin32).")
