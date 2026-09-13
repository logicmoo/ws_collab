(function (root, factory) {
  const runtime = factory();
  if (typeof module === "object" && module.exports) module.exports = runtime;
  else root.WsCollabLanguageChat = runtime;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  function createClientId(storage, crypto) {
    const key = "ws_collab_language_chat_client";
    let id;
    // Duplicating a tab copies sessionStorage, so never reuse its playback owner.
    if (crypto.randomUUID) id = crypto.randomUUID();
    else {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
      id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    try { storage.setItem(key, id); } catch (_) {}
    return id;
  }

  // A user command invalidates every older request, including a slow poll.
  function createRevisionGate() {
    let epoch = 0, sequence = 0, accepted = 0, session = null, generation = -1;
    const retiredSessions = new Set();
    return {
      invalidate() { epoch += 1; },
      ticket() { return { epoch, sequence: ++sequence }; },
      accept(ticket, state) {
        if (ticket.epoch !== epoch || ticket.sequence < accepted) return false;
        const nextSession = String(state.session_id || "");
        const nextGeneration = Number(state.generation || 0);
        if (nextSession && retiredSessions.has(nextSession)) return false;
        if (session === nextSession && nextGeneration < generation) return false;
        if (session && nextSession && session !== nextSession) retiredSessions.add(session);
        session = nextSession;
        generation = nextGeneration;
        accepted = ticket.sequence;
        return true;
      },
      current(ticket) { return ticket.epoch === epoch; },
    };
  }

  function createDraft() {
    let revision = 0, saved = 0;
    return {
      edit() { revision += 1; },
      get dirty() { return revision !== saved; },
      capture() { return revision; },
      saved(atRevision) { if (atRevision === revision) saved = revision; },
      sync(write) { if (revision === saved) write(); },
    };
  }

  function chooseVoice(voices, config = {}) {
    const installed = voices.filter((voice) => voice && voice.voiceURI);
    const preferred = installed.find((voice) => voice.voiceURI === config.voice_uri);
    if (preferred) return preferred;
    const language = String(config.language || "en-US").toLowerCase();
    const matching = installed.filter((voice) => String(voice.lang).toLowerCase() === language);
    const related = installed.filter((voice) => String(voice.lang).toLowerCase().split("-")[0] === language.split("-")[0]);
    return matching.find((voice) => voice.default) || matching.find((voice) => voice.localService) || matching[0] ||
      related.find((voice) => voice.default) || related[0] ||
      installed.find((voice) => voice.default) || installed.find((voice) => voice.localService) || installed[0] || null;
  }

  function parseHistory(text) {
    let messages;
    try { messages = JSON.parse(text); }
    catch (_) { throw new Error("History must be a JSON array of user/assistant messages."); }
    if (!Array.isArray(messages) || messages.some((m) =>
      !m || !["user", "assistant"].includes(m.role) ||
      typeof m.content !== "string" || !m.content.trim() ||
      Object.keys(m).some((key) => !["role", "content"].includes(key)))) {
      throw new Error('Each history entry must contain only role ("user" or "assistant") and nonempty string content. Edit the system prompt separately.');
    }
    return messages.map(({ role, content }) => ({ role, content }));
  }

  function timestampMillis(value) {
    if (typeof value !== "number" && typeof value !== "string") return null;
    if (value === "") return null;
    const numeric = typeof value === "number" || /^-?\d+(\.\d+)?$/.test(value);
    const millis = numeric ? Number(value) * 1000 : Date.parse(value);
    return Number.isFinite(millis) && !Number.isNaN(new Date(millis).getTime()) ? millis : null;
  }

  function measuredMs(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
  }

  function formatDurationMs(value) {
    if (!measuredMs(value)) return "Not measured";
    return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
  }

  function estimateSpeechDurationMs(text, rate = 1) {
    const words = String(text || "").trim().match(/\S+/g);
    if (!words) return null;
    const speed = Number.isFinite(Number(rate)) && Number(rate) > 0 ? Math.max(0.5, Math.min(2, Number(rate))) : 1;
    return Math.round(words.length * 60000 / (180 * speed));
  }

  function queuedSpeechEstimateMs(state) {
    if (!state || !state.active || (state.config && state.config.speak_replies === false)) return null;
    const text = (state.speech_queue || []).filter((item) =>
      ["pending", "playing"].includes(item.status) && String(item.generation) === String(state.generation) &&
      !(state.messages || []).some((message) => message.id === item.message_id && ["interrupted", "error"].includes(message.status)))
      .map((item) => item.text || "").join(" ");
    return estimateSpeechDurationMs(text, state.config && state.config.speech_rate);
  }

  function speechDurationMs(_event, startedAt, endedAt) {
    // Browser voices disagree about elapsedTime units; measure the event interval ourselves.
    const clockMs = measuredMs(startedAt) && measuredMs(endedAt) ? endedAt - startedAt : null;
    return measuredMs(clockMs) && clockMs <= 1800000 ? Math.round(clockMs) : undefined;
  }

  function timingTrace(entries) {
    if (!Array.isArray(entries) || !entries.length) return null;
    const latest = entries.filter((entry) => entry && typeof entry === "object").reduce((chosen, entry) => {
      if (!chosen) return entry;
      const previousTime = timestampMillis(chosen.input_received_at);
      const nextTime = timestampMillis(entry.input_received_at);
      return previousTime !== null && nextTime !== null && previousTime > nextTime ? chosen : entry;
    }, null);
    if (!latest) return null;
    const status = latest.status || "unknown";
    const terminal = ["complete", "interrupted", "error"].includes(status);
    const has = (key) => timestampMillis(latest[key]) !== null;
    const modelStarted = has("request_started_at") || ["thinking", "responding", "speaking"].includes(status);
    const definitions = [
      ["speech_to_stt_ms", "Speech → STT", true, false],
      ["stt_delivery_ms", "STT delivery", true, false],
      ["turn_wait_ms", "Turn-silence wait", has("request_started_at"), has("input_received_at") || status === "thinking"],
      ["model_queue_ms", "Model queue", has("request_started_at"), has("input_received_at") || status === "thinking"],
      ["emullm_first_token_ms", "emullm first token", has("first_token_at") || has("response_finished_at"), modelStarted],
      ["emullm_total_ms", "emullm response total", has("response_finished_at"), modelStarted],
      ["tts_queue_wait_ms", "Browser TTS queue wait", has("tts_started_at") || has("tts_finished_at"), has("response_finished_at") || ["responding", "speaking"].includes(status)],
      ["tts_playback_ms", "Browser TTS playback", has("tts_finished_at"), has("tts_started_at") || status === "speaking"],
    ];
    const stages = definitions.map(([key, label, finished, started]) => {
      const value = latest[key];
      const state = measuredMs(value) ? "measured" : terminal || finished ? "unavailable" : started ? "waiting" : "pending";
      return {
        key, label, state,
        display: state === "measured" ? formatDurationMs(value) : state === "waiting" ? "Waiting for measurement" : state === "pending" ? "Awaiting stage" : "Not measured",
      };
    });
    return {
      latest, status, stages,
      elapsed: formatDurationMs(latest.elapsed_ms),
      total: measuredMs(latest.total_ms) ? formatDurationMs(latest.total_ms) : terminal ? "Not measured" : "Pending",
    };
  }

  function renderMessage(document, message) {
    const row = document.createElement("article");
    row.className = "lc-message";
    row.dataset.role = message.role === "user" ? "user" : "assistant";
    const header = document.createElement("header");
    const who = document.createElement("strong");
    who.textContent = message.role === "user" ? "You" : message.source_agent_id || "Agent";
    const time = document.createElement("time");
    const millis = timestampMillis(message.created_at);
    const date = new Date(millis === null ? NaN : millis);
    time.textContent = Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleTimeString();
    if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
    const badge = document.createElement("span");
    badge.className = "lc-message-status";
    badge.textContent = message.status || "confirmed";
    header.append(who, time, badge);
    const content = document.createElement("div");
    content.className = "lc-message-content";
    content.textContent = message.content || "";
    row.append(header, content);
    return row;
  }

  function createPlayback(options) {
    const synth = options.synthesis;
    const Utterance = options.Utterance;
    const now = options.now || Date.now;
    const monotonicNow = options.monotonicNow || (() => typeof performance !== "undefined" && performance.now ? performance.now() : now());
    const later = options.setTimeout || setTimeout;
    const clear = options.clearTimeout || clearTimeout;
    const clientId = options.clientId;
    const supported = !!(synth && Utterance);
    const staleAfterMs = 6000;
    // The owner changes on reload; the session/generation/queue journal must not.
    const storageKey = "ws_collab_language_chat_spoken";
    const seen = new Set();
    try {
      const stored = JSON.parse(options.storage.getItem(storageKey) || "[]");
      if (Array.isArray(stored)) stored.forEach((key) => seen.add(key));
    } catch (_) {}
    let state = null, revision = "", blockedRevision = null, armed = false;
    let muted = false, current = null, serial = 0, freshAt = 0, watchdog = null;
    let startDeadline = null, queuedByUs = false;
    let status = supported ? "idle" : "unavailable";
    let detail = supported ? "Start voice chat to enable spoken replies." : "Browser speechSynthesis is unavailable; replies are text only.";
    let ackChain = Promise.resolve();

    function report(next, message) {
      status = next;
      detail = message || "";
      if (options.onStatus) options.onStatus({ status, detail, supported, armed });
    }
    function remember(item) {
      seen.add(`${revision}:${item.id}`);
      while (seen.size > 2000) seen.delete(seen.values().next().value);
      try { options.storage.setItem(storageKey, JSON.stringify([...seen])); } catch (_) {}
    }
    function acknowledge(speechState, item, error, durationMs) {
      const payload = { client_id: clientId, speech_state: speechState };
      if (item) payload.speech_id = item.id;
      if (error) payload.error = error;
      if (speechState === "done" && Number.isInteger(durationMs) && durationMs >= 0 && durationMs <= 1800000) payload.duration_ms = durationMs;
      const submittedRevision = revision;
      ackChain = ackChain.then(() => {
        if (submittedRevision !== revision || !owns()) return;
        return options.ack(payload);
      }).catch((err) => {
        if (submittedRevision !== revision || !owns()) return;
        unavailable(`Speech acknowledgement failed: ${err.message || err}`);
        if (options.onUnreachable) options.onUnreachable(err);
      });
    }
    function cancel(reason, acknowledgeCurrent = false) {
      const item = current && current.item;
      serial += 1;
      current = null;
      clear(startDeadline);
      if (supported && queuedByUs) {
        try { synth.cancel(); } catch (_) {}
      }
      queuedByUs = false;
      if (acknowledgeCurrent && item) acknowledge("error", item, reason);
    }
    function owns() {
      return !!(state && state.active && state.client_id === clientId);
    }
    function unavailable(reason) {
      armed = false;
      clear(watchdog);
      cancel(reason);
      report("unknown", `${reason} Playback paused; use Start voice chat to resume after reconnecting.`);
    }
    function expire() {
      if (owns() && armed && now() - freshAt >= staleAfterMs) unavailable("Language chat state is stale.");
    }
    function pump() {
      if (!owns() || !armed || current || revision === blockedRevision) return;
      if (now() - freshAt >= staleAfterMs) { expire(); return; }
      const queue = Array.isArray(state.speech_queue) ? state.speech_queue : [];
      const item = queue.find((entry) =>
        entry.status === "pending" && String(entry.generation) === String(state.generation) &&
        !seen.has(`${revision}:${entry.id}`) &&
        !(state.messages || []).some((m) => m.id === entry.message_id && ["interrupted", "error"].includes(m.status)));
      if (!item) return;
      remember(item); // Reserve before speak: reloads cannot replay an unacknowledged chunk.
      if (muted || (state.config && state.config.speak_replies === false) || !supported) {
        const reason = supported ? "Replies muted; this chunk was not spoken." : "Browser speechSynthesis is unavailable.";
        acknowledge("error", item, reason);
        report(supported ? "muted" : "unavailable", reason);
        // Drain through the next poll, never recurse through an unbounded server queue.
        return;
      }
      const config = state.config || {};
      const utterance = new Utterance(String(item.text || ""));
      utterance.lang = config.language || "en-US";
      utterance.rate = Math.max(0.5, Math.min(2, Number(config.speech_rate) || 1));
      const voiceUri = item.voice_uri || config.voice_uri;
      if (voiceUri) {
        const voice = synth.getVoices().find((v) => v.voiceURI === voiceUri);
        if (!voice) {
          acknowledge("error", item, "Selected browser voice is unavailable. Choose an installed voice.");
          report("error", "Selected browser voice is unavailable. Stop and choose an installed voice.");
          return;
        }
        utterance.voice = voice;
      }
      const ticket = ++serial;
      current = { item, utterance };
      const live = () => current && serial === ticket && owns() && armed;
      utterance.onstart = () => {
        if (!live()) return;
        clear(startDeadline);
        if (!current.started) current.startedAt = monotonicNow();
        current.started = true;
        report("speaking", "Speaking through this browser.");
        acknowledge("speaking", item);
      };
      utterance.onend = (event) => {
        if (!live()) return;
        clear(startDeadline);
        const durationMs = speechDurationMs(event, current.startedAt, monotonicNow());
        current = null;
        report("idle", "Reply chunk spoken.");
        acknowledge("done", item, undefined, durationMs);
        pump();
      };
      utterance.onerror = (event) => {
        if (!live()) return;
        current = null;
        armed = false;
        const error = String(event.error || "Browser speech playback failed");
        acknowledge("error", item, error);
        cancel(error);
        report("error", `${error}. Press Start voice chat to retry; failed chunks are not replayed.`);
      };
      try {
        report("queued", "Waiting for browser speech playback.");
        startDeadline = later(() => {
          if (live() && !current.started) utterance.onerror({ error: "Browser speech did not start" });
        }, 8000);
        queuedByUs = true;
        synth.speak(utterance);
      } catch (error) {
        utterance.onerror({ error: error.message });
      }
    }
    return {
      supported,
      get armed() { return armed; },
      get status() { return status; },
      get detail() { return detail; },
      get speechState() { return current && current.started ? "speaking" : "idle"; },
      get speechId() { return current ? current.item.id : undefined; },
      // Called synchronously from the Start button, before its first await.
      unlock() {
        armed = true;
        blockedRevision = null;
        if (supported) {
          try {
            synth.resume();
            const unlock = new Utterance("");
            unlock.volume = 0;
            queuedByUs = true;
            synth.speak(unlock);
          } catch (error) {
            report("error", `Browser speech could not be enabled: ${error.message}`);
            return;
          }
        }
        report(supported ? "idle" : "unavailable", supported ? "Spoken replies enabled by your Start action." : "Browser speechSynthesis is unavailable; replies are text only.");
      },
      update(next) {
        const nextRevision = `${next.session_id || ""}:${next.generation || 0}`;
        if (state && next.session_id === state.session_id && Number(next.generation) < Number(state.generation)) return;
        const changed = revision !== nextRevision;
        if (changed || !next.active || next.client_id !== clientId) {
          cancel("Session, generation, or playback owner changed.");
          if (changed) blockedRevision = null;
        }
        state = next;
        revision = nextRevision;
        freshAt = now();
        clear(watchdog);
        if (owns() && armed) watchdog = later(expire, staleAfterMs);
        if (!owns()) {
          armed = false;
          if (!supported) report("unavailable", "Browser speechSynthesis is unavailable; replies are text only.");
          else report(next.active ? "other-client" : "idle", next.active ? "Another client owns spoken replies." : "Voice chat stopped; existing STT listening is unaffected.");
          return;
        }
        if (current) {
          const entry = (next.speech_queue || []).find((q) => q.id === current.item.id);
          const message = (next.messages || []).find((m) => m.id === current.item.message_id);
          if (!entry || !["pending", "playing"].includes(entry.status) ||
              (message && ["interrupted", "error"].includes(message.status))) {
            cancel("Reply interrupted.");
            report("idle", "Reply interrupted.");
          }
        }
        if (next.config && next.config.speak_replies === false) {
          cancel("Replies muted.", true);
          report("muted", "Replies muted; text remains visible.");
        }
        pump();
      },
      mute(value) {
        muted = !!value;
        if (muted) {
          cancel("Replies muted.", true);
          report("muted", "Replies muted; text remains visible.");
        } else report(supported ? "idle" : "unavailable", "Voice setting changed.");
      },
      interrupt() {
        blockedRevision = revision;
        cancel("Reply interrupted.");
        report("idle", "Reply interrupted; waiting for the server input gate to reopen.");
      },
      stop() {
        armed = false;
        blockedRevision = revision;
        clear(watchdog);
        cancel("Voice chat stopped.");
        report("idle", "Voice chat stopped; existing STT listening is unaffected.");
      },
      unavailable,
      expire,
      heartbeat() {
        if (owns()) acknowledge(current && current.started ? "speaking" : "idle", current && current.started ? current.item : null);
      },
      flushAcks() { return ackChain; },
    };
  }

  return {
    createClientId, createRevisionGate, createDraft, chooseVoice, parseHistory, renderMessage, createPlayback,
    timestampMillis, formatDurationMs, estimateSpeechDurationMs, queuedSpeechEstimateMs, speechDurationMs, timingTrace,
  };
});
