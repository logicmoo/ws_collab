# Audio, transcription, and speech

## Real hardware by default, doubles as a fallback

WS_COLLAB prefers the machine's **real** audio stack and degrades honestly when a
piece is missing:

| Layer | Real backend | Fallback |
| --- | --- | --- |
| Devices | `sounddevice` / PortAudio | Fake device catalog |
| Capture | Live PortAudio stream + VAD | Injected utterances |
| STT | `faster-whisper`, Vosk, NeMo | Deterministic doubles |
| TTS | Windows SAPI | Simulated playback |

`WS_COLLAB_AUDIO_BACKEND` and `WS_COLLAB_TTS_BACKEND` default to `auto`: use real
hardware if it is there, otherwise fall back and say so in
`capabilities.warnings`. Set either to `fake` to force the doubles (which is what
the test suite does, so tests never depend on the machine).

Install the real stack with:

```bash
pip install sounddevice soundfile faster-whisper vosk pywin32   # pywin32: Windows only
```

Everything still runs with none of them installed — the whole pipeline, including
WER/CER accuracy, works on the doubles.

## Always-listening capture

Capture is a real, event-driven server service — never a self-relaunching script
or a shell polling loop. It must be enabled explicitly:

```bash
export WS_COLLAB_AUDIO_ENABLED=1
```

```bash
POST /ws_collab/v1/audio/capture/start   {"device_id": "..."}
GET  /ws_collab/v1/audio/capture
POST /ws_collab/v1/audio/capture/stop
```

With a real device this opens a PortAudio stream. A callback pushes frames onto a
**bounded** queue (the oldest frame is dropped rather than growing memory, and
drops are counted); a worker thread runs energy VAD, keeps a rolling pre-roll
buffer so the start of a word is never clipped, and emits one segment per
utterance on end-of-utterance silence. Audio is downmixed to mono and resampled
to 16 kHz for the recognizers. `live_capture` in the state tells you whether a
real stream is open, alongside the input meter, peak, clipping indicator, and a
privacy indicator.

Inject an utterance instead (used by the admin page and tests):

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/v1/audio/utterance \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "run the two reports", "source_kind": "operator"}'
```

## Devices

Devices are enumerated with **stable identifiers** derived from backend, host
API, name, and direction — never PortAudio's positional indexes, which shift when
hardware appears or disappears. The volatile index is carried separately as
`backend_index` and re-resolved on every refresh.

Inputs, outputs, loopback, and virtual devices are reported with channels,
formats, sample rates, latency, default flags (input/output/communications/
multimedia), and availability. Devices whose names identify them as system
capture (`Stereo Mix`, `What U Hear`, `loopback`, `monitor of`) are classified as
**loopback**, which is what makes TTS-accuracy measurement possible without a
physical microphone.

Refresh at startup, on demand (`POST /ws_collab/v1/audio/devices/refresh`), or
after hot-plug; the generation counter increments each time. If the active input
disappears, capture recovers to the default input and emits an event.

## Routing matrix

Each `(source, engine)` pair routes to one device with its own gain, VAD, noise
reduction, echo cancellation, format/rate/frame size, language hint, and
eligibility flags:

* `command_eligible` — may produce operator commands
* `diagnostic_eligible` — may be used for diagnostics
* `tts_accuracy_eligible` — may be used for loopback accuracy measurement

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/v1/audio/routing \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"source":"microphone","engine":"whisper","device_id":"fake-input-...","gain":1.0}'
```

One device can feed several engines, and different engines can use different
devices. **Microphones are never silently mixed and a fallback is never silently
chosen**: if the routed device is unavailable, resolution returns nothing unless
the route declares `fallback_policy: explicit_device` (which is audited) or
`fail` (which raises). Routes persist atomically and every change is audited.
Source identity is preserved all the way through the disambiguator, so
simultaneous speakers are never merged.

## STT drivers

Engines are drop-in directories under `ws_collab/drivers/stt/`, each containing
`driver.py` (exposing `get_driver()`) and an optional `driver.json`. They are
enumerated at startup.

| Driver | Notes |
| --- | --- |
| `whisper` | Real Whisper via `faster-whisper`; `whisper:small` selects a size. **Default.** |
| `vosk` | Independent local recognizer. Finds a model from `vosk:/path`, `WS_COLLAB_VOSK_MODEL`, or `~/.cache/ws_collab_models/vosk-*` |
| `nemo` | NVIDIA NeMo (Parakeet / Canary / Nemotron); `nemo:<model-or-path>` |
| `remote_http` | Explicitly configured remote provider; `remote:https://host/path` |
| `deterministic` | Hardware-free doubles used as the fallback and by tests |

The default engine set is `whisper:tiny.en, whisper:base.en, vosk` — two Whisper
sizes plus a materially independent Kaldi-based recognizer, so their errors are
uncorrelated and the disambiguator has something real to arbitrate.

Getting a Vosk model:

```bash
mkdir -p ~/.cache/ws_collab_models && cd ~/.cache/ws_collab_models
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
```

The driver discovers it automatically from that directory.

### Disabling or removing a driver

* Rename its directory to end with `_disabled` (e.g. `vosk` → `vosk_disabled`).
* Or set `{"enabled": false}` in its `driver.json`.
* Or delete the directory.

Skips and load failures are reported in `capabilities.warnings` — a broken driver
never prevents startup.

### Choosing three engines

Pick engines from **different families** so their errors are uncorrelated:

```bash
# the shipped default: two Whisper sizes plus an independent Kaldi recognizer
export WS_COLLAB_STT_ENGINES=whisper:tiny.en,whisper:base.en,vosk
# accuracy-first, with a remote third opinion (opt-in)
export WS_COLLAB_STT_ENGINES=whisper:small.en,nemo:nvidia/parakeet-tdt-0.6b-v2,remote:https://asr.example/v1
# no models installed / CI
export WS_COLLAB_STT_ENGINES=fallback_alpha,fallback_beta,fallback_gamma
```

Each engine gets its own timeout (`WS_COLLAB_STT_TIMEOUT_MS`, default 120 s since
a real model may need to load on first use); one failing or timing out never
cancels the others. If an optional library or model is missing, that engine
degrades to a deterministic double **and says so** in `capabilities.warnings`
rather than disappearing silently. Audio is never sent off-device unless
`WS_COLLAB_STT_ALLOW_REMOTE=1`.

The deterministic doubles derive their hypotheses from a segment's known text, so
they cannot decode real captured audio. When handed live PCM they report that
honestly as a per-engine error instead of inventing a transcript — configure at
least one real recognizer for live capture.

### External recognizers

To feed a recognizer that WS_COLLAB does not host (for example a desktop
application's dictation engine), push its results in:

```bash
POST /ws_collab/v1/stt/ingest
{"engine": "external-asr", "text": "...", "confidence": 0.94, "is_final": true}
```

See `examples/clients/copilot_speech_bridge.py`.

## Final disambiguation

After all engines return, one resolved transcript is **appended** — original
hypotheses are never rewritten. The deterministic resolver uses exact majority,
then positional token voting (which can beat every individual engine), then
highest confidence, always recording alternatives, agreement, and uncertainty.
When every engine fails it returns empty rather than inventing text.

An optional LLM resolver (`WS_COLLAB_DISAMBIGUATOR=llm`, plus
`WS_COLLAB_DISAMBIGUATOR_ALLOW_REMOTE=1` and an endpoint) performs transcription
resolution only. Hypotheses and context are passed as untrusted data, only a
small allow-listed context is forwarded, suspected injection in the response is
discarded, and any error falls back to the deterministic resolver. It never
executes commands.

## Source classification and echo

Captured speech is classified as `operator`, `agent`, `system_tts`, `external`,
or `unknown`, using playback overlap, expected TTS text, loopback state,
correlation ids, timing, and source metadata. Confidence and the reasons are
always recorded; certainty is never claimed without evidence.

Speech confidently identified as the system's own TTS is tagged, preserved
diagnostically, and excluded from command execution — this is what prevents a
TTS → STT → TTS feedback loop. Uncertain consequential speech is never executed
automatically.

Policies (`WS_COLLAB_ECHO_POLICY`):

| Policy | Behaviour |
| --- | --- |
| `mute_input_during_tts` | Ignore live input while speaking |
| `listen_and_filter_tts` | Keep listening; tag and filter echo (default) |
| `listen_and_measure_tts_accuracy` | Also score echo against the spoken text |
| `full_duplex_with_echo_cancellation` | Rely on device echo cancellation |

## Speech output and per-agent voices

Voices come from the platform when available (Windows SAPI) and from the fake
catalog otherwise. Each is reported with a stable id, provider, language, gender,
style, formats, rates, locality, availability, latency, and credential/network
requirements. Provider credentials are never stored.

The SAPI backend selects the requested voice token by name and runs playback on a
worker thread with its own COM apartment, so an agent never speaks with another
agent's voice.

Each agent has a persisted profile: engine, voice, output device/channel,
language, rate, volume, pitch/style, speaking permission, queue priority, maximum
utterance length, and fallback policy.

Assignment policies (`WS_COLLAB_TTS_POLICY`): `manual_only`,
`unique_when_possible` (default), `role_based`, `language_based`, `round_robin`,
`shared_default`. Distinct voices are preferred; intentional sharing is allowed
but warned about. A valid assignment is never silently changed.

If a voice is unavailable, the profile's fallback policy applies — `fail`,
`agent_fallback`, `role_default`, `system_default`, or `operator_approval` — and
the originally requested voice is retained in the metadata.

The queue is fair and priority-ordered with per-agent and global pause/mute,
cancellation, interruption, and duplicate suppression. Agent identity and voice
travel with each item, so an agent never speaks with the wrong voice. Previews
are marked as previews and never masquerade as conversation events.

## TTS transcription accuracy

Known TTS output is a diagnostic reference. `POST /ws_collab/v1/tts/measure`
speaks a phrase, captures the loopback echo, and correlates expected text,
playback, microphone segment, all engine hypotheses, and the resolved transcript.

Per engine and for the final result it computes WER, CER, word accuracy,
normalized exact match, insertions/deletions/substitutions, missing words,
latency, word-level diffs, and whether the disambiguator improved or regressed
against the best single engine. Rolling accuracy with sample sizes and worst
examples is available at `GET /ws_collab/v1/tts/accuracy`.

Semantic similarity is recorded only as a clearly-labelled secondary metric —
never as the sole measure of accuracy.
