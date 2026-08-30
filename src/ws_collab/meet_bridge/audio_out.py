"""Audio output for the ``/say`` command: Windows SAPI TTS + device playback.

Ported from the original bridge design. Two independent things live here:

* ``speak_windows`` / ``sapi_wav_base64`` shell out to PowerShell's
  ``System.Speech.Synthesis`` -- no COM/pywin32 dependency, just a subprocess
  call, so this needs nothing beyond the standard library.
* ``list_audio_devices`` / ``resolve_audio_device`` / ``play_wav_bytes_to_device``
  are the *optional* virtual-cable playback path (an alternative to the
  in-page WebAudio synthetic mic): they need ``sounddevice``/``numpy``
  (already ws_collab's existing ``audio`` extra) and, only when the source and
  device sample rates disagree, ``scipy`` (new -- part of the ``meet`` extra).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable


class AudioPlaybackCancelled(RuntimeError):
    """Raised when cancellable physical-device playback is interrupted."""


def speak_windows(text: str) -> None:
    """Best-effort local TTS through Windows SAPI (no extra pip deps)."""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Speak([Console]::In.ReadToEnd())"
    )
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.communicate(input=text.encode("utf-8"), timeout=120)
    except Exception as error:  # noqa: BLE001 - TTS must never kill the bridge
        print(f"[tts] {error}", file=sys.stderr, flush=True)


def sapi_wav_base64(
    text: str,
    *,
    voice_id: str = "",
    rate: float = 1.0,
    volume: float = 1.0,
    pitch: float = 0.0,
) -> tuple[str, float]:
    """Synthesize text to a WAV with Windows SAPI; return (base64, seconds)."""
    import base64
    import wave

    path = str(Path.cwd() / f".meet_say_{uuid.uuid4().hex}.wav")
    try:
        wanted_voice = (voice_id or "").split(":", 1)[-1].strip() if voice_id.lower().startswith("sapi:") else ""
        sapi_rate = int(max(-10, min(10, round((float(rate) - 1.0) * 10))))
        sapi_volume = int(max(0, min(100, round(float(volume) * 100))))
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.Rate={sapi_rate};$s.Volume={sapi_volume};"
            + (f"try{{$s.SelectVoice({wanted_voice!r})}}catch{{}};" if wanted_voice else "")
            +
            f"$s.SetOutputToWaveFile('{path}');"
            "$s.Speak([Console]::In.ReadToEnd());"
            "$s.Dispose()"
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.communicate(input=text.encode("utf-8"), timeout=120)
        with wave.open(path, "rb") as reader:
            duration = reader.getnframes() / float(reader.getframerate() or 22050)
        data = Path(path).read_bytes()
        return base64.b64encode(data).decode("ascii"), duration
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def list_audio_devices() -> None:
    """Print every Windows audio device sounddevice can see (index, name,
    in/out channel counts) -- use this to find a virtual cable's exact name
    once one is installed."""
    import sounddevice as sd

    for index, entry in enumerate(sd.query_devices()):
        kind = []
        if entry.get("max_input_channels", 0) > 0:
            kind.append("in")
        if entry.get("max_output_channels", 0) > 0:
            kind.append("out")
        print(f"[{index:3}] {entry.get('name', '?')!r}  ({'/'.join(kind) or 'none'}, "
              f"in={entry.get('max_input_channels', 0)} out={entry.get('max_output_channels', 0)})", flush=True)


def resolve_audio_device(name_substring: str, *, want: str) -> int:
    """Resolve a device spec -- either a literal device index (e.g. "17") or
    a case-insensitive name substring -- to a device index.

    ``want`` is "output" (playback, e.g. a cable's "Input" side we play TTS
    to) or "input" (recording, e.g. a cable's "Output" side). Fails loudly
    (never guesses) on zero or multiple name matches, listing candidates
    either way. A virtual cable commonly registers the SAME name multiple
    times (once per Windows host API: MME/DirectSound/WASAPI/WDM-KS) with
    different channel counts/sample rates -- pass the exact index from
    --list-audio-devices to disambiguate when a name substring is not unique
    enough.
    """
    import sounddevice as sd

    channel_key = "max_output_channels" if want == "output" else "max_input_channels"
    spec = name_substring.strip()
    if spec.isdigit():
        index = int(spec)
        devices = sd.query_devices()
        if not (0 <= index < len(devices)):
            raise ValueError(f"device index {index} out of range (0..{len(devices) - 1})")
        if devices[index].get(channel_key, 0) <= 0:
            raise ValueError(f"device #{index} {devices[index].get('name')!r} has no {want} channels")
        return index
    needle = spec.lower()
    matches = [
        (index, entry) for index, entry in enumerate(sd.query_devices())
        if needle in str(entry.get("name", "")).lower() and entry.get(channel_key, 0) > 0
    ]
    if not matches:
        raise ValueError(
            f"no {want} device matches {name_substring!r} -- run --list-audio-devices to see what's available"
        )
    if len(matches) > 1:
        candidates = ", ".join(f"{i}:{e.get('name')!r}" for i, e in matches)
        raise ValueError(
            f"{name_substring!r} matches {len(matches)} {want} devices ({candidates}); "
            "pass the exact index instead (a virtual cable often registers once per host API)"
        )
    return matches[0][0]


def play_wav_bytes_to_device(
    wav_bytes: bytes,
    device_index: int,
    cancellation: object | Callable[[], bool] | None = None,
) -> float:
    """Play a synthesized WAV out to a specific device (e.g. a cable's
    playback side) instead of decoding it into the in-page WebAudio patch.
    Returns the clip duration in seconds. Blocks until playback finishes.

    Two real bugs were found and fixed here via live testing against an
    actual VB-CABLE device, not just code review -- preserved verbatim since
    they are not obvious from the docs and are easy to reintroduce:

    1. Resamples to the device's own native rate before playing: MME (and
       some other Windows host APIs) do NOT resample on the fly the way
       WASAPI shared mode does -- feeding a mismatched sample rate (e.g.
       SAPI's 22050 Hz WAV output into a 44100 Hz-native cable device)
       silently produces a single click/pop instead of the real audio,
       with no error raised.
    2. Streams in fixed-size chunks via `sd.OutputStream` rather than
       handing the whole clip to `sd.play()` in one call: MME has a hard
       internal single-buffer limit around 65536 samples (~1.4s at
       44100 Hz) -- a `sd.play()` call for anything longer than that
       silently plays NOTHING (not even a click), also with no error
       raised. Empirically bisected the exact threshold live (1.3s clips
       played fine, 1.5s+ clips were totally silent) before finding the
       fix; chunked streaming has no such limit regardless of clip length.
    """
    import io
    import wave

    import numpy as np
    import sounddevice as sd

    def cancelled() -> bool:
        if cancellation is None:
            return False
        if callable(cancellation):
            return bool(cancellation())
        is_set = getattr(cancellation, "is_set", None)
        return bool(is_set()) if callable(is_set) else False

    if cancelled():
        raise AudioPlaybackCancelled("physical audio playback cancelled")

    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        frames = reader.readframes(reader.getnframes())
        sample_rate = reader.getframerate()
        sample_width = reader.getsampwidth()
        channels = reader.getnchannels()
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sample_width, np.int16)
    raw = np.frombuffer(frames, dtype=dtype)
    if channels > 1:
        raw = raw.reshape(-1, channels)
    duration = len(raw) / float(sample_rate or 1)
    # Normalize to float32 in [-1, 1] regardless of source bit depth.
    max_val = {1: 128.0, 2: 32768.0, 4: 2147483648.0}.get(sample_width, 32768.0)
    offset = 128.0 if sample_width == 1 else 0.0
    samples = ((raw.astype(np.float32) - offset) / max_val).astype(np.float32)

    device_info = sd.query_devices(device_index)
    target_rate = int(device_info.get("default_samplerate") or sample_rate)
    if target_rate != sample_rate:
        from scipy.signal import resample

        new_length = max(1, int(round(len(samples) * target_rate / sample_rate)))
        samples = resample(samples, new_length, axis=0).astype(np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)

    blocksize = 4096
    frame_count = len(samples)
    out_channels = samples.shape[1]
    with sd.OutputStream(samplerate=target_rate, device=device_index, channels=out_channels, dtype="float32", blocksize=blocksize) as stream:
        index = 0
        while index < frame_count:
            if cancelled():
                abort = getattr(stream, "abort", None)
                if callable(abort):
                    abort()
                raise AudioPlaybackCancelled("physical audio playback cancelled")
            chunk = samples[index:index + blocksize]
            if len(chunk) < blocksize:
                pad = np.zeros((blocksize - len(chunk), out_channels), dtype="float32")
                chunk = np.vstack([chunk, pad])
            stream.write(chunk)
            index += blocksize
        if cancelled():
            abort = getattr(stream, "abort", None)
            if callable(abort):
                abort()
            raise AudioPlaybackCancelled("physical audio playback cancelled")
    return duration
