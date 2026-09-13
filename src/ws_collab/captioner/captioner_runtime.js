"use strict";

((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WsCaptionerRuntime = api;
})(typeof globalThis === "object" ? globalThis : this, () => {
  const sessionKey = "ws-captioner-session";
  const sequenceKey = "ws-captioner-seq";
  const terminalRecognitionErrors = new Set([
    "not-allowed",
    "service-not-allowed",
    "audio-capture",
    "language-not-supported",
    "bad-grammar",
    "phrases-not-supported",
  ]);
  const MAX_PAUSE_MS = 24 * 60 * 60 * 1000;
  const MAX_PAUSES = 128;

  function isoAt(wallMs) {
    return new Date(wallMs).toISOString();
  }

  class BrowserRmsVad {
    constructor({
      frameIntervalMs = 20,
      minPauseMs = 20,
      onsetFrames = 2,
      silenceFrames = 2,
      initialNoiseFloor = 0.003,
      rmsFloor = 0.008,
      thresholdMargin = 0.006,
      thresholdMultiplier = 2.2,
      noiseAlpha = 0.08,
      maxPauseMs = MAX_PAUSE_MS,
    } = {}) {
      this.frameIntervalMs = frameIntervalMs;
      this.minPauseMs = minPauseMs;
      this.onsetFrames = onsetFrames;
      this.silenceFrames = silenceFrames;
      this.initialNoiseFloor = initialNoiseFloor;
      this.rmsFloor = rmsFloor;
      this.thresholdMargin = thresholdMargin;
      this.thresholdMultiplier = thresholdMultiplier;
      this.noiseAlpha = noiseAlpha;
      this.maxPauseMs = maxPauseMs;
      this.reset();
    }

    reset(epoch = null) {
      this.epoch = epoch;
      this.state = "idle";
      this.noiseFloor = this.initialNoiseFloor;
      this.rms = 0;
      this.threshold = this.onThreshold();
      this.lastMonotonicMs = null;
      this.lastSampleClockMs = null;
      this.lastWallMs = null;
      this.hadSpeech = false;
      this.voicedCandidate = null;
      this.silenceCandidate = null;
    }

    start(epoch) {
      this.reset(epoch);
    }

    onThreshold() {
      return Math.max(
        this.rmsFloor,
        this.noiseFloor + this.thresholdMargin,
        this.noiseFloor * this.thresholdMultiplier
      );
    }

    offThreshold() {
      return Math.max(
        this.rmsFloor * 0.7,
        this.noiseFloor + this.thresholdMargin * 0.55,
        this.noiseFloor * 1.55
      );
    }

    processFrame(rms, monotonicMs, wallMs, epoch = this.epoch, sampleClockMs = monotonicMs) {
      if (this.epoch === null || epoch !== this.epoch) return [];
      if (![rms, monotonicMs, wallMs].every(Number.isFinite)) return [];
      if (this.lastMonotonicMs !== null && monotonicMs < this.lastMonotonicMs) return [];
      if (!Number.isFinite(sampleClockMs)) return [];
      if (this.lastSampleClockMs !== null && sampleClockMs < this.lastSampleClockMs) return [];
      if (this.lastSampleClockMs !== null
          && sampleClockMs - this.lastSampleClockMs > Math.max(250, this.frameIntervalMs * 5)) {
        // A suspended/throttled analyser cannot certify silence during its missing frames.
        this.reset(epoch);
      }
      this.rms = Math.max(0, Math.min(1, rms));
      this.lastMonotonicMs = monotonicMs;
      this.lastSampleClockMs = sampleClockMs;
      this.lastWallMs = wallMs;
      const events = [];
      const threshold = this.state === "speech" ? this.offThreshold() : this.onThreshold();
      const voiced = this.rms >= threshold;

      if (this.state !== "speech" && !voiced) {
        this.noiseFloor += (this.rms - this.noiseFloor) * this.noiseAlpha;
        this.noiseFloor = Math.max(0.0001, Math.min(this.rmsFloor * 0.95, this.noiseFloor));
      }
      this.threshold = this.onThreshold();

      if (this.state === "idle") {
        if (voiced) {
          if (!this.voicedCandidate) {
            this.voicedCandidate = { count: 0, monotonicMs, wallMs };
          }
          this.voicedCandidate.count += 1;
          if (this.voicedCandidate.count >= this.onsetFrames) {
            this.state = "speech";
            this.hadSpeech = true;
            events.push({
              type: "speech_start",
              at: isoAt(this.voicedCandidate.wallMs),
              monotonic_ms: this.voicedCandidate.monotonicMs,
            });
            this.voicedCandidate = null;
          }
        } else {
          this.voicedCandidate = null;
        }
        return events;
      }

      if (this.state === "speech") {
        if (!voiced) {
          if (!this.silenceCandidate) {
            this.silenceCandidate = { count: 0, monotonicMs, wallMs };
          }
          this.silenceCandidate.count += 1;
          if (this.silenceCandidate.count === this.silenceFrames) {
            this.state = "silence";
            events.push({
              type: "speech_end",
              at: isoAt(this.silenceCandidate.wallMs),
              monotonic_ms: this.silenceCandidate.monotonicMs,
            });
          }
        } else if (this.silenceCandidate) {
          const pause = this.closePause(monotonicMs, wallMs);
          if (pause) events.push(pause);
        }
        return events;
      }

      if (voiced) {
        if (!this.voicedCandidate) {
          this.voicedCandidate = { count: 0, monotonicMs, wallMs };
        }
        this.voicedCandidate.count += 1;
        if (this.voicedCandidate.count >= this.onsetFrames) {
          const pause = this.closePause(
            this.voicedCandidate.monotonicMs,
            this.voicedCandidate.wallMs
          );
          if (pause) events.push(pause);
          this.state = "speech";
          events.push({
            type: "speech_start",
            at: isoAt(this.voicedCandidate.wallMs),
            monotonic_ms: this.voicedCandidate.monotonicMs,
          });
          this.voicedCandidate = null;
        }
      } else {
        this.voicedCandidate = null;
      }
      return events;
    }

    closePause(endMonotonicMs, endWallMs) {
      const start = this.silenceCandidate;
      this.silenceCandidate = null;
      if (!start || !this.hadSpeech) return null;
      const duration = Math.max(
        Math.round(endMonotonicMs - start.monotonicMs),
        start.count * this.frameIntervalMs
      );
      if (duration < this.minPauseMs || duration > this.maxPauseMs) return null;
      return {
        type: "pause",
        duration_ms: duration,
        start_at: isoAt(start.wallMs),
        end_at: isoAt(endWallMs),
        source: "browser_rms_vad",
      };
    }

    currentSilenceMs(monotonicNow = this.lastMonotonicMs) {
      if (!this.hadSpeech || !this.silenceCandidate || !Number.isFinite(monotonicNow)) {
        return null;
      }
      const elapsed = Math.round(monotonicNow - this.silenceCandidate.monotonicMs);
      return Math.max(0, Math.min(this.maxPauseMs, elapsed));
    }

    status(monotonicNow = this.lastMonotonicMs) {
      return {
        source: "browser_rms_vad",
        available: this.epoch !== null,
        state: this.state,
        rms: Number(this.rms.toFixed(6)),
        noise_floor: Number(this.noiseFloor.toFixed(6)),
        threshold: Number(this.threshold.toFixed(6)),
        current_silence_ms: this.currentSilenceMs(monotonicNow),
        frame_interval_ms: this.frameIntervalMs,
      };
    }
  }

  function nearestTokenBoundary(text, wanted) {
    const characters = [...text];
    const clamped = Math.max(0, Math.min(characters.length, Number(wanted) || 0));
    if (clamped === 0 || clamped === characters.length || /\s/.test(characters[clamped] || "")) {
      return clamped;
    }
    for (let distance = 1; distance <= characters.length; distance += 1) {
      const left = clamped - distance;
      const right = clamped + distance;
      if (left >= 0 && /\s/.test(characters[left] || "")) return left;
      if (right <= characters.length && /\s/.test(characters[right] || "")) return right;
    }
    return clamped;
  }

  class PauseAssociation {
    constructor({ maxPauses = MAX_PAUSES, positionMapper = null } = {}) {
      this.maxPauses = maxPauses;
      this.positionMapper = positionMapper;
      this.pending = [];
      this.keys = new Set();
      this.transcript = "";
      this.openSilence = null;
      this.utteranceId = null;
      this.version = 0;
      this.generation = 0;
    }

    reset() {
      this.pending = [];
      this.keys.clear();
      this.transcript = "";
      this.openSilence = null;
      this.utteranceId = null;
      this.version += 1;
      this.generation += 1;
    }

    updateTranscript(text, utteranceId = this.utteranceId) {
      this.transcript = String(text || "").trim().slice(0, 4000);
      this.utteranceId = utteranceId;
      this.version += 1;
    }

    silenceStarted(at) {
      this.openSilence = { at, prefix: this.transcript, utteranceId: this.utteranceId };
    }

    record(pause) {
      const key = `${pause.start_at}|${pause.end_at}`;
      if (this.keys.has(key)) return false;
      this.keys.add(key);
      const prefix = this.openSilence?.at === pause.start_at
        ? this.openSilence.prefix : this.transcript;
      const utteranceId = this.openSilence?.at === pause.start_at
        ? this.openSilence.utteranceId : this.utteranceId;
      this.openSilence = null;
      this.pending.push({
        duration_ms: pause.duration_ms,
        start_at: pause.start_at,
        end_at: pause.end_at,
        source: pause.source,
        prefix,
        utteranceId,
        after_char: [...prefix].length,
        alignment: prefix ? "interim_prefix" : "between_utterances",
      });
      if (this.pending.length > this.maxPauses) {
        const removed = this.pending.shift();
        this.keys.delete(`${removed.start_at}|${removed.end_at}`);
      }
      return true;
    }

    finalize(text, { consume = true } = {}) {
      const finalText = String(text || "").trim();
      const pauses = this.pending.map(({ prefix, utteranceId: _utteranceId, ...pause }) => {
        if (pause.alignment === "between_utterances") {
          return { ...pause, after_char: 0 };
        }
        if (this.positionMapper) {
          return {
            ...pause, after_char: this.positionMapper(prefix, finalText),
            alignment: finalText.startsWith(prefix) ? pause.alignment : "approximate_text_position",
          };
        }
        if (prefix && finalText.startsWith(prefix)) {
          return {
            ...pause,
            after_char: nearestTokenBoundary(finalText, [...prefix].length),
          };
        }
        return {
          ...pause,
          after_char: nearestTokenBoundary(finalText, pause.after_char),
          alignment: "approximate_text_position",
        };
      });
      const between = pauses.filter((pause) => pause.alignment === "between_utterances");
      if (consume) this.commit();
      return {
        pauses,
        silence_before_ms: between.length ? between[between.length - 1].duration_ms : null,
      };
    }

    snapshot(text, { reserve = false } = {}) {
      const snapshot = {
        metadata: this.finalize(text, { consume: false }),
        entries: new Set(this.pending),
        utteranceId: this.utteranceId,
        version: this.version,
        generation: this.generation,
      };
      if (reserve) this.pending = this.pending.filter(pause => !snapshot.entries.has(pause));
      return snapshot;
    }

    restore(snapshot) {
      if (snapshot.generation !== this.generation) return;
      const existing = new Set(this.pending);
      this.pending = [...snapshot.entries].filter(pause => !existing.has(pause)).concat(this.pending);
    }

    commit(snapshot = null) {
      if (snapshot && snapshot.generation !== this.generation) return;
      const entries = snapshot?.entries || new Set(this.pending);
      for (const pause of entries) this.keys.delete(`${pause.start_at}|${pause.end_at}`);
      this.pending = this.pending.filter(pause => {
        if (entries.has(pause)) {
          this.keys.delete(`${pause.start_at}|${pause.end_at}`);
          return false;
        }
        // A gap detected during the final's async save belongs at the next text boundary.
        if (snapshot && pause.utteranceId === snapshot.utteranceId) {
          pause.prefix = "";
          pause.after_char = 0;
          pause.alignment = "between_utterances";
          pause.utteranceId = null;
        }
        return true;
      });
      if (!snapshot || this.version === snapshot.version) this.updateTranscript("", null);
      if (this.openSilence && (!snapshot || this.openSilence.utteranceId === snapshot.utteranceId)) {
        this.openSilence.prefix = "";
        this.openSilence.utteranceId = null;
      }
    }
  }

  class RmsFrameAccumulator {
    constructor(rate, intervalMs, emit) {
      this.rate = rate;
      this.frameSamples = Math.max(1, Math.round(rate * intervalMs / 1000));
      this.emit = emit;
      this.count = 0;
      this.energy = 0;
    }

    push(samples, firstFrame) {
      for (let index = 0; index < samples.length; index += 1) {
        this.energy += samples[index] * samples[index];
        this.count += 1;
        if (this.count === this.frameSamples) {
          this.emit({
            rms: Math.sqrt(this.energy / this.count),
            audio_time_ms: (firstFrame + index + 1) * 1000 / this.rate,
          });
          this.count = 0;
          this.energy = 0;
        }
      }
    }
  }

  // This same served file is also loaded inside AudioWorklet; no page timers or PCM messages.
  if (typeof registerProcessor === "function") {
    class MicrophoneRmsProcessor extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.frames = new RmsFrameAccumulator(
          sampleRate, options.processorOptions.frameIntervalMs,
          (frame) => this.port.postMessage(frame)
        );
      }

      process(inputs) {
        const samples = inputs[0]?.[0];
        if (samples) this.frames.push(samples, currentFrame);
        return true;
      }
    }
    registerProcessor("ws-collab-microphone-rms", MicrophoneRmsProcessor);
  }

  class BrowserVadCapture {
    constructor({
      mediaDevices,
      AudioContextClass,
      AudioWorkletNodeClass = globalThis.AudioWorkletNode,
      workletUrl = "captioner_runtime.js",
      monotonicNow = () => performance.now(),
      wallNow = () => Date.now(),
      vad = new BrowserRmsVad(),
      onEvents = () => {},
      onStatus = () => {},
    } = {}) {
      this.mediaDevices = mediaDevices;
      this.AudioContextClass = AudioContextClass;
      this.AudioWorkletNodeClass = AudioWorkletNodeClass;
      this.workletUrl = workletUrl;
      this.monotonicNow = monotonicNow;
      this.wallNow = wallNow;
      this.vad = vad;
      this.onEvents = onEvents;
      this.onStatus = onStatus;
      this.stream = null;
      this.context = null;
      this.source = null;
      this.processor = null;
      this.output = null;
      this.lastSampleMs = null;
      this.lastFrameReceivedMs = null;
      this.clockOrigin = null;
      this.generation = 0;
      this.permission = "unknown";
      this.error = null;
      this.input = {
        input_scope: "microphone",
        track_label: "",
        device_fingerprint: null,
        echo_cancellation: null,
        noise_suppression: null,
        auto_gain_control: null,
        channel_count: null,
        sample_rate: null,
      };
    }

    async start(epoch) {
      if (this.stream) return true;
      const generation = ++this.generation;
      if (!this.mediaDevices || typeof this.mediaDevices.getUserMedia !== "function"
          || !this.AudioContextClass || !this.AudioWorkletNodeClass) {
        this.error = "AudioWorklet microphone capture is unavailable";
        this.onStatus(this.status());
        return false;
      }
      let stream = null;
      let context = null;
      try {
        stream = await this.mediaDevices.getUserMedia({
          audio: {
            channelCount: { ideal: 1 },
            echoCancellation: { ideal: true },
            noiseSuppression: { ideal: true },
            autoGainControl: { ideal: false },
          },
        });
        if (generation !== this.generation) {
          stream.getTracks().forEach((track) => track.stop());
          return false;
        }
        context = new this.AudioContextClass();
        if (!context.audioWorklet) throw new Error("AudioWorklet is required for background silence detection");
        await context.audioWorklet.addModule(this.workletUrl);
        if (generation !== this.generation) {
          stream.getTracks().forEach((track) => track.stop());
          await context.close();
          return false;
        }
        const track = typeof stream.getAudioTracks === "function"
          ? stream.getAudioTracks()[0]
          : stream.getTracks()[0];
        const settings = track && typeof track.getSettings === "function"
          ? track.getSettings()
          : {};
        const scalar = (value) => Number.isInteger(value) ? value : null;
        const actualBoolean = (value) => typeof value === "boolean" ? value : null;
        this.input = {
          input_scope: "microphone",
          track_label: String((track && track.label) || "").slice(0, 160),
          // Raw deviceId is intentionally not transmitted.
          device_fingerprint: null,
          echo_cancellation: actualBoolean(settings.echoCancellation),
          noise_suppression: actualBoolean(settings.noiseSuppression),
          auto_gain_control: actualBoolean(settings.autoGainControl),
          channel_count: scalar(settings.channelCount),
          sample_rate: scalar(settings.sampleRate),
        };
        const source = context.createMediaStreamSource(stream);
        const processor = new this.AudioWorkletNodeClass(context, "ws-collab-microphone-rms", {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
          channelCount: 1, channelCountMode: "explicit",
          processorOptions: { frameIntervalMs: this.vad.frameIntervalMs },
        });
        const output = context.createGain();
        output.gain.value = 0;
        source.connect(processor);
        processor.connect(output);
        output.connect(context.destination);
        if (context.state === "suspended" && typeof context.resume === "function") {
          await context.resume();
        }
        if (generation !== this.generation) {
          source.disconnect();
          stream.getTracks().forEach((track) => track.stop());
          await context.close();
          return false;
        }
        this.stream = stream;
        this.context = context;
        this.source = source;
        this.processor = processor;
        this.output = output;
        this.permission = "granted";
        this.error = null;
        this.vad.start(epoch);
        this.resetClock();
        processor.port.onmessage = ({ data }) => {
          if (generation === this.generation) this.sampleFrame(data, epoch);
        };
        processor.onprocessorerror = () => {
          if (generation !== this.generation) return;
          this.error = "Microphone audio processor failed";
          void this.stop();
        };
        context.onstatechange = () => {
          if (generation !== this.generation) return;
          this.vad.reset(epoch);
          this.resetClock();
          this.onStatus(this.status());
        };
        this.onStatus(this.status());
        return true;
      } catch (error) {
        if (stream && stream !== this.stream) stream.getTracks().forEach((track) => track.stop());
        if (context && context !== this.context && context.state !== "closed") await context.close();
        this.permission = error && error.name === "NotAllowedError" ? "denied" : "error";
        this.error = String((error && error.message) || error || "microphone capture failed").slice(0, 500);
        await this.stop();
        this.onStatus(this.status());
        return false;
      }
    }

    resetClock() {
      const audioMs = this.context.currentTime * 1000;
      this.clockOrigin = { audioMs, monotonicMs: this.monotonicNow(), wallMs: this.wallNow() };
      this.lastSampleMs = null;
      this.lastFrameReceivedMs = null;
    }

    sampleFrame(frame, epoch) {
      if (!this.processor || this.vad.epoch !== epoch || this.context.state !== "running") return;
      if (!frame || !Number.isFinite(frame.rms) || !Number.isFinite(frame.audio_time_ms)) return;
      const offset = frame.audio_time_ms - this.clockOrigin.audioMs;
      if (offset < 0) return;
      const receivedAt = this.monotonicNow();
      const output = typeof this.context.getOutputTimestamp === "function"
        ? this.context.getOutputTimestamp() : null;
      let sampleAt = this.clockOrigin.monotonicMs + offset;
      if (output && Number.isFinite(output.contextTime) && output.contextTime >= 0
          && Number.isFinite(output.performanceTime) && output.performanceTime > 0) {
        // The audio clock can advance at a different rate from performance.now().
        sampleAt = output.performanceTime + frame.audio_time_ms - output.contextTime * 1000;
      }
      this.lastSampleMs = Math.max(this.lastSampleMs ?? -Infinity, Math.min(receivedAt, sampleAt));
      this.lastFrameReceivedMs = receivedAt;
      const events = this.vad.processFrame(
        frame.rms,
        this.lastSampleMs,
        this.wallNow() + this.lastSampleMs - receivedAt,
        epoch,
        frame.audio_time_ms
      );
      if (events.length) this.onEvents(events);
      this.onStatus(this.status());
    }

    async stop() {
      this.generation += 1;
      if (this.processor) {
        this.processor.port.onmessage = null;
        this.processor.onprocessorerror = null;
        this.processor.port.close();
        this.processor.disconnect();
      }
      if (this.output) this.output.disconnect();
      if (this.source && typeof this.source.disconnect === "function") {
        try { this.source.disconnect(); } catch (_) {}
      }
      if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
      const context = this.context;
      if (context) context.onstatechange = null;
      this.stream = null;
      this.context = null;
      this.source = null;
      this.processor = null;
      this.output = null;
      this.lastSampleMs = null;
      this.lastFrameReceivedMs = null;
      this.clockOrigin = null;
      this.vad.reset();
      if (context && typeof context.close === "function") {
        try { await context.close(); } catch (_) {}
      }
      this.onStatus(this.status());
    }

    status() {
      const now = this.monotonicNow();
      const receiving = this.lastFrameReceivedMs !== null && now - this.lastFrameReceivedMs <= 1000;
      const running = this.stream && this.context?.state === "running";
      let error = this.error;
      if (!error && this.stream) {
        if (!running) error = `Microphone audio context is ${this.context?.state || "unavailable"}`;
        else if (this.lastFrameReceivedMs === null) error = "Waiting for microphone audio frames";
        else if (!receiving) error = `No microphone audio frames received for ${Math.round(now - this.lastFrameReceivedMs)}ms`;
      }
      return {
        ...this.vad.status(now),
        available: Boolean(running && receiving),
        permission: this.permission,
        error,
        input: { ...this.input },
      };
    }
  }

  function createIdentity(storage, cryptoApi) {
    let sessionId = storage.getItem(sessionKey);
    if (!sessionId) {
      sessionId = cryptoApi.randomUUID();
      storage.setItem(sessionKey, sessionId);
    }
    const instanceId = cryptoApi.randomUUID();
    return {
      sessionId,
      instanceId,
      utteranceId(epoch, index) {
        return `${sessionId}:${instanceId}:${epoch}:${index}`;
      },
    };
  }

  function nextFallbackSequence(storage) {
    const current = Number(storage.getItem(sequenceKey) || "0");
    const next = Number.isSafeInteger(current) && current >= 0 ? current + 1 : 1;
    storage.setItem(sequenceKey, String(next));
    return next;
  }

  class StorageLease {
    constructor(storage, {
      key = "ws-captioner-recognition-lease",
      ownerId,
      now = () => Date.now(),
      ttlMs = 8000,
    }) {
      this.storage = storage;
      this.key = key;
      this.ownerId = ownerId;
      this.now = now;
      this.ttlMs = ttlMs;
    }

    read() {
      try {
        const value = JSON.parse(this.storage.getItem(this.key) || "null");
        return value && typeof value.ownerId === "string" ? value : null;
      } catch (_) {
        return null;
      }
    }

    isOwner() {
      const lease = this.read();
      return Boolean(
        lease
        && lease.ownerId === this.ownerId
        && Number(lease.expiresAt) > this.now()
      );
    }

    tryAcquire() {
      const lease = this.read();
      if (lease && lease.ownerId !== this.ownerId && Number(lease.expiresAt) > this.now()) {
        return false;
      }
      const candidate = {
        ownerId: this.ownerId,
        nonce: `${this.ownerId}:${this.now()}`,
        expiresAt: this.now() + this.ttlMs,
      };
      this.storage.setItem(this.key, JSON.stringify(candidate));
      const confirmed = this.read();
      return Boolean(confirmed && confirmed.nonce === candidate.nonce);
    }

    renew() {
      if (!this.isOwner()) return false;
      this.storage.setItem(this.key, JSON.stringify({
        ownerId: this.ownerId,
        nonce: `${this.ownerId}:${this.now()}`,
        expiresAt: this.now() + this.ttlMs,
      }));
      return this.isOwner();
    }

    release() {
      if (this.isOwner()) this.storage.removeItem(this.key);
    }
  }

  function classifyRecognitionError(error) {
    const value = String(error || "recognizer error");
    return terminalRecognitionErrors.has(value) ? "terminal" : "transient";
  }

  function pruneQueueRows(rows, maxSize) {
    const ordered = [...rows].sort(
      (a, b) => Number(a.seq) - Number(b.seq) || String(a.id).localeCompare(String(b.id))
    );
    const latestRevision = new Map();
    const finalized = new Set();
    for (const row of ordered) {
      const key = String(row.utterance_id || "");
      latestRevision.set(key, Math.max(latestRevision.get(key) || -1, Number(row.revision) || 0));
      if (row.is_final) finalized.add(key);
    }
    const superseded = ordered.filter((row) => (
      !row.is_final
      && (
        finalized.has(String(row.utterance_id || ""))
        || Number(row.revision) < latestRevision.get(String(row.utterance_id || ""))
      )
    ));
    const remove = new Set(superseded.map((row) => row.id));
    let kept = ordered.filter((row) => !remove.has(row.id));
    if (kept.length > maxSize) {
      for (const row of kept) {
        if (kept.length <= maxSize) break;
        if (!row.is_final) {
          remove.add(row.id);
          kept = kept.filter((candidate) => candidate.id !== row.id);
        }
      }
    }
    return {
      kept,
      removedIds: [...remove],
      fullOfFinals: kept.length >= maxSize && kept.every((row) => Boolean(row.is_final)),
    };
  }

  class HeartbeatFailureGuard {
    constructor({ now = () => Date.now(), graceMs = 15000 } = {}) {
      this.now = now;
      this.graceMs = graceMs;
      this.failedAt = null;
    }
    success() { this.failedAt = null; }
    failure() {
      if (this.failedAt === null) this.failedAt = this.now();
      return this.expired();
    }
    expired() {
      return this.failedAt !== null && this.now() - this.failedAt >= this.graceMs;
    }
  }

  function captionerState({
    paused, enabled, supported, terminal, terminalState, lastError,
    authorized, authorityReason, queueBlocked, owned, starting, restarting,
    speaking, listening, micPermission,
  }) {
    if (paused) return { state: "paused", label: "Paused", detail: "Listening paused by the user. Use Resume listening to continue." };
    if (!enabled) return { state: "standby", label: "Disabled", detail: "Chrome Captions is disabled in the source controls." };
    if (!supported) return { state: "unsupported", label: "Unsupported", detail: "Chrome Web Speech API is unavailable." };
    if (micPermission === "denied") return { state: "permission_denied", label: "Permission denied", detail: "Allow microphone access in Chrome, then use Resume listening." };
    if (terminal) return { state: terminalState, label: "Error", detail: lastError || "Recognition is unavailable." };
    if (!authorized) return {
      state: "standby", label: "Standby",
      detail: `Waiting for backend selection: ${authorityReason || "selection unavailable"}.`,
    };
    if (queueBlocked) return { state: "error", label: "Delivery blocked", detail: "Caption delivery queue is full; waiting for acknowledgements." };
    if (!owned) return { state: "standby", label: "Standby", detail: "Waiting for this window's microphone ownership." };
    if (starting || restarting) return { state: "restarting", label: "Reconnecting", detail: "Reconnecting speech recognition; local detection continues if the microphone is available." };
    if (speaking) return { state: "speaking", label: "Speaking", detail: "" };
    if (listening) return { state: "listening", label: "Listening", detail: "" };
    return { state: "idle", label: "Starting", detail: lastError || "Starting the microphone and speech recognition." };
  }

  function recognitionRestartDelay(reason, attempt, random = Math.random) {
    const normalEnd = reason === "no-speech" || reason === "recognizer ended";
    const base = normalEnd ? 500 : Math.min(30000, 500 * (2 ** Math.min(attempt - 1, 6)));
    return base + Math.floor(random() * Math.min(3000, base * 0.25));
  }

  function recognitionUpdates(event) {
    const updates = [];
    const interim = [];
    let interimIndex = null;
    for (let index = 0; index < event.results.length; index += 1) {
      const result = event.results[index];
      const text = String(result[0]?.transcript || "").trim();
      if (result.isFinal) {
        if (index >= event.resultIndex && text) {
          updates.push({ index, text, isFinal: true, confidence: result[0]?.confidence });
        }
      } else {
        if (interimIndex === null) interimIndex = index;
        if (text) interim.push(text);
      }
    }
    if (interimIndex !== null && interim.length) {
      updates.push({ index: interimIndex, text: interim.join(" "), isFinal: false, confidence: null });
    }
    return updates;
  }

  return {
    MAX_PAUSES,
    MAX_PAUSE_MS,
    BrowserRmsVad,
    BrowserVadCapture,
    RmsFrameAccumulator,
    PauseAssociation,
    createIdentity,
    nextFallbackSequence,
    StorageLease,
    classifyRecognitionError,
    pruneQueueRows,
    HeartbeatFailureGuard,
    captionerState,
    recognitionRestartDelay,
    recognitionUpdates,
  };
});
