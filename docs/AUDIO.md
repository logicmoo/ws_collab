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
POST /ws_collab/audio/capture/start   {"device_id": "..."}
GET  /ws_collab/audio/capture
POST /ws_collab/audio/capture/stop
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
curl -X POST http://127.0.0.1:8802/ws_collab/audio/utterance \
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

Refresh at startup, on demand (`POST /ws_collab/audio/devices/refresh`), or
after hot-plug; the generation counter increments each time. If the active input
disappears, capture recovers to the default input and emits an event.

### Google Meet role device routing

Meet browser devices are separate from server PortAudio devices and are
reported per controlled HOST/COMPANION tab. A meeting policy stores stable
labels plus the last Chromium device IDs. Sync always re-enumerates and resolves
one exact normalized label; it never silently uses `default`, an ambiguous
label, or a stale ID.

Physical-computer HOST routing accepts only candidates correlated to enumerated
physical hardware. COMPANION routing is stricter: Meet input is the TRANSMIT
cable recording endpoint and Meet output is the RECEIVE cable playback
endpoint. Its meeting-scoped four-endpoint wiring validates paired RECEIVE and
TRANSMIT halves as distinct, starts RECEIVE secondary capture into Silence and
non-Meet STT, and verifies the browser sink, outbound track, capture, and
serialized TTS output before unmuting. A global cable policy remains an
explicit fallback for meetings without an override.

## Routing matrix

Each `(source, engine)` pair routes to one device with its own gain, VAD, noise
reduction, echo cancellation, format/rate/frame size, language hint, and
eligibility flags:

* `command_eligible` — may produce operator commands
* `diagnostic_eligible` — may be used for diagnostics
* `tts_accuracy_eligible` — may be used for loopback accuracy measurement

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/audio/routing \
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

### Always-on browser captioner

`browser_captioner` (`chrome-web-speech`) is a first-class push source, not a
drop-in driver and not run once per `AudioSegment`. A supervised same-origin page
at `/ws_collab/captioner/` exclusively owns microphone recognition, maintains a
durable browser queue, and posts revisioned interim/final results with ACK-based
retry. Superseded interims are bounded; unacknowledged finals are never evicted,
and a full final queue stops recognition until delivery makes room. Web Locks,
with an expiring local-storage lease fallback, ensure
only one tab starts recognition; every page instance has a fresh UUID while queue
sequence allocation remains durable. Finals use a crash-recoverable outbox and
idempotent resolved/heard event keys so finalization completes exactly once.

Manual Pause is durable and prevents watchdog restart until Resume. Enabled
defaults to true, language to `en-US`, and interim delivery to true. Status
reports the tab, recognizer, permission, listening state, heartbeat age, queue,
last acknowledgement, and last final.

The **Chrome Captions** admin page owns these controls, keeps interim text
separate, and rebuilds its finalized transcript from the durable STT stream.
The lease owner first acquires an explicit `getUserMedia` stream, then starts
Chrome recognition. A local AudioWorklet feeds audio-clocked 20ms RMS frames to an
adaptive-noise-floor VAD with hysteresis. It reports every acoustic pause of at
least 20ms, including multiple pauses inside one Chrome final. PCM/RMS frame
samples are never posted or stored; only RMS summaries cross to the page, and only bounded scalar VAD health and validated
pause metadata leave the page. On an explicit listening pause, ownership loss, or shutdown,
the page stops all media tracks, closes its `AudioContext`, and resets the VAD
epoch before reacquiring.
Normal Web Speech silence timeouts, reconnects, and language changes do not stop
the independent microphone detector. "Paused" is reserved for a user-requested
listening pause, not acoustic silence, standby, or a connection failure. Backend
pause settings are authoritative; an old browser-local pause flag cannot override
a resumed driver. Page timers are not used for audio sampling: Chrome can throttle
them to one second in background tabs. Missing audio frames or a suspended audio context invalidate
the silence interval rather than counting unobserved time as measured silence.

The page requests ideal mono input with echo cancellation and noise suppression
enabled and auto gain disabled, then reports the bounded actual track label and
actual `getSettings()` values. Raw `deviceId` is never transmitted. Its input
scope is microphone only: browser-origin isolation prevents direct observation
of other tabs, and RMS cannot identify speaker leakage as human vs website
audio. On Chrome desktop 135+, recognition receives the same live
`getUserMedia` microphone track through `SpeechRecognition.start(audioTrack)`.
If that track is unavailable, the page reports an error rather than starting
recognition on an unrelated default input. Use headphones or separate virtual
audio devices when acoustic isolation matters.
Recognition remains continuous; measured silences do not force the recognizer
to stop or shorten phrases. Text replacement is handled separately from timing.

Only owner-authenticated, ordered `browser_rms_vad` transitions—not PCM or RMS
frames—acquire the `local_microphone` floor. Speech onset blocks new
conversational TTS/backchannels and requests cancellation of cancellable active
output. Speech end starts the configured 350ms hangover; another onset cancels
release. Every queued conversational path checks the same guard again
immediately before playback. A stale lane becomes unavailable/unknown and fails
open rather than locking speech forever. Releasing the floor never enqueues
speech. A reserved, unimplemented `browser_tab_audio` lane is explicitly unable
to acquire this microphone floor.

Small solid blue pause markers use these measured acoustic boundaries; lighter
dashed markers prefixed with `~` are only approximate gaps of at least 300ms
between legacy event timestamps. Chrome supplies no word timestamps, so internal
marker placement uses a captured interim-text prefix and is explicitly
best-effort; the displayed duration remains acoustically measured. **Clear
view** is local and non-destructive; refreshing restores durable history.
Both caption lists end with a live silence duration while the detector is active
and a separate "Last caption ... ago" age. The running silence timer advances
between detector reports and switches to an explicit unavailable/paused state
when detection stops or reports become stale. Caption age keeps updating but is
never presented as measured silence. Timer updates do not rebuild saved captions
or announce every tick to screen readers.
The list also shows a recognizing-speech placeholder immediately on detected
speech, then provisional words as interim results arrive. Measured silences are
inserted inside that evolving text, including between words, and retained when
the matching final replaces it. Interim text is not counted as a committed
caption. Silence durations use the audio clock even when the UI receives a batch
of frames late; text positions remain best-effort because Chrome provides no
word timestamps.
Silence markers are duration-only (`80ms`, `4s`, `1m 08s`), with no pause glyph.
Consecutive markers with no words between them are combined, including across
sentence, interim, and live-counter boundaries. Separate intervals add together;
repeated or overlapping timed intervals count only once. Whitespace is not a
word boundary for this purpose, and merging never changes the stored raw timing.
Silence anchors are captured when acoustic silence starts, rather than moved by
later sentence-completion text. Final-caption persistence consumes only its own
timing snapshot; silences detected during an asynchronous save remain pending
for the following text. The backend retains previously received silence intervals
across interim/final revisions even when a later revision omits them. When words
are rewritten, placement may be marked approximate, but measured duration is kept.
The same interval anchors also survive whole-text replacement in both live views.
For example, reformatting `1 2 3` as `123` retains the two internal silence
boundaries instead of snapping both to the end of the number. Pending finals
reserve only their own intervals while being saved, preventing timing from being
copied into the next utterance.
Detector health uses the monotonic time at which audio frames actually arrive,
not an audio-clock timestamp compared to a page clock. Audio/output timestamps
correlate measured boundaries with real time even if those clocks advance at
different rates. A genuinely stalled processor or suspended audio context reports
that specific reason; disabling Google Meet or other STTs does not disable this
microphone detector.

#### Repeatable caption and silence baselines

`tests\captioner_counting_harness.py` records Windows stock-voice TTS fixtures
and inserts exact PCM-zero intervals: four 2-second gaps, mixed 80/240/1000/2000/
4000ms gaps, a 4-second phrase gap, and a continuous-speech control. Generated
WAVs, source crops, sample indices, hashes, baseline runtime snapshots, and
machine-readable reports are retained in `collab_state\captioner_counting_tests`.

```powershell
.\.venv\Scripts\python.exe tests\captioner_counting_harness.py generate
.\.venv\Scripts\python.exe tests\captioner_counting_harness.py vad
.\.venv\Scripts\python.exe tests\captioner_counting_harness.py compare
.\.venv\Scripts\python.exe tests\captioner_counting_harness.py replay
.\.venv\Scripts\python.exe tests\captioner_counting_harness.py position
```

The optional `native` command runs continuous Chrome recognition and the real
AudioWorklet on the same prerecorded track in an isolated browser context.
It neither captures the user's microphone nor publishes test captions to the
live backend. Run only one native benchmark at a time. Offline `replay` and
`position` reuse the saved Google result arrays without more cloud recognition.

The recorded baseline found all ten inserted TTS gaps. Eight VAD configurations
at two gains show timing/extra-detection tradeoffs; defaults remain unchanged.
Timing retention and word placement are scored separately: the current
frozen-anchor strategy placed 7/10 gaps at the expected boundaries, while the
experimental deferred-initial strategy placed 9/10. Neither result establishes
perfect alignment, and these experiments do not invent word timestamps.

Two CC-BY-4.0 LibriSpeech human recordings and their reference transcripts are
preserved in `collab_state\captioner_recording_baseline`, with source attribution.
Their normalized word error rates were 3/64 and 6/50. Natural silence is unlabeled:
energy-envelope comparisons are proxies, not human silence ground truth.
Derived recordings additionally contain verified zero-sample insertions between
whole utterances; original quiet tails/heads are reported separately.

Toolbar labels and tooltips distinguish **Open captioner** (open/reuse the
dedicated tab), **Show window** (bring it into view, for example for microphone
permission), and **Pause listening** (explicitly stop microphone, recognition,
and silence detection until resumed; history is kept).
The instance list counts recently reporting pages separately from collapsed
earlier/unresponsive records; a new instance ID after a reload does not imply a
new browser window. Browser-tab presence, speech-recognition retries, and local
detector activity are reported separately. Normal `no-speech` / recognizer-end
retries stay at 500-625ms rather than backing off for quiet input; actual failures
retain bounded exponential backoff. A speech-engine retry reuses the existing
browser tab and microphone stream.
Its persistent **Caption source priority** panel stores three strict booleans: prefer it over recent
duplicate Google Meet finals (default true), suppress new typed Meet caption
publication (default false), and bypass other configured audio-segment STTs
(default false). The last policy leaves capture/VAD active, emits an explicit
text-free skipped diagnostic, and takes effect immediately without restart.
Manual external ingest remains available unless it claims a configured engine.

This is the only current WS_COLLAB autostart resource. Chrome Web Speech
requires a live, normal browser page; it cannot run headless or solely in a
service worker/offscreen document. It uses only the dedicated
`chrome_captioner` user-data directory and CDP port
`WS_COLLAB_CAPTIONER_CDP_PORT` (default 9224), never the Meet profile, account
registry, cookies, or Google auth URLs. **Isolated browser profile: no Google
login is used or inherited.** If the configured CDP/profile is already
running, the supervisor opens or reuses exactly one captioner tab in the
background where the browser API permits. If no configured browser exists, it
launches a new visible Chrome window so the first microphone permission can be
granted visibly. Once permission is granted, the tab may stay in the background
or the window may be minimized, subject to Chrome and OS throttling. Periodic
supervision does not foreground the tab; **Foreground** is an explicit operator
action.

On server shutdown the supervisor stops recognition and closes only its captioner
tabs. A page also releases the microphone itself after a bounded heartbeat/auth
outage, while allowing a short server-restart grace period.

**Privacy:** Chrome Web Speech may transmit microphone audio to Google/cloud. It
is not offline/local recognition. The independent RMS pause detector is local,
and WS_COLLAB does not transmit or store its audio samples. This is a clean-room implementation inspired
by concepts in
[`MidCamp/live-captioning` at `893ebc75e9847dbb055963875822cf9b6afb94b8`](https://github.com/MidCamp/live-captioning/tree/893ebc75e9847dbb055963875822cf9b6afb94b8);
no upstream GPL-3.0 code, assets, or styles were copied.

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
POST /ws_collab/stt/ingest
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

Known TTS output is a diagnostic reference. `POST /ws_collab/tts/measure`
speaks a phrase, captures the loopback echo, and correlates expected text,
playback, microphone segment, all engine hypotheses, and the resolved transcript.

Per engine and for the final result it computes WER, CER, word accuracy,
normalized exact match, insertions/deletions/substitutions, missing words,
latency, word-level diffs, and whether the disambiguator improved or regressed
against the best single engine. Rolling accuracy with sample sizes and worst
examples is available at `GET /ws_collab/tts/accuracy`.

Semantic similarity is recorded only as a clearly-labelled secondary metric —
never as the sole measure of accuracy.
