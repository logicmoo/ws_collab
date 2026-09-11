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

    processFrame(rms, monotonicMs, wallMs, epoch = this.epoch) {
      if (this.epoch === null || epoch !== this.epoch) return [];
      if (![rms, monotonicMs, wallMs].every(Number.isFinite)) return [];
      if (this.lastMonotonicMs !== null && monotonicMs < this.lastMonotonicMs) return [];
      this.rms = Math.max(0, Math.min(1, rms));
      this.lastMonotonicMs = monotonicMs;
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
    constructor({ maxPauses = MAX_PAUSES } = {}) {
      this.maxPauses = maxPauses;
      this.pending = [];
      this.keys = new Set();
      this.transcript = "";
    }

    reset() {
      this.pending = [];
      this.keys.clear();
      this.transcript = "";
    }

    updateTranscript(text) {
      this.transcript = String(text || "").trim().slice(0, 4000);
    }

    record(pause) {
      const key = `${pause.start_at}|${pause.end_at}`;
      if (this.keys.has(key)) return false;
      this.keys.add(key);
      const prefix = this.transcript;
      this.pending.push({
        ...pause,
        prefix,
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
      const pauses = this.pending.map(({ prefix, ...pause }) => {
        if (pause.alignment === "between_utterances") {
          return { ...pause, after_char: 0 };
        }
        if (prefix && finalText.startsWith(prefix)) {
          return {
            ...pause,
            after_char: Math.min([...prefix].length, [...finalText].length),
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

    commit() {
      this.pending = [];
      this.keys.clear();
      this.transcript = "";
    }
  }

  class BrowserVadCapture {
    constructor({
      mediaDevices,
      AudioContextClass,
      setTimer = setInterval,
      clearTimer = clearInterval,
      monotonicNow = () => performance.now(),
      wallNow = () => Date.now(),
      vad = new BrowserRmsVad(),
      onEvents = () => {},
      onStatus = () => {},
    } = {}) {
      this.mediaDevices = mediaDevices;
      this.AudioContextClass = AudioContextClass;
      this.setTimer = setTimer;
      this.clearTimer = clearTimer;
      this.monotonicNow = monotonicNow;
      this.wallNow = wallNow;
      this.vad = vad;
      this.onEvents = onEvents;
      this.onStatus = onStatus;
      this.stream = null;
      this.context = null;
      this.source = null;
      this.analyser = null;
      this.samples = null;
      this.timer = null;
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
          || !this.AudioContextClass) {
        this.error = "Web Audio microphone capture is unavailable";
        this.onStatus(this.status());
        return false;
      }
      try {
        const stream = await this.mediaDevices.getUserMedia({
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
        const context = new this.AudioContextClass();
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
        const analyser = context.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0;
        source.connect(analyser);
        if (context.state === "suspended" && typeof context.resume === "function") {
          await context.resume();
        }
        this.stream = stream;
        this.context = context;
        this.source = source;
        this.analyser = analyser;
        this.samples = new Float32Array(analyser.fftSize);
        this.permission = "granted";
        this.error = null;
        this.vad.start(epoch);
        this.timer = this.setTimer(() => this.sample(epoch), this.vad.frameIntervalMs);
        this.onStatus(this.status());
        return true;
      } catch (error) {
        this.permission = error && error.name === "NotAllowedError" ? "denied" : "error";
        this.error = String((error && error.message) || error || "microphone capture failed").slice(0, 500);
        await this.stop();
        this.onStatus(this.status());
        return false;
      }
    }

    sample(epoch) {
      if (!this.analyser || this.vad.epoch !== epoch) return;
      this.analyser.getFloatTimeDomainData(this.samples);
      let sum = 0;
      for (let index = 0; index < this.samples.length; index += 1) {
        sum += this.samples[index] * this.samples[index];
      }
      const rms = Math.sqrt(sum / this.samples.length);
      const events = this.vad.processFrame(
        rms,
        this.monotonicNow(),
        this.wallNow(),
        epoch
      );
      if (events.length) this.onEvents(events);
      this.onStatus(this.status());
    }

    async stop() {
      this.generation += 1;
      if (this.timer !== null) this.clearTimer(this.timer);
      this.timer = null;
      if (this.source && typeof this.source.disconnect === "function") {
        try { this.source.disconnect(); } catch (_) {}
      }
      if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
      const context = this.context;
      this.stream = null;
      this.context = null;
      this.source = null;
      this.analyser = null;
      this.samples = null;
      this.vad.reset();
      if (context && typeof context.close === "function") {
        try { await context.close(); } catch (_) {}
      }
      this.onStatus(this.status());
    }

    status() {
      return {
        ...this.vad.status(this.monotonicNow()),
        available: Boolean(this.stream),
        permission: this.permission,
        error: this.error,
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

  return {
    MAX_PAUSES,
    MAX_PAUSE_MS,
    BrowserRmsVad,
    BrowserVadCapture,
    PauseAssociation,
    createIdentity,
    nextFallbackSequence,
    StorageLease,
    classifyRecognitionError,
    pruneQueueRows,
    HeartbeatFailureGuard,
  };
});
