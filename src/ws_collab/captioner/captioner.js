"use strict";

(() => {
  const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
  const token = meta("ws-captioner-token");
  const bootId = meta("ws-captioner-boot");
  const endpoint = (name) => new URL(name, window.location.href).toString();
  const $ = (id) => document.getElementById(id);
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const Runtime = window.WsCaptionerRuntime;
  const Transcript = window.WsCollabTranscript;
  if (!Runtime) throw new Error("Captioner runtime failed to load");
  if (!Transcript) throw new Error("Transcript runtime failed to load");
  const maxQueue = 500;
  const maxBatch = 25;

  let config = {
    enabled: meta("ws-captioner-enabled") === "true",
    paused: meta("ws-captioner-paused") === "true",
    language: meta("ws-captioner-language"),
    send_interims: meta("ws-captioner-send-interims") === "true",
  };
  const identity = Runtime.createIdentity(localStorage, crypto);
  const sessionId = identity.sessionId;
  const instanceId = identity.instanceId;
  let recognitionEpoch = 0;
  let recognizer = null;
  let starting = false;
  let listening = false;
  let speaking = false;
  let stopping = false;
  let terminal = false;
  let restartTimer = null;
  let restartAttempt = 0;
  let deliveryTimer = null;
  let delivering = false;
  let lastError = "";
  let lastResultAt = "";
  let lastAckAt = "";
  let micPermission = "unknown";
  let hasOwnership = false;
  let backendAuthorized = false;
  let backendAuthorityReason = "awaiting backend selection";
  let ownershipRetryTimer = null;
  let queueBlocked = false;
  let microphonePermissionStatus = null;
  let terminalState = "permission_denied";
  let vadFailureLatched = false;
  let vadOwnerToken = null;
  let vadTransitionSeq = 0;
  let vadTransitionChain = Promise.resolve();
  let vadTransitionError = "";
  let vadSpeechStartedAt = null;
  let vadSpeechEndedAt = null;
  let vadBoundaryVersion = 0;
  let recognizerSpeakingDiagnostic = false;
  let vadStatus = {
    source: "browser_rms_vad",
    available: false,
    state: "unavailable",
    rms: 0,
    noise_floor: 0,
    threshold: 0,
    current_silence_ms: null,
    frame_interval_ms: 20,
    permission: "unknown",
    error: "pause detector has not started",
  };
  const utterances = new Map();
  const finalized = new Set();
  const finalizing = new Set();
  const pauseAssociator = new Runtime.PauseAssociation({ positionMapper: Transcript.silencePosition });
  const vadCapture = new Runtime.BrowserVadCapture({
    mediaDevices: navigator.mediaDevices,
    AudioContextClass: window.AudioContext || window.webkitAudioContext,
    onEvents: handleVadEvents,
    onStatus: updateVadStatus,
  });
  let transcriptRows = [];
  const transcriptTail = new Transcript.LiveTranscriptTail(document, $("final-log"));
  let transcriptTimer = null;
  const heartbeatFailures = new Runtime.HeartbeatFailureGuard({ graceMs: 15000 });

  function nowIso() { return new Date().toISOString(); }
  function setState(state, detail = "") {
    $("status").textContent = detail ? `${state}: ${detail}` : state;
    $("recording-label").textContent = state;
    $("recording-indicator").className = `indicator ${state.toLowerCase()}`;
    $("error").textContent = detail;
  }
  function updateVadStatus(status) {
    vadStatus = {
      ...status,
      state: status.available ? status.state : "unavailable",
    };
    transcriptTail.observe(vadStatus, { maxAgeMs: 1000 });
    const state = vadStatus.available ? vadStatus.state : "pause detector unavailable";
    const rms = Number(vadStatus.rms || 0).toFixed(4);
    const threshold = Number(vadStatus.threshold || 0).toFixed(4);
    const silence = vadStatus.current_silence_ms == null
      ? "—" : Transcript.formatDuration(vadStatus.current_silence_ms);
    $("vad-status").textContent =
      `Local silence detector: ${state} · RMS ${rms} · threshold ${threshold} · silence ${silence}`;
    const input = status.input || {};
    $("input-scope").textContent = input.input_scope || "microphone";
    $("input-device").textContent = input.track_label || "Chrome/OS default input";
    $("input-processing").textContent = [
      `echo ${formatActual(input.echo_cancellation)}`,
      `noise suppression ${formatActual(input.noise_suppression)}`,
      `auto gain ${formatActual(input.auto_gain_control)}`,
      `channels ${input.channel_count ?? "unknown"}`,
      `sample rate ${input.sample_rate ? `${input.sample_rate} Hz` : "unknown"}`,
    ].join(" · ");
  }
  function formatActual(value) {
    return value === true ? "on" : value === false ? "off" : "unknown";
  }
  function queueVadTransition(event, state, at = nowIso()) {
    if (!vadOwnerToken || !hasOwnership) return;
    const payload = {
      source: "browser_rms_vad",
      source_id: "local_microphone",
      input_scope: "microphone",
      session_id: sessionId,
      instance_id: instanceId,
      owner_token: vadOwnerToken,
      epoch: Math.max(1, recognitionEpoch),
      seq: ++vadTransitionSeq,
      event,
      state,
      at,
    };
    vadTransitionChain = vadTransitionChain
      .then(() => authenticatedFetch("vad-transition", payload))
      .then(() => { vadTransitionError = ""; })
      .catch((error) => {
        vadTransitionError = String(error && (error.message || error)).slice(0, 200);
      });
  }
  function handleVadEvents(events) {
    for (const event of events) {
      if (event.type === "speech_start") {
        vadBoundaryVersion += 1;
        if (!vadSpeechStartedAt) vadSpeechStartedAt = event.at;
        vadSpeechEndedAt = null;
        speaking = true;
        if (listening) setState("Speaking");
        queueVadTransition("speech_start", "speech", event.at);
      } else if (event.type === "speech_end") {
        vadBoundaryVersion += 1;
        vadSpeechEndedAt = event.at;
        pauseAssociator.silenceStarted(event.at);
        speaking = false;
        if (listening) setState("Listening");
        queueVadTransition("speech_end", "silence", event.at);
      } else if (event.type === "pause") {
        pauseAssociator.record(event);
        const interim = transcriptTail.interim;
        if (interim) {
          transcriptTail.setInterim({
            ...interim.event,
            type: "STT_PARTIAL_RESULT",
            data: {
              ...interim.event.data, is_final: false,
              ...pauseAssociator.finalize(interim.text, { consume: false }),
            },
          });
        }
      }
    }
  }
  function updateControls() {
    $("language").value = config.language;
    $("send-interims").checked = config.send_interims;
    $("pause").disabled = config.paused || !config.enabled;
    $("resume").disabled = !config.paused && listening && !terminal;
    $("mic").textContent = micPermission;
  }
  function currentState() {
    return driverStatus().state;
  }
  function driverStatus() {
    return Runtime.captionerState({
      paused: config.paused, enabled: config.enabled, supported: Boolean(Recognition),
      terminal, terminalState, lastError, authorized: backendAuthorized,
      authorityReason: backendAuthorityReason, queueBlocked, owned: hasOwnership,
      starting, restarting: Boolean(restartTimer), speaking, listening, micPermission,
    });
  }
  function showDriverStatus() {
    const status = driverStatus();
    setState(status.label, status.detail);
  }
  function shouldDetect() {
    return config.enabled && !config.paused && backendAuthorized && hasOwnership
      && micPermission !== "denied" && !unloading;
  }
  function updateTranscriptTail() {
    if (!shouldDetect()) {
      const status = driverStatus();
      transcriptTail.observe(null, { reason: `${status.label}: ${status.detail}` });
    }
    const log = $("final-log");
    const follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    transcriptTail.update(transcriptRows.at(-1));
    if (follow) log.scrollTop = log.scrollHeight;
  }
  function shouldRun() {
    return Boolean(
      Recognition && config.enabled && !config.paused && !terminal && !queueBlocked
      && backendAuthorized && hasOwnership
    );
  }
  function wantsRecognition() {
    return Boolean(
      Recognition && config.enabled && !config.paused && !terminal && backendAuthorized
    );
  }

  class DurableQueue {
    constructor() {
      this.db = null;
      this.degraded = false;
    }
    async open() {
      if (!window.indexedDB) return this.useFallback("IndexedDB unavailable");
      try {
        this.db = await new Promise((resolve, reject) => {
          const request = indexedDB.open("ws-collab-captioner", 2);
          request.onupgradeneeded = () => {
            if (!request.result.objectStoreNames.contains("queue")) {
              request.result.createObjectStore("queue", { keyPath: "id" });
            }
            if (!request.result.objectStoreNames.contains("meta")) {
              request.result.createObjectStore("meta", { keyPath: "key" });
            }
          };
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        await this.seedSequence();
      } catch (error) {
        this.useFallback(`IndexedDB failed: ${error.message || error}`);
      }
    }
    useFallback(reason) {
      this.db = null;
      this.degraded = true;
      $("error").textContent = `${reason}; using bounded localStorage queue (degraded durability).`;
    }
    fallbackRows() {
      try { return JSON.parse(localStorage.getItem("ws-captioner-queue") || "[]"); }
      catch (_) { return []; }
    }
    async allRows() {
      if (!this.db) return this.fallbackRows();
      return new Promise((resolve, reject) => {
        const tx = this.db.transaction("queue", "readonly");
        const request = tx.objectStore("queue").getAll();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    async put(item) {
      if (!this.db) {
        const rows = this.fallbackRows().filter((row) => row.id !== item.id);
        rows.push(item);
        const pruned = Runtime.pruneQueueRows(rows, maxQueue);
        const accepted = pruned.kept.some((row) => row.id === item.id);
        localStorage.setItem("ws-captioner-queue", JSON.stringify(pruned.kept));
        return { accepted, backpressure: Boolean(item.is_final && pruned.kept.length >= maxQueue) };
      }
      await this.tx("readwrite", (store) => store.put(item));
      const pruned = await this.compact();
      return {
        accepted: pruned.kept.some((row) => row.id === item.id),
        backpressure: Boolean(item.is_final && pruned.kept.length >= maxQueue),
      };
    }
    async compact() {
      const pruned = Runtime.pruneQueueRows(await this.allRows(), maxQueue);
      if (pruned.removedIds.length) await this.remove(pruned.removedIds);
      return pruned;
    }
    async list(limit = maxBatch) {
      if (!this.db) return this.fallbackRows().sort(
        (a, b) => a.seq - b.seq || String(a.id).localeCompare(String(b.id))
      ).slice(0, limit);
      return (await this.allRows()).sort(
        (a, b) => a.seq - b.seq || String(a.id).localeCompare(String(b.id))
      ).slice(0, limit);
    }
    async remove(ids) {
      const wanted = new Set(ids);
      if (!this.db) {
        localStorage.setItem("ws-captioner-queue", JSON.stringify(this.fallbackRows().filter((row) => !wanted.has(row.id))));
        return;
      }
      await this.tx("readwrite", (store) => ids.forEach((id) => store.delete(id)));
    }
    async depth() { return (await this.allRows()).length; }
    async seedSequence() {
      const rows = await this.allRows();
      const queuedMax = rows.reduce(
        (highest, row) => Math.max(highest, Number(row.seq) || 0),
        0
      );
      const localMax = Number(localStorage.getItem("ws-captioner-seq") || "0");
      await new Promise((resolve, reject) => {
        const tx = this.db.transaction("meta", "readwrite");
        const store = tx.objectStore("meta");
        const request = store.get("sequence");
        request.onsuccess = () => {
          const stored = Number(request.result?.value || 0);
          store.put({
            key: "sequence",
            value: Math.max(stored, queuedMax, Number.isSafeInteger(localMax) ? localMax : 0),
          });
        };
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
    }
    async nextSequence() {
      if (!this.db) return Runtime.nextFallbackSequence(localStorage);
      return new Promise((resolve, reject) => {
        const tx = this.db.transaction("meta", "readwrite");
        const store = tx.objectStore("meta");
        const request = store.get("sequence");
        let next = 1;
        request.onsuccess = () => {
          const current = Number(request.result?.value || 0);
          next = Number.isSafeInteger(current) && current >= 0 ? current + 1 : 1;
          store.put({ key: "sequence", value: next });
        };
        tx.oncomplete = () => {
          localStorage.setItem("ws-captioner-seq", String(next));
          resolve(next);
        };
        tx.onerror = () => reject(tx.error);
      });
    }
    tx(mode, operation) {
      return new Promise((resolve, reject) => {
        const tx = this.db.transaction("queue", mode);
        operation(tx.objectStore("queue"));
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    }
  }
  const queue = new DurableQueue();
  let unloading = false;

  class RecognitionOwnership {
    constructor() {
      this.storageLease = new Runtime.StorageLease(localStorage, { ownerId: instanceId });
      this.releaseWebLock = null;
      this.acquiring = false;
      this.renewTimer = null;
      this.channel = typeof BroadcastChannel === "function"
        ? new BroadcastChannel("ws-captioner-recognition-owner")
        : null;
      if (this.channel) this.channel.onmessage = () => this.externalChange();
      window.addEventListener("storage", (event) => {
        if (event.key === this.storageLease.key) this.externalChange();
      });
    }

    async acquire() {
      if (hasOwnership || this.acquiring || !wantsRecognition() || unloading) return hasOwnership;
      this.acquiring = true;
      try {
        if (navigator.locks?.request) return await this.acquireWebLock();
        const acquired = this.storageLease.tryAcquire();
        setOwnership(acquired);
        if (acquired) {
          clearInterval(this.renewTimer);
          this.renewTimer = setInterval(() => {
            if (!this.storageLease.renew()) setOwnership(false);
          }, 2000);
          this.channel?.postMessage({ type: "acquired", ownerId: instanceId });
        }
        return acquired;
      } finally {
        this.acquiring = false;
      }
    }

    acquireWebLock() {
      return new Promise((resolve) => {
        let resolved = false;
        navigator.locks.request(
          "ws-collab-captioner-microphone",
          { mode: "exclusive", ifAvailable: true },
          async (lock) => {
            if (!lock) {
              resolved = true;
              resolve(false);
              return;
            }
            setOwnership(true);
            resolved = true;
            resolve(true);
            await new Promise((release) => { this.releaseWebLock = release; });
            this.releaseWebLock = null;
            setOwnership(false);
          }
        ).catch(() => {
          if (!resolved) resolve(false);
        });
      });
    }

    externalChange() {
      if (!navigator.locks?.request && hasOwnership && !this.storageLease.isOwner()) {
        setOwnership(false);
      }
      if (!hasOwnership) scheduleOwnershipRetry(100);
    }

    release() {
      clearInterval(this.renewTimer);
      this.renewTimer = null;
      if (this.releaseWebLock) this.releaseWebLock();
      this.releaseWebLock = null;
      this.storageLease.release();
      this.channel?.postMessage({ type: "released", ownerId: instanceId });
      setOwnership(false);
    }
  }
  const ownership = new RecognitionOwnership();

  function permissionPromptNeedsVisiblePage() {
    return ["unknown", "prompt"].includes(micPermission)
      && document.visibilityState !== "visible";
  }

  async function watchMicrophonePermission() {
    if (!navigator.permissions?.query) return;
    try {
      microphonePermissionStatus = await navigator.permissions.query({ name: "microphone" });
      micPermission = microphonePermissionStatus.state;
      microphonePermissionStatus.onchange = () => {
        micPermission = microphonePermissionStatus.state;
        if (micPermission === "granted" && shouldRun()) startRecognition();
        if (micPermission === "denied") {
          stopRecognition();
          setState("Permission denied", "Microphone permission was denied.");
        }
      };
    } catch (_) {
      micPermission = "unknown";
    }
  }

  function setOwnership(owned) {
    const changed = hasOwnership !== owned;
    hasOwnership = owned;
    if (!changed) return;
    if (owned) {
      clearTimeout(ownershipRetryTimer);
      ownershipRetryTimer = null;
      if (wantsRecognition()) startRecognition();
    } else {
      stopRecognition();
      if (wantsRecognition() && !unloading) {
        setState("Standby", "Another captioner tab owns the microphone.");
        scheduleOwnershipRetry();
      }
    }
    updateControls();
  }

  function applyBackendAuthority(authority) {
    const allowed = Boolean(authority && authority.may_capture);
    backendAuthorityReason = String(authority?.reason || "");
    if (backendAuthorized === allowed) return;
    backendAuthorized = allowed;
    if (!allowed) {
      stopRecognition();
      ownership.release();
      showDriverStatus();
    } else if (!hasOwnership) {
      requestOwnership();
    }
  }

  function scheduleOwnershipRetry(delay = 1500 + Math.floor(Math.random() * 1500)) {
    if (!wantsRecognition() || hasOwnership || ownershipRetryTimer || unloading) return;
    const bounded = Math.max(100, Math.min(3000, delay));
    ownershipRetryTimer = setTimeout(async () => {
      ownershipRetryTimer = null;
      if (!await ownership.acquire()) scheduleOwnershipRetry();
    }, bounded);
  }

  async function requestOwnership() {
    if (!wantsRecognition()) return false;
    const acquired = await ownership.acquire();
    if (!acquired) {
      setState("Standby", "Another captioner tab owns the microphone.");
      scheduleOwnershipRetry();
    }
    return acquired;
  }

  async function authenticatedFetch(path, body) {
    const response = await fetch(endpoint(path), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "content-type": "application/json",
        "x-ws-collab-captioner-token": token,
      },
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      throw new Error("Server authentication unavailable");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
    if (payload.boot_id && payload.boot_id !== bootId) {
      throw new Error("Server boot changed");
    }
    return payload;
  }

  async function enqueue(envelope) {
    const result = await queue.put({
      id: [
        envelope.session_id,
        envelope.seq,
        envelope.utterance_id,
        envelope.revision,
        Number(envelope.is_final),
      ].join(":"),
      ...envelope,
    });
    $("queue-depth").textContent = String(await queue.depth());
    if (!result.accepted) return result;
    if (result.backpressure) {
      queueBlocked = true;
      stopRecognition({ stopDetector: false });
      setState("Error", "queue full; delivery required");
    }
    scheduleDelivery(0);
    return result;
  }
  function scheduleDelivery(delay) {
    clearTimeout(deliveryTimer);
    deliveryTimer = setTimeout(deliver, delay);
  }
  async function deliver() {
    if (delivering) return;
    delivering = true;
    try {
      const rows = await queue.list(maxBatch);
      if (!rows.length) return;
      const payload = await authenticatedFetch("ingest", {
        items: rows.map(({ id, ...item }) => item),
      });
      const acknowledged = payload.results || [];
      await queue.remove(rows.filter((row) => acknowledged.some((result) => (
        result.session_id === row.session_id
        && result.seq === row.seq
        && result.utterance_id === row.utterance_id
        && result.revision === row.revision
        && result.is_final === row.is_final
      ))).map((row) => row.id));
      lastAckAt = nowIso();
      $("last-ack").textContent = new Date().toLocaleTimeString();
      $("queue-depth").textContent = String(await queue.depth());
      const depth = await queue.depth();
      if (queueBlocked && depth < maxQueue) {
        queueBlocked = false;
        lastError = "";
        if (shouldRun()) startRecognition();
      }
      if (depth) scheduleDelivery(100);
    } catch (error) {
      lastError = `delivery: ${error.message || error}`;
      setState(currentState(), lastError);
      scheduleDelivery(Math.min(30000, 1000 * (2 ** Math.min(restartAttempt, 5))) + Math.random() * 500);
    } finally {
      delivering = false;
    }
  }

  function appendFinal(envelope) {
    const event = {
      type: "STT_FINAL_RESULT",
      source_id: "browser_captioner",
      ts: envelope.result_at,
      data: {
        engine: "browser_captioner",
        raw_text: envelope.text,
        is_final: true,
        session_id: envelope.session_id,
        utterance_id: envelope.utterance_id,
        caption_seq: envelope.seq,
        speech_started_at: envelope.speech_started_at,
        speech_ended_at: envelope.speech_ended_at,
        silence_before_ms: envelope.silence_before_ms,
        pauses: envelope.pauses,
      },
    };
    const finalRow = transcriptTail.finish(event);
    transcriptRows = Transcript.mergeFinalEvents(transcriptRows, [finalRow || event]);
    Transcript.renderTranscript(document, $("final-log"), transcriptRows);
    updateTranscriptTail();
    $("final-log").scrollTop = $("final-log").scrollHeight;
  }
  function scheduleRestart(reason) {
    if (!shouldRun() || restartTimer || starting || listening || stopping) return;
    restartAttempt += 1;
    const delay = Runtime.recognitionRestartDelay(reason, restartAttempt);
    setState("Reconnecting", `Speech recognition: ${reason}; retry in ${(delay / 1000).toFixed(1)}s. This does not reopen the browser; local silence detection continues while the microphone is available.`);
    restartTimer = setTimeout(() => {
      restartTimer = null;
      startRecognition();
    }, delay);
  }
  function stopRecognition({ stopDetector = true } = {}) {
    recognitionEpoch += 1;
    if (stopDetector) {
      queueVadTransition("state_sync", "unavailable");
      vadSpeechStartedAt = null;
      vadSpeechEndedAt = null;
      speaking = false;
      pauseAssociator.reset();
      void vadCapture.stop();
    }
    recognizerSpeakingDiagnostic = false;
    clearTimeout(restartTimer);
    restartTimer = null;
    if (recognizer && (listening || starting)) {
      stopping = true;
      try { recognizer.stop(); } catch (_) { stopping = false; }
    }
  }
  function failSafeStop(reason) {
    unloading = true;
    queueBlocked = true;
    recognitionEpoch += 1;
    pauseAssociator.reset();
    void vadCapture.stop();
    if (microphonePermissionStatus) microphonePermissionStatus.onchange = null;
    clearTimeout(ownershipRetryTimer);
    clearTimeout(deliveryTimer);
    clearTimeout(restartTimer);
    restartTimer = null;
    if (recognizer) {
      stopping = true;
      try {
        if (typeof recognizer.abort === "function") recognizer.abort();
        else recognizer.stop();
      } catch (_) {
        stopping = false;
      }
    }
    ownership.release();
    setState("Error", reason);
  }
  window.__wsCollabCaptionerShutdown = () => {
    failSafeStop("server shutdown");
    return { stopped: true, instance_id: instanceId };
  };
  async function startRecognition() {
    if (!shouldRun() || starting || listening || stopping) return;
    if (permissionPromptNeedsVisiblePage()) {
      setState(
        "Permission required",
        "First run requires visible microphone permission. Use Foreground, then allow microphone access."
      );
      return;
    }
    starting = true;
    recognitionEpoch += 1;
    const epoch = recognitionEpoch;
    if (!vadCapture.stream) {
      vadSpeechStartedAt = null;
      vadSpeechEndedAt = null;
      pauseAssociator.reset();
    }
    if (!vadFailureLatched) {
      const vadReady = await vadCapture.start(epoch);
      if (!vadReady) vadFailureLatched = true;
    }
    if (!shouldRun() || epoch !== recognitionEpoch) {
      starting = false;
      await vadCapture.stop();
      return;
    }
    const audioTrack = vadCapture.stream?.getAudioTracks().find(
      track => track.kind === "audio" && track.readyState === "live"
    );
    if (!audioTrack) {
      starting = false;
      terminal = true;
      micPermission = vadCapture.permission;
      terminalState = micPermission === "denied" ? "permission_denied" : "error";
      lastError = vadCapture.error || "No live shared microphone track; recognition was not started on a different input.";
      showDriverStatus();
      updateControls();
      return;
    }
    micPermission = "granted";
    updateControls();
    const instance = new Recognition();
    recognizer = instance;
    instance.continuous = true;
    instance.interimResults = true;
    instance.lang = config.language;
    instance.onstart = () => {
      if (recognizer !== instance) return;
      starting = false;
      listening = true;
      stopping = false;
      lastError = "";
      setState("Listening");
    };
    instance.onspeechstart = () => {
      if (recognizer !== instance || !listening || stopping || !shouldRun()) return;
      recognizerSpeakingDiagnostic = true;
    };
    instance.onspeechend = () => {
      if (recognizer !== instance || !listening || stopping || !shouldRun()) return;
      recognizerSpeakingDiagnostic = false;
    };
    instance.onresult = async (event) => {
      if (!hasOwnership || recognizer !== instance || stopping || !shouldRun()) return;
      const resultAt = nowIso();
      lastResultAt = resultAt;
      restartAttempt = 0;
      const updates = Runtime.recognitionUpdates(event);
      for (const update of updates) {
        const utteranceId = identity.utteranceId(epoch, update.index);
        if (finalized.has(utteranceId) || finalizing.has(utteranceId)) continue;
        const revision = (utterances.get(utteranceId) || 0) + 1;
        utterances.set(utteranceId, revision);
        const text = update.text.slice(0, 4000);
        if (!text) continue;
        const isFinal = update.isFinal;
        pauseAssociator.updateTranscript(text, utteranceId);
        const pauseSnapshot = pauseAssociator.snapshot(text, { reserve: isFinal });
        const boundaryVersion = vadBoundaryVersion;
        const speechMetadata = {
          speech_started_at: vadStatus.available ? vadSpeechStartedAt : null,
          speech_ended_at: vadStatus.available ? vadSpeechEndedAt : null,
          ...pauseSnapshot.metadata,
        };
        $("interim").textContent = isFinal ? "Waiting for speech..." : text;
        if (!isFinal) {
          transcriptTail.setInterim({
            type: "STT_PARTIAL_RESULT", source_id: "browser_captioner",
            data: {
              engine: "browser_captioner", raw_text: text, is_final: false,
              session_id: sessionId, utterance_id: utteranceId, ...speechMetadata,
            },
          });
          updateTranscriptTail();
        }
        if (isFinal || config.send_interims) {
          if (isFinal) finalizing.add(utteranceId);
          try {
            const envelope = {
              session_id: sessionId,
              instance_id: instanceId,
              utterance_id: utteranceId,
              seq: await queue.nextSequence(),
              revision,
              text,
              is_final: isFinal,
              confidence: Number.isFinite(update.confidence) ? Math.max(0, Math.min(1, update.confidence)) : 0,
              language: config.language,
              started_at: resultAt,
              result_at: resultAt,
              ...speechMetadata,
            };
            const queued = await enqueue(envelope);
            if (isFinal && queued.accepted) {
              finalized.add(utteranceId);
              pauseAssociator.commit(pauseSnapshot);
              if (vadBoundaryVersion === boundaryVersion) {
                vadSpeechStartedAt = null;
                vadSpeechEndedAt = null;
              }
              appendFinal(envelope);
            } else if (isFinal) {
              pauseAssociator.restore(pauseSnapshot);
            }
            if (queueBlocked) break;
          } catch (error) {
            if (isFinal) pauseAssociator.restore(pauseSnapshot);
            lastError = `queue: ${error.message || error}`;
            queueBlocked = true;
            stopRecognition({ stopDetector: false });
            setState("Error", `${lastError}; queue full; delivery required`);
            break;
          } finally {
            finalizing.delete(utteranceId);
          }
        }
      }
    };
    instance.onerror = (event) => {
      lastError = String(event.error || "recognizer error");
      if (Runtime.classifyRecognitionError(lastError) === "terminal") {
        terminal = true;
        terminalState = ["not-allowed", "service-not-allowed"].includes(lastError)
          ? "permission_denied"
          : "error";
        if (terminalState === "permission_denied") micPermission = "denied";
        setState(
          terminalState === "permission_denied" ? "Permission denied" : "Error",
          `${lastError}; change configuration or use Resume listening.`
        );
        stopRecognition({ stopDetector: terminalState === "permission_denied" });
      }
    };
    instance.onend = () => {
      if (recognizer !== instance) return;
      recognitionEpoch += 1;
      pauseAssociator.updateTranscript("", null);
      recognizer = null;
      starting = false;
      listening = false;
      stopping = false;
      if (shouldRun()) scheduleRestart(lastError || "recognizer ended");
      else showDriverStatus();
    };
    try {
      instance.start(audioTrack);
    } catch (error) {
      recognitionEpoch += 1;
      pauseAssociator.updateTranscript("", null);
      recognizer = null;
      starting = false;
      lastError = String(error.message || error);
      scheduleRestart(lastError);
    }
  }

  function applyServerConfig(next) {
    if (!next || typeof next !== "object") return;
    const languageChanged = Boolean(next.language && next.language !== config.language);
    config = { ...config, ...next };
    if (languageChanged) {
      terminal = false;
      terminalState = "permission_denied";
      lastError = "";
      stopRecognition({ stopDetector: false });
    }
    updateControls();
    if (!wantsRecognition()) {
      stopRecognition({ stopDetector: !shouldDetect() });
      if (!shouldDetect()) ownership.release();
      showDriverStatus();
    } else if (!hasOwnership) {
      requestOwnership();
    } else if (!listening && !starting) {
      startRecognition();
    }
  }
  async function control(action, extra = {}) {
    const response = await authenticatedFetch("control", {
      action,
      session_id: sessionId,
      instance_id: instanceId,
      boot_id: bootId,
      ...extra,
    });
    applyServerConfig(response.config || response);
  }
  async function heartbeat() {
    try {
      updateVadStatus(vadCapture.status());
      const depth = await queue.depth();
      $("queue-depth").textContent = String(depth);
      const response = await authenticatedFetch("heartbeat", {
        session_id: sessionId,
        instance_id: instanceId,
        boot_id: bootId,
        owns_lease: hasOwnership,
        state: currentState(),
        last_error: lastError,
        queue_depth: depth,
        recognizer_supported: Boolean(Recognition),
        mic_permission: micPermission,
        last_result_at: lastResultAt,
        last_ack_at: lastAckAt,
        restart_count: restartAttempt,
        input: vadStatus.input || {
          input_scope: "microphone",
          track_label: "",
          device_fingerprint: null,
          echo_cancellation: null,
          noise_suppression: null,
          auto_gain_control: null,
          channel_count: null,
          sample_rate: null,
        },
        current_silence_ms: vadStatus.available ? vadStatus.current_silence_ms : null,
        vad: {
          source: "browser_rms_vad",
          available: Boolean(vadStatus.available),
          state: vadStatus.available ? vadStatus.state : "unavailable",
          rms: Number(vadStatus.rms || 0),
          noise_floor: Number(vadStatus.noise_floor || 0),
          threshold: Number(vadStatus.threshold || 0),
          current_silence_ms: vadStatus.available ? vadStatus.current_silence_ms : null,
          frame_interval_ms: Number(vadStatus.frame_interval_ms || 20),
          permission: vadStatus.permission || micPermission,
          error: vadStatus.error || null,
        },
      });
      heartbeatFailures.success();
      vadOwnerToken = response.vad_owner_token || null;
      applyServerConfig(response.config);
      applyBackendAuthority(response.instance);
    } catch (error) {
      lastError = `heartbeat: ${error.message || error}`;
      if (heartbeatFailures.failure()) {
        failSafeStop("server heartbeat/auth unavailable; microphone stopped");
      }
    }
  }

  $("pause").addEventListener("click", async () => {
    config.paused = true;
    stopRecognition();
    ownership.release();
    updateControls();
    showDriverStatus();
    try { await control("pause"); } catch (error) { setState("Error", error.message); }
  });
  $("resume").addEventListener("click", async () => {
    $("resume").disabled = true;
    terminal = false;
    lastError = "";
    micPermission = "prompt";
    vadFailureLatched = false;
    localStorage.removeItem("ws-captioner-paused");
    try {
      await heartbeat();
      await control("resume");
      await heartbeat();
      await requestOwnership();
    } catch (error) {
      stopRecognition();
      setState("Error", error.message);
    } finally {
      updateControls();
    }
  });
  $("language").addEventListener("change", async () => {
    const language = $("language").value.trim();
    try {
      await control("configure", { config: { language } });
      terminal = false;
      terminalState = "permission_denied";
      lastError = "";
      stopRecognition({ stopDetector: false });
      if (!recognizer && hasOwnership) startRecognition();
    } catch (error) { setState("Error", error.message); }
  });
  $("send-interims").addEventListener("change", async () => {
    try {
      await control("configure", { config: { send_interims: $("send-interims").checked } });
    } catch (error) { setState("Error", error.message); }
  });

  async function initialize() {
    await queue.open();
    await queue.compact();
    await watchMicrophonePermission();
    updateControls();
    transcriptTimer = setInterval(updateTranscriptTail, 100);
    if (!Recognition) {
      setState("Unsupported", "Chrome Web Speech API is unavailable.");
    } else if (config.paused) {
      showDriverStatus();
    } else setState("Standby", "Registering with backend selection authority.");
    scheduleDelivery(0);
    await heartbeat();
    setInterval(heartbeat, 3000);
    setInterval(() => {
      if (!vadOwnerToken || !hasOwnership) return;
      const state = vadStatus.available ? vadStatus.state : "unavailable";
      queueVadTransition("state_sync", state, nowIso());
    }, 1000);
    setInterval(() => {
      if (wantsRecognition() && !hasOwnership) {
        scheduleOwnershipRetry();
      } else if (shouldRun() && !recognizer && !starting && !restartTimer && !stopping) {
        scheduleRestart("watchdog found no recognizer");
      }
    }, 3000);
  }
  window.addEventListener("pagehide", () => {
    clearInterval(transcriptTimer);
    failSafeStop("page closed");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && shouldRun()) startRecognition();
  });
  initialize().catch((error) => setState("Error", error.message || String(error)));
})();
