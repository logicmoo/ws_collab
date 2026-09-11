(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WsCollabTranscript = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SILENCE_MARKER_THRESHOLD_MS = 300;
  const MEASURED_PAUSE_THRESHOLD_MS = 20;
  const MAX_GAP_MS = 24 * 60 * 60 * 1000;
  const MAX_TRANSCRIPT_EVENTS = 5000;

  function finiteTimestamp(value) {
    const parsed = Date.parse(typeof value === "string" ? value : "");
    return Number.isFinite(parsed) ? parsed : null;
  }

  function validDuration(value) {
    return Number.isInteger(value) && value >= 0 && value <= MAX_GAP_MS
      ? value
      : null;
  }

  function finalCaption(event) {
    const data = event && event.data;
    if (!data || (data.engine || event.source_id) !== "browser_captioner") return null;
    if (event.type && event.type !== "STT_FINAL_RESULT") return null;
    if (data.is_final === false) return null;
    const text = String(data.raw_text || data.normalized_text || "").trim();
    if (!text) return null;
    const eventSeq = event.seq;
    const captionSeq = data.caption_seq;
    const pauses = Array.isArray(data.pauses)
      ? data.pauses.map((pause) => validPause(pause, text)).filter(Boolean)
      : [];
    return {
      event,
      id: String(event.id || ""),
      seq: eventSeq !== null && eventSeq !== undefined && Number.isSafeInteger(Number(eventSeq))
        ? Number(eventSeq) : null,
      text,
      sessionId: String(data.session_id || ""),
      utteranceId: String(data.utterance_id || ""),
      captionSeq: captionSeq !== null && captionSeq !== undefined && Number.isSafeInteger(Number(captionSeq))
        ? Number(captionSeq) : null,
      eventAtMs: finiteTimestamp(event.ts),
      speechStartedAt: typeof data.speech_started_at === "string" ? data.speech_started_at : null,
      speechEndedAt: typeof data.speech_ended_at === "string" ? data.speech_ended_at : null,
      silenceBeforeMs: validDuration(data.silence_before_ms),
      hasBoundaryMetadata: Object.prototype.hasOwnProperty.call(data, "silence_before_ms"),
      pauses,
    };
  }

  function validPause(pause, text) {
    if (!pause || typeof pause !== "object") return null;
    if (pause.source !== "browser_rms_vad") return null;
    if (!["interim_prefix", "between_utterances", "approximate_text_position"].includes(pause.alignment)) {
      return null;
    }
    const duration = validDuration(pause.duration_ms);
    if (duration === null || duration < MEASURED_PAUSE_THRESHOLD_MS) return null;
    if (!Number.isInteger(pause.after_char) || pause.after_char < 0 || pause.after_char > [...text].length) {
      return null;
    }
    if (finiteTimestamp(pause.start_at) === null || finiteTimestamp(pause.end_at) === null) {
      return null;
    }
    return {
      durationMs: duration,
      startAt: pause.start_at,
      endAt: pause.end_at,
      source: pause.source,
      alignment: pause.alignment,
      afterChar: pause.after_char,
    };
  }

  function identityKeys(row) {
    const keys = [];
    if (row.id) keys.push(`id:${row.id}`);
    if (row.sessionId && row.utteranceId && row.captionSeq !== null) {
      keys.push(`caption:${row.sessionId}:${row.utteranceId}:${row.captionSeq}`);
    }
    if (!keys.length && row.seq !== null) keys.push(`seq:${row.seq}`);
    return keys;
  }

  function chronologicalCompare(a, b) {
    if (a.eventAtMs !== null && b.eventAtMs !== null && a.eventAtMs !== b.eventAtMs) {
      return a.eventAtMs - b.eventAtMs;
    }
    if (a.seq !== null && b.seq !== null && a.seq !== b.seq) return a.seq - b.seq;
    return a.id.localeCompare(b.id);
  }

  function mergeFinalEvents(existing, incoming, maxItems = MAX_TRANSCRIPT_EVENTS) {
    const rows = [];
    const seen = new Set();
    for (const eventOrRow of [...(existing || []), ...(incoming || [])]) {
      const row = eventOrRow && eventOrRow.event ? eventOrRow : finalCaption(eventOrRow);
      if (!row) continue;
      const keys = identityKeys(row);
      if (keys.some((key) => seen.has(key))) continue;
      keys.forEach((key) => seen.add(key));
      rows.push(row);
    }
    rows.sort(chronologicalCompare);
    const bound = Math.max(0, maxItems);
    return bound ? rows.slice(-bound) : [];
  }

  function clearViewCutoff(rows) {
    const seqs = (rows || []).map((row) => row.seq).filter(Number.isFinite);
    return seqs.length ? Math.max(...seqs) : -1;
  }

  function rowsAfterCutoff(rows, cutoffSeq) {
    if (cutoffSeq === null || cutoffSeq === undefined) return rows || [];
    return (rows || []).filter((row) => row.seq === null || row.seq > cutoffSeq);
  }

  async function collectFinalPages(fetchPage, {
    maxItems = MAX_TRANSCRIPT_EVENTS,
    pageSize = 1000,
  } = {}) {
    const events = [];
    const seenCursors = new Set();
    let cursor = null;
    let hasMore = true;
    let truncated = false;
    while (hasMore && events.length < maxItems) {
      const page = await fetchPage(cursor, Math.min(pageSize, maxItems - events.length));
      events.push(...((page && page.events) || []));
      hasMore = Boolean(page && page.has_more);
      const next = page && page.next_cursor;
      if (!hasMore) break;
      if (!next || next === cursor || seenCursors.has(next)) {
        truncated = true;
        break;
      }
      seenCursors.add(next);
      cursor = next;
    }
    if (hasMore && events.length >= maxItems) truncated = true;
    return { events: events.slice(0, maxItems), truncated };
  }

  function formatDuration(ms) {
    const value = Math.max(0, Math.round(Number(ms) || 0));
    if (value < 1000) return `${value}ms`;
    if (value < 60000) {
      const seconds = value / 1000;
      return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1).replace(/\.0$/, "")}s`;
    }
    const totalSeconds = Math.round(value / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    return `${minutes}m ${String(totalSeconds % 60).padStart(2, "0")}s`;
  }

  function spokenDuration(ms) {
    if (ms < 1000) return `${ms} ${ms === 1 ? "millisecond" : "milliseconds"}`;
    if (ms < 60000) {
      const seconds = Number((ms / 1000).toFixed(1));
      return `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
    }
    const totalSeconds = Math.round(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes} ${minutes === 1 ? "minute" : "minutes"}${seconds ? ` ${seconds} ${seconds === 1 ? "second" : "seconds"}` : ""}`;
  }

  function markerBetween(previous, current) {
    if (!previous || !current) return null;
    if (current.pauses.length) return null;
    if (current.silenceBeforeMs !== null) {
      if (current.silenceBeforeMs < MEASURED_PAUSE_THRESHOLD_MS) return null;
      return measuredMarker(current.silenceBeforeMs, "between_utterances");
    }

    if (current.hasBoundaryMetadata) return null;
    if (previous.eventAtMs === null || current.eventAtMs === null) return null;
    const gap = current.eventAtMs - previous.eventAtMs;
    if (gap < SILENCE_MARKER_THRESHOLD_MS || gap > MAX_GAP_MS) return null;
    return {
      kind: "approximate",
      durationMs: gap,
      text: `~${formatDuration(gap)}`,
      ariaLabel: `Approximate gap ${spokenDuration(gap)}`,
      title: "Approximate gap from event timestamps; utterance duration unknown",
    };
  }

  function alignmentLabel(alignment) {
    return {
      interim_prefix: "interim transcript prefix",
      between_utterances: "between utterances",
      approximate_text_position: "approximate text position",
    }[alignment] || "best-effort position";
  }

  function measuredMarker(durationMs, alignment) {
    return {
      kind: "measured",
      durationMs,
      text: formatDuration(durationMs),
      ariaLabel: `Measured acoustic silence ${spokenDuration(durationMs)}`,
      title: "Measured locally from microphone RMS; text position is best-effort because Chrome supplies no word timestamps. "
        + `Alignment: ${alignmentLabel(alignment)}.`,
      alignment,
    };
  }

  function silenceMarker(documentRef, marker) {
    const node = documentRef.createElement("span");
    node.className = `silence-marker ${marker.kind}`;
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", marker.ariaLabel);
    node.title = marker.title;
    node.style.setProperty(
      "--silence-scale",
      String(Math.min(1.45, 0.8 + Math.log10(Math.max(1, marker.durationMs / 300)) * 0.16))
    );
    const icon = documentRef.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 8 10");
    icon.setAttribute("aria-hidden", "true");
    const first = documentRef.createElementNS("http://www.w3.org/2000/svg", "rect");
    first.setAttribute("x", "1");
    first.setAttribute("y", "1");
    first.setAttribute("width", "2");
    first.setAttribute("height", "8");
    const second = documentRef.createElementNS("http://www.w3.org/2000/svg", "rect");
    second.setAttribute("x", "5");
    second.setAttribute("y", "1");
    second.setAttribute("width", "2");
    second.setAttribute("height", "8");
    icon.append(first, second);
    const label = documentRef.createElement("span");
    label.textContent = marker.text;
    node.append(icon, label);
    return node;
  }

  function renderTranscript(documentRef, container, rows) {
    container.replaceChildren();
    if (!rows.length) return;
    const fragment = documentRef.createDocumentFragment();
    rows.forEach((row, index) => {
      const marker = index ? markerBetween(rows[index - 1], row) : null;
      if (marker) fragment.appendChild(silenceMarker(documentRef, marker));
      const utterance = documentRef.createElement("span");
      utterance.className = "transcript-utterance";
      let cursor = 0;
      const characters = [...row.text];
      [...row.pauses].sort((a, b) => a.afterChar - b.afterChar).forEach((pause) => {
        const chunk = documentRef.createElement("span");
        chunk.textContent = characters.slice(cursor, pause.afterChar).join("");
        utterance.appendChild(chunk);
        utterance.appendChild(silenceMarker(
          documentRef,
          measuredMarker(pause.durationMs, pause.alignment)
        ));
        cursor = pause.afterChar;
      });
      const tail = documentRef.createElement("span");
      tail.textContent = characters.slice(cursor).join("");
      utterance.appendChild(tail);
      fragment.appendChild(utterance);
    });
    container.appendChild(fragment);
  }

  class SpeechBoundaryTracker {
    constructor({
      wallNow = () => Date.now(),
      monotonicNow = () => performance.now(),
      maxGapMs = MAX_GAP_MS,
    } = {}) {
      this.wallNow = wallNow;
      this.monotonicNow = monotonicNow;
      this.maxGapMs = maxGapMs;
      this.epoch = null;
      this.lastEnd = null;
      this.active = null;
      this.pending = null;
      this.utterances = new Map();
    }

    beginEpoch(epoch) {
      this.epoch = epoch;
      this.lastEnd = null;
      this.active = null;
      this.pending = null;
    }

    abortEpoch() {
      this.epoch = null;
      this.lastEnd = null;
      this.active = null;
      this.pending = null;
    }

    speechStart(epoch = this.epoch) {
      if (this.epoch === null || epoch !== this.epoch) {
        return this.publicMetadata(null);
      }
      const wallMs = this.wallNow();
      const monotonicMs = this.monotonicNow();
      let silence = null;
      if (this.lastEnd) {
        const elapsed = monotonicMs - this.lastEnd.monotonicMs;
        if (Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= this.maxGapMs) {
          silence = Math.round(elapsed);
        }
      }
      this.lastEnd = null;
      this.active = {
        speech_started_at: new Date(wallMs).toISOString(),
        speech_ended_at: null,
        silence_before_ms: silence,
        startedMonotonicMs: monotonicMs,
      };
      this.pending = this.active;
      return this.publicMetadata(this.active);
    }

    speechEnd(epoch = this.epoch) {
      if (this.epoch === null || epoch !== this.epoch || !this.active) {
        return this.publicMetadata(null);
      }
      const wallMs = this.wallNow();
      const monotonicMs = this.monotonicNow();
      if (monotonicMs >= this.active.startedMonotonicMs) {
        this.active.speech_ended_at = new Date(wallMs).toISOString();
      }
      this.lastEnd = { wallMs, monotonicMs };
      const ended = this.active;
      this.active = null;
      return this.publicMetadata(ended);
    }

    forUtterance(utteranceId) {
      if (!this.utterances.has(utteranceId)) {
        this.utterances.set(utteranceId, this.pending || null);
        this.pending = null;
      }
      return this.publicMetadata(this.utterances.get(utteranceId));
    }

    currentSilenceMs() {
      if (this.epoch === null || this.active || !this.lastEnd) return null;
      const elapsed = this.monotonicNow() - this.lastEnd.monotonicMs;
      if (!Number.isFinite(elapsed) || elapsed < 0) return null;
      return Math.min(this.maxGapMs, Math.round(elapsed));
    }

    publicMetadata(boundary) {
      return {
        speech_started_at: boundary ? boundary.speech_started_at : null,
        speech_ended_at: boundary ? boundary.speech_ended_at : null,
        silence_before_ms: boundary ? boundary.silence_before_ms : null,
      };
    }
  }

  return {
    MAX_GAP_MS,
    MAX_TRANSCRIPT_EVENTS,
    MEASURED_PAUSE_THRESHOLD_MS,
    SILENCE_MARKER_THRESHOLD_MS,
    SpeechBoundaryTracker,
    clearViewCutoff,
    collectFinalPages,
    finalCaption,
    formatDuration,
    markerBetween,
    measuredMarker,
    mergeFinalEvents,
    renderTranscript,
    rowsAfterCutoff,
    silenceMarker,
    spokenDuration,
    validPause,
  };
});
