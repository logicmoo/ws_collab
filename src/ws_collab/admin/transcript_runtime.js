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
  const markerData = new WeakMap();
  const transcriptEdges = new WeakMap();

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

  function interimCaption(event) {
    if (event?.type !== "STT_PARTIAL_RESULT" || event.data?.is_final !== false) return null;
    return finalCaption({
      ...event, type: "STT_FINAL_RESULT", data: { ...event.data, is_final: true },
    });
  }

  function silencePosition(prefix, text) {
    const characters = [...text];
    if (!prefix) return 0;
    if (text.startsWith(prefix)) return [...prefix].length;
    const normalizedPrefix = [...prefix.normalize("NFKC").toLowerCase()]
      .filter(character => /[\p{L}\p{N}]/u.test(character)).join("");
    let normalized = "";
    const positions = [];
    characters.forEach((character, index) => {
      for (const part of character.normalize("NFKC").toLowerCase()) {
        if (/[\p{L}\p{N}]/u.test(part)) {
          normalized += part;
          positions.push(index + 1);
        }
      }
    });
    if (normalizedPrefix && normalized.startsWith(normalizedPrefix)) {
      return positions[[...normalizedPrefix].length - 1];
    }
    const wordCount = prefix.trim().split(/\s+/u).length;
    const ends = [];
    characters.forEach((character, index) => {
      if (!/\s/u.test(character) && (index + 1 === characters.length || /\s/u.test(characters[index + 1]))) {
        ends.push(index + 1);
      }
    });
    return ends[Math.min(wordCount, ends.length) - 1] ?? 0;
  }

  function reviseCaption(previous, current) {
    if (!current) return null;
    const sameUtterance = previous && current.sessionId && current.utteranceId
      && previous.sessionId === current.sessionId && previous.utteranceId === current.utteranceId;
    const key = pause => `${pause.startAt}|${pause.endAt}|${pause.durationMs}`;
    const old = new Map((sameUtterance ? previous.pauses : []).map(pause => [key(pause), pause]));
    const combined = new Map(current.pauses.map(pause => [key(pause), pause]));
    for (const [id, pause] of old) if (!combined.has(id)) combined.set(id, pause);
    const pauses = [...combined].map(([id, pause]) => {
      const original = old.get(id);
      const anchorPrefix = original
        ? (original.anchorPrefix ?? [...previous.text].slice(0, original.afterChar).join(""))
        : [...current.text].slice(0, pause.afterChar).join("");
      return {
        ...pause,
        anchorPrefix,
        afterChar: silencePosition(anchorPrefix, current.text),
        alignment: original && previous.text !== current.text && original.alignment !== "between_utterances"
          ? "approximate_text_position" : pause.alignment,
      };
    });
    return { ...current, pauses };
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
      if (current.silenceBeforeMs < 1) return null;
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

  function measuredMarker(durationMs, alignment, interval = {}) {
    return {
      kind: "measured",
      durationMs,
      text: formatDuration(durationMs),
      ariaLabel: `Measured acoustic silence ${spokenDuration(durationMs)}`,
      title: "Measured locally from microphone RMS; text position is best-effort because Chrome supplies no word timestamps. "
        + `Alignment: ${alignmentLabel(alignment)}.`,
      alignment,
      startMs: finiteTimestamp(interval.startAt),
      endMs: finiteTimestamp(interval.endAt),
    };
  }

  function combineSilenceMarkers(markers) {
    if (markers.length === 1) return markers[0];
    const segments = markers.flatMap(marker => marker.segments || [marker]);
    const intervals = new Map();
    let durationMs = 0;
    for (const segment of segments) {
      if (Number.isFinite(segment.startMs) && Number.isFinite(segment.endMs)
          && segment.endMs >= segment.startMs) {
        const key = `${segment.startMs}:${segment.endMs}`;
        const previous = intervals.get(key);
        if (!previous || segment.durationMs > previous.durationMs) intervals.set(key, segment);
      } else {
        durationMs += segment.durationMs;
      }
    }
    let coveredUntil = -Infinity;
    for (const segment of [...intervals.values()].sort(
      (a, b) => a.startMs - b.startMs || b.endMs - a.endMs
    )) {
      const overlap = Math.max(0, Math.min(coveredUntil, segment.endMs) - segment.startMs);
      durationMs += Math.max(0, segment.durationMs - overlap);
      coveredUntil = Math.max(coveredUntil, segment.endMs);
    }
    const approximate = markers.some(marker => marker.kind === "approximate");
    return {
      ...measuredMarker(durationMs, markers[0].alignment),
      kind: approximate ? "approximate" : "measured",
      text: `${approximate ? "~" : ""}${formatDuration(durationMs)}`,
      ariaLabel: `${approximate ? "Combined approximate gap" : "Measured acoustic silence"} ${spokenDuration(durationMs)}`,
      title: approximate
        ? "Combined adjacent gaps; includes approximate event-timestamp timing."
        : "Combined adjacent measured silences with no words between them; overlapping intervals are counted once. Text position is best-effort.",
      segments,
    };
  }

  function displaySilenceMarker(node, marker, live = false) {
    const className = `silence-marker ${marker.kind}${live ? " live" : ""}`;
    if (node.className !== className) node.className = className;
    node.setAttribute("role", "note");
    node.setAttribute("aria-label", marker.ariaLabel);
    node.title = marker.title;
    if (node.textContent !== marker.text) node.textContent = marker.text;
    node.style.setProperty(
      "--silence-scale",
      String(Math.min(1.45, 0.8 + Math.log10(Math.max(1, marker.durationMs / 300)) * 0.16))
    );
  }

  function silenceMarker(documentRef, marker) {
    const node = documentRef.createElement("span");
    displaySilenceMarker(node, marker);
    markerData.set(node, marker);
    return node;
  }

  function renderTranscript(documentRef, container, rows) {
    container.replaceChildren();
    const edges = { leading: null, trailing: null };
    transcriptEdges.set(container, edges);
    if (!rows.length) return;
    const fragment = documentRef.createDocumentFragment();
    let pending = null;
    let hasWords = false;
    const appendText = (parent, text) => {
      if (text.trim()) {
        hasWords = true;
        pending = null;
      }
      const chunk = documentRef.createElement("span");
      chunk.textContent = text;
      parent.appendChild(chunk);
    };
    const appendMarker = (parent, marker) => {
      if (pending) {
        const combined = combineSilenceMarkers([markerData.get(pending), marker]);
        markerData.set(pending, combined);
        displaySilenceMarker(pending, combined);
      } else {
        pending = silenceMarker(documentRef, marker);
        parent.appendChild(pending);
      }
      if (!hasWords) edges.leading = pending;
    };
    rows.forEach((row, index) => {
      const marker = index ? markerBetween(rows[index - 1], row) : null;
      if (marker) appendMarker(fragment, marker);
      const utterance = documentRef.createElement("span");
      utterance.className = "transcript-utterance";
      let cursor = 0;
      const characters = [...row.text];
      [...row.pauses].sort((a, b) => a.afterChar - b.afterChar).forEach((pause) => {
        appendText(utterance, characters.slice(cursor, pause.afterChar).join(""));
        appendMarker(utterance, measuredMarker(pause.durationMs, pause.alignment, pause));
        cursor = pause.afterChar;
      });
      appendText(utterance, characters.slice(cursor).join(""));
      fragment.appendChild(utterance);
    });
    container.appendChild(fragment);
    edges.trailing = pending;
  }

  function liveSilenceState(vad, { ageMs = 0, maxAgeMs = 15000, reason = "" } = {}) {
    if (reason) return { text: reason, active: false };
    if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs > maxAgeMs) {
      return { text: "Silence detector unavailable (stale update)", active: false };
    }
    if (!vad || vad.source !== "browser_rms_vad" || !vad.available) {
      return { text: "Silence detector unavailable", active: false };
    }
    if (vad.state === "speech") return { text: "Speech detected", active: false };
    const duration = validDuration(vad.current_silence_ms);
    if (vad.state !== "silence" || duration === null) {
      return { text: "Listening for speech", active: false };
    }
    const elapsed = Math.min(MAX_GAP_MS, Math.round(duration + ageMs));
    return {
      text: formatDuration(elapsed),
      ariaLabel: `Current silence ${spokenDuration(elapsed)}; still listening`,
      active: true,
      durationMs: elapsed,
    };
  }

  class LiveTranscriptTail {
    constructor(documentRef, container, {
      monotonicNow = () => performance.now(),
      wallNow = () => Date.now(),
    } = {}) {
      this.container = container;
      this.monotonicNow = monotonicNow;
      this.wallNow = wallNow;
      this.documentRef = documentRef;
      this.sample = null;
      this.interim = null;
      this.mergedNodes = new Set();
      this.hiddenMarkers = new Set();
      this.node = documentRef.createElement("span");
      this.node.className = "transcript-live-tail";
      // The transcript remains live, but do not announce the timer on every tick.
      this.node.setAttribute("aria-live", "off");
      this.silence = documentRef.createElement("span");
      this.draft = documentRef.createElement("span");
      this.draft.className = "transcript-draft";
      this.draft.setAttribute("aria-live", "polite");
      this.draft.title = "Interim caption: these words and their positions may change before recognition is final.";
      this.age = documentRef.createElement("span");
      this.age.className = "transcript-last-age";
      this.age.title = "Time since the last finalized caption, not a measurement of silence.";
      this.node.append(this.draft, this.silence, this.age);
    }

    observe(vad, { ageMs = 0, maxAgeMs = 15000, reason = "" } = {}) {
      this.sample = {
        vad: vad ? { ...vad } : null,
        observedAt: this.monotonicNow(),
        ageMs,
        maxAgeMs,
        reason,
      };
    }

    setInterim(event) {
      this.interim = event ? reviseCaption(this.interim, interimCaption(event)) : null;
      renderTranscript(this.documentRef, this.draft, this.interim ? [this.interim] : []);
    }

    finish(event) {
      const row = finalCaption(event);
      if (row && this.interim && row.sessionId === this.interim.sessionId
          && row.utteranceId === this.interim.utteranceId) {
        const revised = reviseCaption(this.interim, row);
        this.setInterim(null);
        return revised;
      }
      return row;
    }

    mergeBoundarySilences(status, waitingForWords) {
      const previous = transcriptEdges.get(this.container)?.trailing;
      const draft = transcriptEdges.get(this.draft);
      if (status.active) {
        const now = this.wallNow();
        markerData.set(this.silence, {
          ...measuredMarker(status.durationMs, "between_utterances"),
          startMs: now - status.durationMs, endMs: now,
        });
      }
      const live = status.active ? this.silence : null;
      const groups = this.interim
        ? [[previous, draft?.leading], [draft?.trailing, live]]
        : [waitingForWords ? [] : [previous, live]];
      const merged = new Map();
      const hidden = new Set();
      for (const group of groups) {
        const nodes = group.filter(Boolean);
        if (nodes.length < 2) continue;
        const sink = nodes.includes(this.silence) ? this.silence : nodes[0];
        const marker = combineSilenceMarkers(nodes.map(node => markerData.get(node)));
        if (sink === this.silence) {
          marker.ariaLabel += "; current silence is still ongoing";
          marker.title += " Includes the current, ongoing silence.";
        }
        merged.set(sink, marker);
        nodes.filter(node => node !== sink).forEach(node => hidden.add(node));
      }
      for (const node of this.mergedNodes) {
        if (node !== this.silence && !merged.has(node)) displaySilenceMarker(node, markerData.get(node));
      }
      for (const node of this.hiddenMarkers) if (!hidden.has(node)) node.hidden = false;
      for (const node of hidden) node.hidden = true;
      for (const [node, marker] of merged) displaySilenceMarker(node, marker, node === this.silence);
      this.mergedNodes = new Set(merged.keys());
      this.hiddenMarkers = hidden;
    }

    update(lastRow) {
      const sample = this.sample;
      const status = liveSilenceState(sample?.vad, sample ? {
        ageMs: sample.ageMs + Math.max(0, this.monotonicNow() - sample.observedAt),
        maxAgeMs: sample.maxAgeMs,
        reason: sample.reason,
      } : {});
      const waitingForWords = status.text === "Speech detected" || (status.active && !lastRow);
      const empty = this.container.querySelector?.(".transcript-empty");
      if (!lastRow && !this.interim && !waitingForWords) {
        this.mergeBoundarySilences(status, true);
        this.node.remove();
        if (empty) empty.hidden = false;
        return;
      }
      if (!this.interim) {
        const placeholder = waitingForWords ? "Recognizing speech... " : "";
        if (this.draft.textContent !== placeholder) this.draft.textContent = placeholder;
      }
      const ageMs = !lastRow || lastRow.eventAtMs === null
        ? null : Math.max(0, this.wallNow() - lastRow.eventAtMs);
      const ageText = ageMs === null ? "" : ` Last caption ${formatDuration(ageMs)} ago`;
      const silenceClass = status.active ? "silence-marker live" : "transcript-detector-state";
      if (this.silence.className !== silenceClass) this.silence.className = silenceClass;
      if (this.silence.textContent !== status.text) {
        this.silence.textContent = status.text;
        this.silence.setAttribute("aria-label", status.ariaLabel || status.text);
      }
      this.silence.title = status.active
        ? "Silence detected locally from microphone RMS. The timer advances between detector reports; listening has not been paused."
        : status.text;
      this.mergeBoundarySilences(status, waitingForWords);
      if (this.age.textContent !== ageText) this.age.textContent = ageText;
      if (empty) empty.hidden = true;
      if (this.node.parentNode !== this.container) this.container.appendChild(this.node);
    }
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
    LiveTranscriptTail,
    clearViewCutoff,
    collectFinalPages,
    finalCaption,
    interimCaption,
    silencePosition,
    reviseCaption,
    formatDuration,
    markerBetween,
    liveSilenceState,
    measuredMarker,
    combineSilenceMarkers,
    mergeFinalEvents,
    renderTranscript,
    rowsAfterCutoff,
    silenceMarker,
    spokenDuration,
    validPause,
  };
});
