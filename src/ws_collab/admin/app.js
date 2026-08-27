/* WS_COLLAB operations workbench.
 *
 * Transport: WebSocket preferred, automatic REST long-poll fallback. The durable
 * cursor is preserved across transport switches so no event is missed or shown
 * twice. Streams are rendered (not dumped) with virtualization and a bounded
 * in-browser buffer; "Clear view" only clears the browser, never durable data.
 */
(() => {
"use strict";

/* Resolve the API root.
 *
 * Served by the backend the page lives at <prefix>/admin/, so the prefix is the
 * path with /admin stripped. Under a dev server (Vite) the page is served from
 * the root instead, so fall back to the default mount point. `WS_COLLAB_BASE`
 * overrides both, for embedding under an unusual prefix.
 */
const BASE = (() => {
  if (window.WS_COLLAB_BASE) return String(window.WS_COLLAB_BASE).replace(/\/+$/, "");
  const match = location.pathname.match(/^(.*)\/admin\/?$/);
  if (match) return match[1];
  return "/ws_collab";
})();
/* The API answers at /, /v1, /ws_collab, and /ws_collab/v1, and the admin page
 * is served beneath each of them. When the page is already inside a versioned
 * mount the prefix ends with /v1, so appending it again would ask for the
 * nonexistent /v1/v1. */
const V1 = /\/v1$/.test(BASE) ? BASE : BASE + "/v1";
const ROW_H = 22;
const MAX_BUFFER = 5000;

const state = {
  token: sessionStorage.getItem("ws_collab_token") || "",
  transport: "disconnected",
  ws: null,
  wsReady: false,
  restTimers: {},
  page: "transcript",
  selected: null,
  inspectorTab: "event",
  cursors: {},          // stream -> cursor
  buffers: {},          // stream -> events[]
  seen: {},             // stream -> Set(seq)
  views: {},            // viewId -> view controller
  config: null,
  caps: null,
  bootId: null,         // server boot id at page load; changes => server restarted
  endpoints: null,       // endpoint map from the server
  serverStatus: null,    // last /status rollup
  reconnectTimer: null,
  reconnectAt: 0,
  reconnectDelay: 1000,
  errors: [],
};

// Stream names are NOT hard-coded here: they are discovered from
// /ws_collab/v1/capabilities so the server remains the single source of truth.
// STREAM_ROLES maps behaviour (which streams feed which view) to capability
// flags rather than to literal names, so renaming a stream never breaks the UI.
let STREAMS = [];
let TRANSCRIPT_STREAMS = [];
const ROUTINE_TYPES = new Set([
  "LISTENING_STARTED", "LISTENING_STOPPED", "SPEECH_DETECTED", "WORKER_STATUS",
  "INPUT_MUTED_DURING_TTS", "SECURITY_AUDIT",
]);

function adoptStreams(capabilities) {
  STREAMS = Object.keys((capabilities && capabilities.streams) || {});
  const roles = (capabilities && capabilities.stream_roles) || {};
  TRANSCRIPT_STREAMS = (roles.speech_pipeline || []).filter((s) => STREAMS.includes(s));
  populateStreamMenu();
}

/* The "JSONL Streams" nav item expands into one entry per durable stream, each
 * showing its entry count and going bold when it has unread events. */
function populateStreamMenu() {
  const sub = document.getElementById("nav-streams-sub");
  if (!sub) return;
  sub.replaceChildren();
  STREAMS.forEach((s) => {
    const item = el("div", "nav-subitem");
    item.dataset.stream = s;
    item.tabIndex = 0;
    item.append(el("span", "nav-subitem-label", s), el("span", "nav-subitem-count"));
    const go = () => selectStream(s);
    item.addEventListener("click", (e) => { e.stopPropagation(); go(); });
    item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    sub.appendChild(item);
  });
  renderStreamMenu();
}

function scheduleStreamMenuRender() {
  if (state._streamMenuRaf) return;
  state._streamMenuRaf = requestAnimationFrame(() => { state._streamMenuRaf = null; renderStreamMenu(); });
}

function renderStreamMenu() {
  const sub = document.getElementById("nav-streams-sub");
  if (!sub) return;
  sub.querySelectorAll(".nav-subitem").forEach((item) => {
    const s = item.dataset.stream;
    const stats = (state.streamStats && state.streamStats[s]) || { count: 0, unread: 0 };
    const buffered = (state.buffers[s] || []).length;
    const count = Math.max(stats.count || 0, buffered);
    const countEl = item.querySelector(".nav-subitem-count");
    if (countEl) countEl.textContent = count ? String(count) : "";
    item.classList.toggle("unread", (stats.unread || 0) > 0);
    item.title = `${s} — ${count} entries` + ((stats.unread || 0) > 0 ? `, ${stats.unread} unread` : "");
  });
}

function selectStream(stream) {
  const sel = document.getElementById("st-stream");
  if (sel) {
    sel.value = stream;
    const view = state.views && state.views.streams;
    if (view) { view.streams = [stream]; view.rebuild(state.buffers[stream] || []); }
  }
  if (state.streamStats && state.streamStats[stream]) state.streamStats[stream].unread = 0;
  document.querySelectorAll("#nav-streams-sub .nav-subitem").forEach((n) =>
    n.classList.toggle("active", n.dataset.stream === stream));
  renderStreamMenu();
  showPage("streams");
  if (state.streamMode === "tile") renderTiles();
  if (!TRANSCRIPT_STREAMS.length) TRANSCRIPT_STREAMS = STREAMS.slice(0, 1);
}

function streamForRole(role, fallbackIndex = 0) {
  const roles = (state.caps && state.caps.stream_roles) || {};
  const value = roles[role];
  if (Array.isArray(value)) return value[0];
  return value || STREAMS[fallbackIndex];
}

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const esc = (s) => String(s == null ? "" : s);
const shortTs = (ts) => (ts || "").slice(11, 23);
const fmt = (v) => JSON.stringify(v, null, 2);

function pushError(message) {
  state.errors.unshift(`${new Date().toLocaleTimeString()} ${message}`);
  state.errors = state.errors.slice(0, 20);
  $("sb-errors").textContent = state.errors[0] || "";
}

/* --------------------------------------------------------------- API client */
async function api(path, options = {}) {
  const headers = Object.assign({ "Authorization": `Bearer ${state.token}` }, options.headers || {});
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 304) return null;
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { raw: text }; }
  if (!response.ok) {
    const message = (payload && payload.error && payload.error.message) || response.statusText;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/* ------------------------------------------------------------- event buffer */
function bufferFor(stream) {
  if (!state.buffers[stream]) { state.buffers[stream] = []; state.seen[stream] = new Set(); }
  return state.buffers[stream];
}

function ingest(event) {
  const stream = event.stream;
  const buffer = bufferFor(stream);
  const seen = state.seen[stream];
  const key = event.seq;
  if (seen.has(key)) return false;          // duplicate/replay marker guard
  seen.add(key);
  buffer.push(event);
  if (buffer.length > MAX_BUFFER) {
    const dropped = buffer.splice(0, buffer.length - MAX_BUFFER);
    dropped.forEach((e) => seen.delete(e.seq));
  }
  // Per-stream entry count (from the durable seq) + unread tracking for the menu.
  if (!state.streamStats) state.streamStats = {};
  const stats = state.streamStats[stream] || (state.streamStats[stream] = { count: 0, unread: 0 });
  stats.count = Math.max(stats.count, (event.seq ?? -1) + 1);
  const sel = document.getElementById("st-stream");
  const viewingThis = state.page === "streams" && sel && sel.value === stream;
  if (!viewingThis) stats.unread += 1;
  scheduleStreamMenuRender();
  scheduleTilesRender(stream);
  Object.values(state.views).forEach((view) => view.onEvent(event));
  if (stream === "stt_transcripts") handleSttTranscriptEvent(event);
  return true;
}

/* ------------------------------------------------------------------ transport */

/* The server stamps a unique boot id on every process start (surfaced in
 * capabilities, /status and /diagnostics). The page records the id it loaded
 * against; the first time a (re)connect or poll reports a different one the
 * server has restarted -- possibly with new HTML/JS/CSS -- so we reload and
 * swap in the freshly hosted frame instead of running stale assets. Pages left
 * open through a restart therefore heal themselves without a manual refresh. */
function checkBootId(id) {
  if (!id) return;
  if (!state.bootId) { state.bootId = id; return; }
  if (id !== state.bootId) {
    state.bootId = id;
    setTransport("reloading");
    location.reload();
  }
}

function setTransport(kind) {
  state.transport = kind;
  const badge = $("transport");
  badge.dataset.state = kind === "wss" || kind === "ws" ? kind : (kind === "disconnected" ? "disconnected" : "rest");
  badge.textContent = kind;
  $("sb-transport").textContent = "transport " + kind;
  if (!$("endpoint-popover").hidden) renderEndpoints();
}

function connectWs() {
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.reconnectAt = 0;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  let socket;
  try {
    socket = new WebSocket(`${scheme}://${location.host}${BASE}/ws`);
  } catch (error) {
    scheduleReconnect();
    startRestFallback();
    return;
  }
  state.ws = socket;
  socket.onopen = () => socket.send(JSON.stringify({ type: "auth", token: state.token }));
  socket.onmessage = (message) => {
    let frame;
    try { frame = JSON.parse(message.data); } catch { return; }
    if (frame.type === "auth_ok") {
      state.wsReady = true;
      state.reconnectDelay = 1000;   // healthy again: reset the backoff
      setTransport(scheme);
      stopRestFallback();
      state.caps = frame.capabilities;
      checkBootId(state.caps && state.caps.boot_id);
      adoptStreams(state.caps);
      socket.send(JSON.stringify({ type: "subscribe", streams: STREAMS, cursors: state.cursors }));
    } else if (frame.type === "event") {
      const event = frame.event;
      state.cursors[event.stream] = state.cursors[event.stream] || null;
      ingest(event);
    } else if (frame.type === "caught_up") {
      state.cursors[frame.stream] = frame.cursor;
    } else if (frame.type === "ping") {
      socket.send(JSON.stringify({ type: "pong" }));
    } else if (frame.type === "error") {
      pushError(`ws: ${frame.error ? frame.error.message : "error"}`);
    }
  };
  socket.onclose = () => {
    state.wsReady = false;
    state.ws = null;
    setTransport("disconnected");
    startRestFallback();
    scheduleReconnect();
  };
  socket.onerror = () => { try { socket.close(); } catch {} };
}

/* Exponential backoff so a server that stays down is not hammered; capped so a
 * server that comes back is picked up promptly. "Reconnect now" bypasses it. */
function scheduleReconnect() {
  clearTimeout(state.reconnectTimer);
  const delay = state.reconnectDelay;
  state.reconnectAt = Date.now() + delay;
  state.reconnectDelay = Math.min(delay * 2, 30000);
  state.reconnectTimer = setTimeout(() => {
    if (!state.wsReady) connectWs();
  }, delay);
  if (!$("endpoint-popover").hidden) renderEndpoints();
}

function reconnectNow() {
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.reconnectDelay = 1000;
  state.reconnectAt = 0;
  if (state.ws) { try { state.ws.close(); } catch {} state.ws = null; }
  state.wsReady = false;
  setTransport("connecting");
  connectWs();
  renderEndpoints();
}

function startRestFallback() {
  if (Object.keys(state.restTimers).length) return;
  setTransport(location.protocol === "https:" ? "https rest" : "http rest");
  STREAMS.forEach((stream) => {
    const poll = async () => {
      if (state.wsReady) return;
      try {
        const query = new URLSearchParams({ stream, limit: "200", wait_ms: "20000" });
        if (state.cursors[stream]) query.set("after", state.cursors[stream]);
        const page = await api(`${V1}/events?${query}`);
        if (page) {
          state.cursors[stream] = page.next_cursor;
          page.events.forEach(ingest);
        }
      } catch (error) {
        if (error.status === 401) { logout(); return; }
        pushError(`rest ${stream}: ${error.message}`);
        await new Promise((r) => setTimeout(r, 3000));
      }
      if (!state.wsReady) state.restTimers[stream] = setTimeout(poll, 50);
    };
    state.restTimers[stream] = setTimeout(poll, 0);
  });
}

function stopRestFallback() {
  Object.values(state.restTimers).forEach(clearTimeout);
  state.restTimers = {};
}

/* ------------------------------------------------------- virtualized stream view */
function createView(opts) {
  const scroll = $(opts.scrollId);
  const spacer = $(opts.spacerId);
  const viewport = $(opts.viewportId);
  const jump = $(opts.jumpId);
  const unseenLabel = opts.unseenId ? $(opts.unseenId) : null;

  const view = {
    id: opts.id,
    streams: opts.streams,
    rows: [],
    paused: false,
    follow: true,
    unseen: 0,
    hidden: 0,
    filter: opts.filter || (() => true),
    render: opts.render || defaultRender,
    pending: [],

    onEvent(event) {
      if (!this.streams.includes(event.stream)) return;
      if (this.paused) { this.pending.push(event); if (this.pending.length > MAX_BUFFER) this.pending.shift(); return; }
      this.append(event);
    },
    append(event) {
      if (!this.filter(event)) { this.hidden += 1; this.updateCounts(); return; }
      this.rows.push(event);
      if (this.rows.length > MAX_BUFFER) this.rows.shift();
      if (this.follow) { this.draw(); this.scrollToEnd(); }
      else { this.unseen += 1; jump.classList.add("visible"); if (unseenLabel) unseenLabel.textContent = this.unseen; this.draw(); }
      this.updateCounts();
    },
    rebuild(source) {
      this.rows = source.filter(this.filter);
      this.hidden = source.length - this.rows.length;
      this.draw();
      this.updateCounts();
    },
    updateCounts() {
      if (opts.countsId) $(opts.countsId).textContent = `${this.rows.length} shown · ${this.hidden} hidden`;
    },
    draw() {
      const cap = opts.capFn ? opts.capFn() : MAX_BUFFER;
      const rows = this.rows.slice(-cap);
      spacer.style.height = `${rows.length * ROW_H}px`;
      const top = scroll.scrollTop;
      const height = scroll.clientHeight || 400;
      const first = Math.max(0, Math.floor(top / ROW_H) - 8);
      const last = Math.min(rows.length, Math.ceil((top + height) / ROW_H) + 8);
      viewport.style.transform = `translateY(${first * ROW_H}px)`;
      viewport.replaceChildren();
      for (let index = first; index < last; index += 1) {
        viewport.appendChild(this.render(rows[index]));
      }
    },
    scrollToEnd() { scroll.scrollTop = scroll.scrollHeight; },
    setPaused(value) {
      this.paused = value;
      if (!value) { this.pending.forEach((e) => this.append(e)); this.pending = []; }
    },
    clearView() { this.rows = []; this.hidden = 0; this.draw(); this.updateCounts(); },
  };

  scroll.addEventListener("scroll", () => {
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
    // Automatic pause when the operator scrolls upward; never force the view down.
    view.follow = atBottom;
    if (atBottom) { view.unseen = 0; jump.classList.remove("visible"); }
    view.draw();
  });
  jump.addEventListener("click", () => {
    view.follow = true; view.unseen = 0; jump.classList.remove("visible");
    view.draw(); view.scrollToEnd();
  });

  state.views[opts.id] = view;
  return view;
}

function eventSummary(event) {
  const data = event.data || {};
  if (event.type === "HEARD_SPEECH") {
    const resolved = (data.resolved && data.resolved.resolved_text) || "";
    const cls = data.classification || {};
    return { text: resolved, cls: cls.is_echo ? "filtered-text" : "resolved-text", extra: cls.source };
  }
  if (event.type === "STT_PARTIAL_RESULT") return { text: data.raw_text || "", cls: "partial-text", extra: data.engine };
  if (event.type === "STT_FINAL_RESULT") return { text: data.raw_text || "", cls: "", extra: `${data.engine} ${(data.confidence ?? "")}` };
  if (event.type === "TRANSCRIPT_RESOLVED") return { text: data.resolved_text || "", cls: "resolved-text", extra: data.method };
  if (event.type === "TRANSCRIPT_FILTERED") return { text: data.reason || "", cls: "filtered-text", extra: "" };
  if (event.type === "STT_ENGINE_ERROR") return { text: data.error || "", cls: "error-text", extra: data.engine };
  if (event.type === "CONVERSATION_MESSAGE") return { text: data.text || "", cls: "", extra: event.source_id };
  if (event.type === "TTS_TRANSCRIPTION_EVALUATED") {
    const wer = data.final ? data.final.wer : "";
    return { text: `WER ${wer} (best engine ${data.best_engine_wer})`, cls: "", extra: "accuracy" };
  }
  if (event.type === "ALERT_RAISED") return { text: data.message || `${data.worker_id || ""} ${data.state || ""}`, cls: "error-text", extra: data.severity };
  if (event.type && event.type.startsWith("TTS_")) return { text: data.text || data.id || "", cls: "", extra: data.agent_id };
  return { text: JSON.stringify(data).slice(0, 160), cls: "", extra: "" };
}

function defaultRender(event) {
  const row = el("div", "event-row");
  row.dataset.source = event.source_kind || "system";
  const summary = eventSummary(event);
  if (event.type === "TTS_AUDIO_DETECTED_BY_MICROPHONE" || event.type === "TRANSCRIPT_FILTERED") row.dataset.echo = "true";
  row.appendChild(el("span", "ev-ts", shortTs(event.ts)));
  row.appendChild(el("span", "ev-seq", String(event.seq ?? "")));
  row.appendChild(el("span", "ev-type", event.type));
  const text = el("span", `ev-text ${summary.cls}`, summary.text);
  row.appendChild(text);
  row.appendChild(el("span", "ev-src", summary.extra || event.source_id || ""));
  row.addEventListener("click", () => selectEvent(event, row));
  if (state.selected && state.selected.id === event.id) row.classList.add("selected");
  return row;
}

function selectEvent(event, row) {
  state.selected = event;
  document.querySelectorAll(".event-row.selected").forEach((n) => n.classList.remove("selected"));
  if (row) row.classList.add("selected");
  renderInspector();
}

/* -------------------------------------------------- tile / chat-bubble mode */

/* Minimal, safe GitHub-flavored-markdown renderer for message bodies. All user
 * content is HTML-escaped first; only our own generated tags are emitted, and
 * link URLs are restricted to http/https/mailto. Covers headings, bold/italic/
 * strikethrough, inline code, fenced code blocks, links, lists, blockquotes,
 * and horizontal rules -- enough to match the workspace markdown viewer for
 * chat-style messages. */
function mdEscape(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function mdInline(text) {
  const codes = [];
  text = text.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
    /^https?:/i.test(url) ? `<img src="${url}" alt="${alt}">` : m);
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) =>
    /^(https?:|mailto:|#)/i.test(url)
      ? `<a href="${url}"${/^https?:/i.test(url) ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`
      : m);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  text = text.replace(/(^|[^_\w])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  text = text.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  return text;
}

function mdToHtml(raw) {
  const lines = String(raw == null ? "" : raw).split(/\r?\n/);
  const out = [];
  let para = [];
  let i = 0;
  const flush = () => { if (para.length) { out.push(`<p>${para.join("<br>")}</p>`); para = []; } };
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flush();
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre><code>${mdEscape(buf.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) { flush(); i += 1; continue; }
    // GFM table: a header row followed by a |---|---| separator.
    if (line.includes("|") && i + 1 < lines.length
        && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      const header = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) { body.push(cells(lines[i])); i += 1; }
      let t = `<table><thead><tr>${header.map((c) => `<th>${mdInline(mdEscape(c))}</th>`).join("")}</tr></thead><tbody>`;
      t += body.map((r) => `<tr>${r.map((c) => `<td>${mdInline(mdEscape(c))}</td>`).join("")}</tr>`).join("");
      out.push(`${t}</tbody></table>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flush(); out.push(`<h${h[1].length}>${mdInline(mdEscape(h[2]))}</h${h[1].length}>`); i += 1; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); i += 1; continue; }
    if (/^>\s?/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      out.push(`<blockquote>${buf.map((l) => mdInline(mdEscape(l))).join("<br>")}</blockquote>`);
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      flush();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, "")); i += 1; }
      out.push(`<ul>${items.map((t) => {
        const task = t.match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          const checked = task[1].toLowerCase() === "x" ? " checked" : "";
          return `<li class="task-list-item"><input type="checkbox" disabled${checked}> ${mdInline(mdEscape(task[2]))}</li>`;
        }
        return `<li>${mdInline(mdEscape(t))}</li>`;
      }).join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      flush();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i += 1; }
      out.push(`<ol>${items.map((t) => `<li>${mdInline(mdEscape(t))}</li>`).join("")}</ol>`);
      continue;
    }
    para.push(mdInline(mdEscape(line)));
    i += 1;
  }
  flush();
  return out.join("\n");
}

/* Render into a container carrying the workbench's own markdown classes, so the
 * result is styled identically to the workspace markdown viewer (help_tabs.css
 * .markdown-body, reused verbatim in app.css). */
function renderMarkdown(text, extraClass) {
  const div = el("div", `markdown-body${extraClass ? " " + extraClass : ""}`);
  div.innerHTML = mdToHtml(text);
  return div;
}

/* MeTTa codec — ported verbatim from the workbench's
 * frontend/src/lib/mettaResourceCodec.ts (jsonValueToMetta) so the MeTTa view
 * here renders identically to the workspace. */
const METTA_SAFE_ATOM = /^[^\s(){}";\\]+$/;
const METTA_TYPED_ATOM = /^(?:true|false|null|-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)$/i;
const METTA_EMBEDDED = "__metta_json_string_parts__";

function mettaQuote(value, force) {
  if (force) return JSON.stringify(value);
  return METTA_SAFE_ATOM.test(value) && value !== "{}" && !METTA_TYPED_ATOM.test(value) ? value : JSON.stringify(value);
}
function mettaSingleQuote(value) {
  return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t")}'`;
}
function mettaEmbeddedParts(value) {
  const parts = []; let cursor = 0; let scan = 0; let found = false;
  while (scan < value.length) {
    if (value[scan] !== "{" && value[scan] !== "[") { scan += 1; continue; }
    const start = scan; const stack = []; let quoted = false; let escaped = false; let end = -1;
    for (; scan < value.length; scan += 1) {
      const ch = value[scan];
      if (quoted) { if (escaped) escaped = false; else if (ch === "\\") escaped = true; else if (ch === '"') quoted = false; continue; }
      if (ch === '"') { quoted = true; continue; }
      if (ch === "{" || ch === "[") stack.push(ch);
      else if (ch === "}" || ch === "]") { const op = stack.pop(); if ((op === "{" && ch !== "}") || (op === "[" && ch !== "]")) break; if (!stack.length) { end = scan + 1; break; } }
    }
    if (end < 0) { scan = start + 1; continue; }
    try {
      const parsed = JSON.parse(value.slice(start, end));
      if (parsed === null || typeof parsed !== "object") { scan = start + 1; continue; }
      if (start > cursor) parts.push(value.slice(cursor, start));
      parts.push(parsed); found = true; cursor = end; scan = end;
    } catch { scan = start + 1; }
  }
  if (!found) return undefined;
  if (cursor < value.length) parts.push(value.slice(cursor));
  return parts;
}
function mettaFmtEmbeddedItem(value) {
  const parts = mettaEmbeddedParts(value);
  if (parts === undefined || !parts.some((p) => typeof p !== "string")) return undefined;
  const lines = [""];
  parts.forEach((part) => {
    if (typeof part === "string") { lines[lines.length - 1] += part; return; }
    const pretty = JSON.stringify(part, null, 2).split("\n");
    lines[lines.length - 1] += pretty[0];
    pretty.slice(1).forEach((l) => lines.push(l));
  });
  return [JSON.stringify(lines[0]), ...lines.slice(1).map((l) => mettaSingleQuote(l))];
}
function mettaSplitLongSentence(value, minimumPrefix) {
  minimumPrefix = minimumPrefix || 50;
  if (value.length <= minimumPrefix || /\r|\n/.test(value)) return undefined;
  const lines = []; let remaining = value;
  while (remaining.length > minimumPrefix) {
    const boundary = /[A-Za-z][.!?]\s+/g; let splitAt = -1; let m;
    while ((m = boundary.exec(remaining)) !== null) { const b = m.index + m[0].length; if (b >= minimumPrefix) { splitAt = b; break; } }
    if (splitAt < 0) break;
    lines.push(remaining.slice(0, splitAt)); remaining = remaining.slice(splitAt);
  }
  if (!lines.length) return undefined;
  lines.push(remaining); return lines;
}
function mettaFmtLongSentence(value) {
  const lines = mettaSplitLongSentence(value);
  if (!lines || lines.length <= 1) return undefined;
  return [JSON.stringify(lines[0]), ...lines.slice(1).map((l) => mettaSingleQuote(l))];
}
function jsonValueToMetta(value, depth, forceQuoteString) {
  depth = depth || 0;
  const indent = "  ".repeat(depth);
  const childIndent = "  ".repeat(depth + 1);
  if (value === null) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  if (typeof value === "string") {
    if (forceQuoteString) return mettaQuote(value, true);
    const embedded = mettaEmbeddedParts(value);
    return embedded === undefined ? mettaQuote(value) : jsonValueToMetta({ [METTA_EMBEDDED]: embedded }, depth, false);
  }
  if (Array.isArray(value)) {
    if (!value.length) return "([])";
    if (value.every((it) => typeof it === "number")) return `([] ${value.map((it) => String(it)).join(" ")})`;
    const quoteStringItems = value.some((it) => typeof it === "string" && /\s/.test(it));
    const items = value.flatMap((it) => {
      if (quoteStringItems && typeof it === "string") {
        const f = mettaFmtEmbeddedItem(it); if (f) return f.map((l) => `${childIndent}${l}`);
        const w = mettaFmtLongSentence(it); if (w) return w.map((l) => `${childIndent}${l}`);
      }
      return [`${childIndent}${jsonValueToMetta(it, depth + 1, quoteStringItems && typeof it === "string")}`];
    });
    return `([]\n${items.join("\n")}\n${indent})`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return "()";
    const items = entries.map(([k, it]) => `${childIndent}(${mettaQuote(k)} ${jsonValueToMetta(it, depth + 1, false)})`);
    return `(\n${items.join("\n")}\n${indent})`;
  }
  return String(value);
}

/* Cached conversions come from the shared server-side codec service
 * (POST /convert). The ported client codec is only an instant fallback shown
 * while the authoritative server result loads. */
function convCacheGet(format, id) {
  return state.convCache && state.convCache[format] ? state.convCache[format][id] : undefined;
}
function convCacheSet(format, id, text) {
  if (!state.convCache) state.convCache = {};
  if (!state.convCache[format]) state.convCache[format] = {};
  state.convCache[format][id] = text;
}
async function ensureConversions(events, format) {
  const pending = events.filter((e) => e && e.id && convCacheGet(format, e.id) === undefined);
  if (!pending.length) return;
  pending.forEach((e) => convCacheSet(format, e.id, null));   // mark in-flight
  try {
    const res = await api(`${V1}/convert`, {
      method: "POST",
      body: { to: format, items: pending.map((e) => ({ id: e.id, value: e.data || {} })) },
    });
    (res.results || []).forEach((r) => convCacheSet(format, r.id, r.text));
  } catch (error) {
    pending.forEach((e) => convCacheSet(format, e.id, `(conversion failed: ${error.message})`));
  }
  if (state.streamMode === "tile" && state.page === "streams") renderTiles();
}

/* Render one event's body in the selected representation. */
function renderBubbleBody(event, format) {
  const data = event.data || {};
  if (format === "json") {
    const pre = el("pre", "bubble-code");
    pre.textContent = JSON.stringify(data, null, 2);
    return pre;
  }
  if (format === "metta") {
    const pre = el("pre", "bubble-code");
    const cached = convCacheGet("metta", event.id);
    if (cached !== undefined && cached !== null) pre.textContent = cached;
    else { try { pre.textContent = jsonValueToMetta(data); } catch { pre.textContent = JSON.stringify(data, null, 2); } }
    return pre;
  }
  const summary = eventSummary(event);
  if (format === "text") {
    const div = el("div", "bubble-body");
    div.textContent = summary.text || JSON.stringify(data);
    return div;
  }
  const md = renderMarkdown(summary.text || JSON.stringify(data), "bubble-body");
  if (summary.cls) md.classList.add(summary.cls);
  return md;
}

/* An alternative to the dense list: each event is a chat bubble (like the
 * workbench mailbox reader). Operator/agent messages align right, everything
 * else left. Bounded to the most recent events for performance. */
function bubbleFor(event) {
  const data = event.data || {};
  const summary = eventSummary(event);
  const kind = event.source_kind || "system";
  const wrap = el("div", `bubble-row bubble-${kind}`);
  const bubble = el("div", "bubble");
  if (state.selected && state.selected.id === event.id) bubble.classList.add("selected");
  const head = el("div", "bubble-head");
  head.append(
    el("span", "bubble-src", event.source_id || kind),
    el("span", "bubble-type", event.type),
    el("span", "bubble-ts", shortTs(event.ts)),
  );
  bubble.appendChild(head);
  const format = state.streamFormat || "markdown";
  bubble.appendChild(renderBubbleBody(event, format));
  if (summary.extra && (format === "markdown" || format === "text")) {
    bubble.appendChild(el("div", "bubble-meta", String(summary.extra)));
  }
  bubble.addEventListener("click", () => { selectEvent(event, null); renderTiles(); });
  wrap.appendChild(bubble);
  return wrap;
}

function renderTiles() {
  const tiles = document.getElementById("st-tiles");
  if (!tiles) return;
  const stream = document.getElementById("st-stream").value;
  const view = state.views && state.views.streams;
  const all = state.buffers[stream] || [];
  const filtered = view ? all.filter((e) => view.filter(e)) : all;
  const shown = filtered.slice(-300);
  const atBottom = tiles.scrollHeight - tiles.scrollTop - tiles.clientHeight < 60;
  tiles.replaceChildren();
  shown.forEach((e) => tiles.appendChild(bubbleFor(e)));
  if ((state.streamFormat || "markdown") === "metta") ensureConversions(shown, "metta");
  if (atBottom) tiles.scrollTop = tiles.scrollHeight;
}

function scheduleTilesRender(stream) {
  if (state.streamMode !== "tile" || state.page !== "streams") return;
  if (stream !== document.getElementById("st-stream").value) return;
  if (state._tilesRaf) return;
  state._tilesRaf = requestAnimationFrame(() => { state._tilesRaf = null; renderTiles(); });
}

function setStreamMode(mode) {
  state.streamMode = mode;
  const tile = mode === "tile";
  const scroll = document.getElementById("st-scroll");
  const tiles = document.getElementById("st-tiles");
  const jump = document.getElementById("st-jump");
  const btn = document.getElementById("st-mode");
  if (scroll) scroll.style.display = tile ? "none" : "";
  if (tiles) tiles.style.display = tile ? "" : "none";
  if (jump && tile) jump.classList.remove("visible");
  if (btn) btn.textContent = tile ? "List view" : "Tile view";
  if (tile) renderTiles();
  else if (state.views && state.views.streams) state.views.streams.draw();
}

/* ------------------------------------------------------- endpoint inspector */

/* The URL tree is built from the server's own endpoint map (/v1/endpoints), so
 * a changed prefix, port, or scheme can never leave this view lying. */
function endpointStatus(endpoint) {
  if (endpoint.kind === "ws") {
    if (state.wsReady) return { dot: "ok", label: "connected" };
    if (state.transport === "connecting") return { dot: "warn", label: "connecting" };
    return { dot: "danger", label: "disconnected" };
  }
  if (endpoint.id === "admin") return { dot: "ok", label: "this page" };
  const server = state.serverStatus;
  if (endpoint.id === "status" && server) {
    const dot = { ok: "ok", degraded: "warn", down: "danger" }[server.status] || "idle";
    return { dot, label: server.status };
  }
  if (endpoint.id === "rest") {
    const polling = Object.keys(state.restTimers).length > 0;
    if (polling) return { dot: "ok", label: "active (fallback)" };
    return { dot: state.serverStatus ? "ok" : "idle", label: state.serverStatus ? "reachable" : "unknown" };
  }
  return { dot: state.serverStatus ? "ok" : "idle", label: state.serverStatus ? "reachable" : "unknown" };
}

function renderEndpoints() {
  const tree = $("endpoint-tree");
  tree.replaceChildren();
  const map = state.endpoints;
  if (!map) {
    tree.appendChild(el("div", "hint", "Endpoint map unavailable — the server could not be reached."));
  } else {
    const origin = map.origin || location.origin;
    const rootRow = el("div", "endpoint-row");
    rootRow.appendChild(el("span", "endpoint-url", origin));
    rootRow.appendChild(el("span", "endpoint-kind", map.tls ? "tls" : "no tls"));
    tree.appendChild(rootRow);

    const baseRow = el("div", "endpoint-row");
    baseRow.appendChild(el("span", "endpoint-branch", "└─"));
    baseRow.appendChild(el("span", "endpoint-url", map.base));
    tree.appendChild(baseRow);

    const items = map.endpoints || [];
    items.forEach((endpoint, index) => {
      const last = index === items.length - 1;
      const row = el("div", "endpoint-row");
      row.appendChild(el("span", "endpoint-branch", `   ${last ? "└─" : "├─"}`));
      const status = endpointStatus(endpoint);
      row.appendChild(el("span", `endpoint-dot ${status.dot}`, "\u25cf"));

      const shortPath = endpoint.path.startsWith(map.base)
        ? endpoint.path.slice(map.base.length) || "/"
        : endpoint.path;
      if (endpoint.kind === "http") {
        const link = el("a", "endpoint-url clickable", shortPath);
        link.href = endpoint.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.title = `${endpoint.url}\n${endpoint.description}`;
        row.appendChild(link);
      } else {
        const span = el("span", "endpoint-url", shortPath);
        span.title = `${endpoint.url}\n${endpoint.description}`;
        row.appendChild(span);
      }
      row.appendChild(el("span", "endpoint-state", status.label));
      row.appendChild(el("span", "endpoint-kind", endpoint.kind === "ws" ? "ws" : endpoint.auth));
      tree.appendChild(row);
    });
  }

  const retry = $("endpoint-retry");
  if (state.wsReady) {
    retry.textContent = "";
  } else if (state.reconnectAt) {
    const seconds = Math.max(0, Math.round((state.reconnectAt - Date.now()) / 1000));
    retry.textContent = `auto-retry in ${seconds}s`;
  } else {
    retry.textContent = state.transport === "connecting" ? "connecting…" : "";
  }
}

async function refreshEndpointData() {
  try {
    const [map, status] = await Promise.all([
      api(`${V1}/endpoints`).catch(() => state.endpoints),
      fetch(`${BASE}/status`).then((r) => r.json()).catch(() => null),
    ]);
    if (map) state.endpoints = map;
    if (status) { state.serverStatus = status; checkBootId(status.boot_id); }
  } catch (error) {
    state.serverStatus = null;
  }
  renderEndpoints();
}

function toggleEndpointPopover(force) {
  const popover = $("endpoint-popover");
  const open = force !== undefined ? force : popover.hidden;
  popover.hidden = !open;
  $("transport").setAttribute("aria-expanded", String(open));
  if (open) {
    renderEndpoints();
    refreshEndpointData();
    if (!state.endpointTicker) {
      state.endpointTicker = setInterval(renderEndpoints, 1000);
    }
  } else if (state.endpointTicker) {
    clearInterval(state.endpointTicker);
    state.endpointTicker = null;
  }
}

/* -------------------------------------------------------------- inspector */
function renderInspector() {
  const target = $("insp-content");
  const event = state.selected;
  const tab = state.inspectorTab;
  if (tab === "raw") { target.textContent = event ? fmt(event) : "No event selected."; return; }
  if (tab === "event") {
    if (!event) { target.textContent = "Select an event to inspect it."; return; }
    target.textContent = [
      `id            ${event.id}`,
      `stream        ${event.stream}`,
      `seq           ${event.seq}`,
      `type          ${event.type}`,
      `ts            ${event.ts}`,
      `source        ${event.source_id} (${event.source_kind})`,
      `correlation   ${event.correlation_id || "—"}`,
      `schema        v${event.schema_version}`,
      "",
      fmt(event.data),
    ].join("\n");
    return;
  }
  if (tab === "transcript") {
    const data = event && event.data ? event.data : {};
    const resolved = data.resolved || (event && event.type === "TRANSCRIPT_RESOLVED" ? data : null);
    if (!resolved) { target.textContent = "Select a HEARD_SPEECH or TRANSCRIPT_RESOLVED event."; return; }
    const lines = [`resolved: ${resolved.resolved_text}`, `method:   ${resolved.method}`,
      `conf:     ${resolved.confidence}`, `agreement:${resolved.engine_agreement}`,
      `uncertain:${resolved.uncertain}`, "", "hypotheses:"];
    (resolved.raw_hypotheses || []).forEach((h) => {
      lines.push(`  ${h.engine.padEnd(18)} ${String(h.confidence).padEnd(6)} ${h.latency_ms}ms  ${h.error ? "ERR " + h.error : h.raw_text}`);
    });
    target.textContent = lines.join("\n");
    return;
  }
  if (tab === "routing") { loadInto(target, `${V1}/audio/routing`); return; }
  if (tab === "agent") { loadInto(target, `${V1}/voices`); return; }
  if (tab === "prompt") { loadInto(target, `${V1}/prompt`); return; }
  if (tab === "audit") { loadInto(target, `${V1}/audit?limit=50`); return; }
  if (tab === "docs") {
    target.textContent = [
      "WS_COLLAB — operations quick reference",
      "",
      "Transports  REST (/ws_collab/v1) and WS (/ws_collab/ws) are at full parity.",
      "Cursors     Opaque, per stream+consumer. Rewind replays; forward skips.",
      "Streams     conversation, worker_statuses, translated_audio, stt_transcripts,",
      "            tts_queue, audio_*, system_*, prompt.",
      "Echo        System TTS captured by the microphone is tagged, kept for",
      "            diagnostics, and excluded from operator-command execution.",
      "State       All writable data lives in collab_state/.",
      "",
      "This view never loads a whole stream into memory: it renders a bounded,",
      "virtualized buffer. 'Clear view' clears the browser only.",
    ].join("\n");
  }
}

async function loadInto(target, path) {
  target.textContent = "loading…";
  try { target.textContent = fmt(await api(path)); }
  catch (error) { target.textContent = `error: ${error.message}`; }
}

/* ------------------------------------------------------------------- pages */
function showPage(page) {
  state.page = page;
  document.querySelectorAll(".page").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  $("top-title").textContent = page.charAt(0).toUpperCase() + page.slice(1);
  // Keep the URL in sync so any page can be deep-linked and reloaded in place.
  if (location.hash.slice(1) !== page) history.replaceState(null, "", `#${page}`);
  const loaders = {
    workers: loadWorkers, alerts: loadAlerts, devices: loadDevices, voices: loadVoices,
    accuracy: loadAccuracy, cursors: loadCursors, prompt: loadPrompt, system: loadSystem, stt: loadStt,
  };
  if (loaders[page]) loaders[page]();
  Object.values(state.views).forEach((v) => v.draw());
}

function pageFromHash() {
  const requested = location.hash.slice(1);
  return document.querySelector(`.nav-item[data-page="${requested}"]`) ? requested : "transcript";
}

/* ---- workers */
async function loadWorkers() {
  const body = $("wk-body");
  try {
    const data = await api(`${V1}/workers`);
    $("badge-workers").textContent = data.workers.length;
    $("sb-workers").textContent = `workers ${data.workers.length}`;
    const bad = data.workers.filter((w) => w.state === "overdue" || w.state === "unresponsive").length;
    $("badge-workers").classList.toggle("alert", bad > 0);
    body.replaceChildren(table(
      ["Worker", "Task", "State", "Last status", "Age", "Errors", "Last conversation", "Actions"],
      data.workers.map((w) => [
        mono(w.worker_id), w.task || "—",
        badge(w.state, { ok: "ok", warn: "warn", overdue: "warn", unresponsive: "danger", terminated: "" }[w.state] || ""),
        w.last_status || "—", `${w.last_status_age_seconds}s`,
        String((w.errors || []).length), mono(w.last_conversation_id || "—"),
        actionButton("Confirm terminated", "danger", async () => {
          if (!confirm(`Confirm ${w.worker_id} is terminated? Only do this when independently verified.`)) return;
          await api(`${V1}/workers/${encodeURIComponent(w.worker_id)}/status`, { method: "POST", body: { status: "terminated_confirmed" } });
          loadWorkers();
        }),
      ])));
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

/* ---- alerts */
async function loadAlerts() {
  const body = $("al-body");
  try {
    const data = await api(`${V1}/alerts?limit=200`);
    const events = data.events.slice().reverse();
    $("badge-alerts").textContent = events.filter((e) => e.type === "ALERT_RAISED").length;
    body.replaceChildren(table(["Time", "Type", "Scope", "Severity", "Detail"],
      events.map((e) => [shortTs(e.ts), e.type, e.data.worker_id || e.data.scope || "—",
        badge(e.data.severity || "—", e.data.severity === "danger" ? "danger" : "warn"),
        mono(JSON.stringify(e.data).slice(0, 120))])));
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

/* ---- devices & routing */

/* Devices are classified on two independent axes so the filters compose:
 *   direction -- can it capture (input) or play (output)?
 *   class     -- real hardware (physical), system capture (loopback), or a
 *                software endpoint (virtual)?
 * Anything the backend could not place on either axis is "unknown".
 */
function deviceGroup(device) {
  if (device.direction === "input" || device.direction === "loopback") return "input";
  if (device.direction === "output") return "output";
  return "unknown";
}

function deviceClass(device) {
  if (device.direction === "loopback") return "loopback";
  if (device.direction === "virtual") return "virtual";
  if (device.direction === "input" || device.direction === "output") return "physical";
  return "unknown";
}

/* ---- device category filters: tri-state show / hide / filtered ---- */

const DEVICE_FILTER_DEFAULTS = {
  inputs: "show", outputs: "show", physical: "show", loopback: "show",
  virtual: "show", unknown: "hide", disabled: "hide",
};
const DEVICE_FILTER_STATES = ["hide", "show", "neutral"];

function catState(cat) {
  return localStorage.getItem(`ws_collab_devfilter_${cat}`) || DEVICE_FILTER_DEFAULTS[cat] || "show";
}

function wireCatFilter(button, onChange) {
  const cat = button.dataset.cat;
  button.dataset.state = catState(cat);
  button.title = `${cat}: click to cycle hide → show → neutral`;
  button.addEventListener("click", () => {
    const cur = DEVICE_FILTER_STATES.indexOf(button.dataset.state || "show");
    const next = DEVICE_FILTER_STATES[(cur + 1) % DEVICE_FILTER_STATES.length];
    button.dataset.state = next;
    localStorage.setItem(`ws_collab_devfilter_${cat}`, next);
    onChange();
  });
}

/* A device sits on several categories at once (direction, class, availability). */
function deviceCategories(device) {
  const group = deviceGroup(device);
  const klass = deviceClass(device);
  const cats = [];
  if (group === "input") cats.push("inputs");
  if (group === "output") cats.push("outputs");
  if (group === "unknown" || klass === "unknown") cats.push("unknown");
  if (klass === "physical") cats.push("physical");
  if (klass === "loopback") cats.push("loopback");
  if (klass === "virtual") cats.push("virtual");
  if (!device.available) cats.push("disabled");
  return cats;
}

/* Resolve the tri-state filters to one verdict per device. The strictest state
 * wins: hide > filtered > show. "filtered" keeps the row visible but dimmed and
 * tagged with why. A non-empty search box always hides the rows it does not
 * match. Returns { state, reason }. */
function deviceVerdict(device) {
  let state = "show";
  let reason = "";
  for (const cat of deviceCategories(device)) {
    const s = catState(cat);
    if (s === "hide") return { state: "hide", reason: `${cat} hidden` };
    if (s === "neutral" && state === "show") { state = "neutral"; reason = `${cat} neutral`; }
  }
  const needle = ($("dv-search").value || "").trim().toLowerCase();
  if (needle) {
    const hay = `${device.name} ${device.host_api} ${device.id} ${device.direction}`.toLowerCase();
    if (!hay.includes(needle)) return { state: "hide", reason: "no text match" };
  }
  return { state, reason };
}

async function loadDevices() {
  try {
    const [devices, capture, routing] = await Promise.all([
      api(`${V1}/audio/devices`), api(`${V1}/audio/capture`), api(`${V1}/audio/routing`),
    ]);

    const capPanel = panel("Capture state");
    capPanel.content.appendChild(kv({
      listening: capture.listening, privacy: capture.privacy_indicator,
      device: capture.device_name || capture.device_id,
      backend: capture.backend, live_capture: capture.live_capture,
      echo_policy: capture.echo_policy, meter: capture.meter_level, peak: capture.peak_level,
      clipping: capture.clipping, captured: capture.captured,
      dropped_echo: capture.dropped_echo, dropped_frames: capture.dropped_frames,
      error: capture.error || "—",
    }));
    $("dv-capture").replaceChildren(capPanel.root);

    const all = devices.devices || [];
    const decorated = all.map((d) => {
      const verdict = deviceVerdict(d);
      return { device: d, state: verdict.state, reason: verdict.reason };
    });
    const shownCount = decorated.filter((r) => r.state === "show").length;
    const neutralCount = decorated.filter((r) => r.state === "neutral").length;
    const visible = decorated.filter((r) => r.state !== "hide");
    $("dv-counts").textContent =
      `${shownCount} shown · ${neutralCount} neutral · ${all.length - visible.length} hidden`;

    const target = $("dv-table");
    target.replaceChildren();
    if (!visible.length) {
      target.appendChild(el("div", "hint", "No devices match the current filters."));
    } else {
      const rows = visible.map(({ device: d, state, reason }) => [
        mono(d.id), d.name,
        badge(d.direction, { input: "teal", loopback: "purple", output: "" }[d.direction] || "warn"),
        badge(deviceClass(d), deviceClass(d) === "physical" ? "ok" : ""),
        mono(d.host_api || "—"), String(d.channels),
        mono((d.sample_rates || []).join(",")),
        mono(d.latency_ms != null ? `${d.latency_ms}ms` : "—"),
        [d.is_default_input && "in", d.is_default_output && "out", d.is_default_comm && "comm"]
          .filter(Boolean).join("/") || "—",
        badge(d.available ? "yes" : "no", d.available ? "ok" : "danger"),
        state === "neutral"
          ? badge(reason, "warn")
          : (deviceGroup(d) === "input" && d.available
              ? actionButton("Use", "", async () => {
                  if (!confirm(`Switch the active capture device to "${d.name}"? Listening will restart on this device.`)) return;
                  try { await api(`${V1}/audio/capture/start`, { method: "POST", body: { device_id: d.id } }); }
                  catch (error) { pushError(error.message); }
                  loadDevices();
                })
              : ((d.direction === "output" || d.direction === "virtual") && d.available
                  ? actionButton("Test", "", async () => {
                      try { await api(`${V1}/audio/devices/test`, { method: "POST", body: { device_id: d.id } }); }
                      catch (error) { pushError(error.message); }
                    })
                  : "—")),
      ]);
      const table_ = table(
        ["ID", "Name", "Direction", "Class", "Host API", "Ch", "Rates", "Latency", "Default", "Available", "Select"],
        rows);
      // Mark neutral rows so it is obvious they are passive, not selectable.
      const bodyRows = table_.querySelectorAll("tbody tr");
      visible.forEach((row, index) => {
        if (row.state === "neutral" && bodyRows[index]) bodyRows[index].classList.add("filtered-out");
      });
      target.appendChild(table_);
    }

    // ---- one row per STT engine, each pointing at a chosen input device
    const [engines, defaults] = await Promise.all([
      api(`${V1}/audio/engines`), api(`${V1}/audio/defaults`),
    ]);
    const captureDevices = all.filter((d) => ["input", "loopback", "virtual"].includes(d.direction) && d.available);
    const enginePanel = panel("Speech recognition engines — choose the input each one listens on");
    enginePanel.content.appendChild(table(
      ["Engine", "Model", "Input device", "In use", "Locality"],
      (engines.engines || []).map((row) => {
        const select = el("select");
        const auto = new Option("(follow active capture device)", "");
        select.appendChild(auto);
        captureDevices.forEach((d) => {
          const option = new Option(`${d.name} · ${d.host_api} [${d.direction}]`, d.id);
          select.appendChild(option);
        });
        select.value = row.device_id || "";
        select.onchange = async () => {
          try {
            await api(`${V1}/audio/engines/${encodeURIComponent(row.engine)}/device`,
                      { method: "POST", body: { device_id: select.value } });
          } catch (error) { pushError(error.message); }
          loadDevices();
        };
        return [
          mono(row.engine),
          mono(row.model || "—"),
          select,
          mono(row.effective_device_name || "—"),
          badge(row.is_remote ? "remote" : "local", row.is_remote ? "warn" : "ok"),
        ];
      })));
    enginePanel.content.appendChild(el("div", "hint",
      "Engines left on “follow active capture device” use whichever device capture is running on. " +
      "Pointing an engine at a loopback device marks it diagnostic and TTS-accuracy only, never a source of commands."));
    $("dv-engines").replaceChildren(enginePanel.root);

    // ---- default output device agents speak through
    const outputs = all.filter((d) => ["output", "virtual"].includes(d.direction) && d.available);
    const defPanel = panel("Default agent output device");
    const defRow = el("div", "toolbar");
    const outSelect = el("select");
    outSelect.appendChild(new Option("(system default output)", ""));
    outputs.forEach((d) => outSelect.appendChild(new Option(`${d.name} · ${d.host_api}`, d.id)));
    outSelect.value = defaults.agent_output_device || "";
    outSelect.onchange = async () => {
      try {
        await api(`${V1}/audio/defaults/output`, { method: "POST", body: { device_id: outSelect.value } });
      } catch (error) { pushError(error.message); }
      loadDevices();
    };
    const testNote = el("span", "mono hint", "");
    const testBtn = actionButton("▶ Test", "", async () => {
      const id = outSelect.value || (outputs.find((d) => d.is_default_output) || outputs[0] || {}).id || "";
      if (!id) { pushError("No output device available to test."); return; }
      testBtn.disabled = true;
      testNote.textContent = "testing…";
      try {
        const r = await api(`${V1}/audio/devices/test`, { method: "POST", body: { device_id: id } });
        testNote.textContent = `✓ ${r.method === "tone" ? "tone" : "spoken test"} on ${r.device_name || id}`;
      } catch (error) {
        testNote.textContent = "";
        pushError(error.message);
      } finally {
        testBtn.disabled = false;
      }
    });
    testBtn.title = "Play a test sound on the selected output device";
    defRow.append(el("span", "filter-label", "Speak through"), outSelect, testBtn, testNote);
    if (defaults.available === false) {
      defRow.appendChild(badge("saved device missing", "danger"));
    }
    defRow.appendChild(el("span", "mono hint", `in use: ${defaults.effective_device_name || "—"}`));
    defPanel.content.appendChild(defRow);
    defPanel.content.appendChild(el("div", "hint",
      "Agents without their own output device in their voice profile speak through this one."));
    $("dv-defaults").replaceChildren(defPanel.root);

    const routePanel = panel("Full routing matrix (source → engine)");
    const rows = routing.routes.map((r) => [
      mono(r.source), mono(r.engine), mono(r.device_id), String(r.gain),
      badge(r.vad ? "vad" : "raw", r.vad ? "teal" : ""),
      badge(r.command_eligible ? "cmd" : "no-cmd", r.command_eligible ? "ok" : "warn"),
      badge(r.diagnostic_eligible ? "diag" : "—", ""),
      badge(r.tts_accuracy_eligible ? "acc" : "—", ""),
      r.fallback_policy,
    ]);
    routePanel.content.appendChild(
      rows.length
        ? table(["Source", "Engine", "Device", "Gain", "VAD", "Command", "Diag", "Accuracy", "Fallback"], rows)
        : el("div", "hint", "No explicit routes; every engine follows the active capture device."));
    $("dv-routing").replaceChildren(routePanel.root);
  } catch (error) {
    $("dv-capture").textContent = `error: ${error.message}`;
  }
}

/* ---- voices */
async function loadVoices() {
  const body = $("vc-body");
  try {
    const [data, fleet] = await Promise.all([api(`${V1}/voices`), api(`${V1}/workers`)]);
    body.replaceChildren();

    // Combine configured voice profiles with connected workers, so a worker that
    // has no profile yet is still listed (its params editable) and assignable.
    const byId = new Map();
    (data.profiles || []).forEach((p) => byId.set(p.agent_id, { ...p }));
    (fleet.workers || []).forEach((w) => {
      if (!byId.has(w.worker_id)) {
        byId.set(w.worker_id, {
          agent_id: w.worker_id, voice_id: "", engine: "", rate: 1.0, pitch: 0.0,
          volume: 1.0, queue_priority: 5, speaking_permission: true,
        });
      }
    });
    const agents = Array.from(byId.values()).sort((a, b) => a.agent_id.localeCompare(b.agent_id));
    const agentIds = agents.map((a) => a.agent_id);
    const used = {};
    agents.forEach((p) => { if (p.voice_id) used[p.voice_id] = (used[p.voice_id] || 0) + 1; });

    const numInput = (value, step, lo, hi, onSave) => {
      const inp = el("input");
      inp.type = "number"; inp.value = String(value);
      inp.step = String(step); inp.min = String(lo); inp.max = String(hi);
      inp.style.width = "62px";
      inp.onchange = async () => { try { await onSave(parseFloat(inp.value)); } catch (e) { pushError(e.message); } };
      return inp;
    };
    const saveProfile = (agentId, patch) =>
      api(`${V1}/voices/${encodeURIComponent(agentId)}`, { method: "POST", body: patch });

    const profPanel = panel("Agent & worker voice profiles — Speed, Pitch, Volume, Priority editable and persistent");
    profPanel.content.appendChild(table(
      ["Agent / Worker", "Voice", "Engine", "Speed", "Pitch", "Vol", "Priority", "Speak", "Conflict", "Preview"],
      agents.map((p) => [
        mono(p.agent_id), p.voice_id ? mono(p.voice_id) : badge("unassigned", "warn"), p.engine || "—",
        numInput(p.rate, 0.05, 0.5, 2.0, (v) => saveProfile(p.agent_id, { rate: v })),
        numInput(p.pitch, 1, -10, 10, (v) => saveProfile(p.agent_id, { pitch: v })),
        numInput(p.volume, 0.05, 0, 1.0, (v) => saveProfile(p.agent_id, { volume: v })),
        numInput(p.queue_priority, 1, 1, 10, (v) => saveProfile(p.agent_id, { queue_priority: Math.round(v) })),
        actionButton(p.speaking_permission ? "on" : "muted", p.speaking_permission ? "ok" : "warn", async () => {
          try { await saveProfile(p.agent_id, { speaking_permission: !p.speaking_permission }); loadVoices(); }
          catch (error) { pushError(error.message); }
        }),
        p.voice_id ? (used[p.voice_id] > 1 ? badge("shared", "warn") : badge("unique", "ok")) : "—",
        actionButton("Preview", "", async () => {
          try { await api(`${V1}/tts/speak`, { method: "POST", body: { agent_id: p.agent_id, text: `This is ${p.agent_id}.`, priority: 1 } }); }
          catch (error) { pushError(error.message); }
        }),
      ])));
    body.appendChild(profPanel.root);

    const voicePanel = panel("Available voices — preview any voice, or clone one with custom speed/pitch");
    voicePanel.content.appendChild(table(
      ["ID", "Name", "Provider", "Language", "Style", "Available", "Assign to", "Actions"],
      data.voices.map((v) => {
        const select = el("select");
        select.appendChild(new Option("assign to…", ""));
        agentIds.forEach((id) => select.appendChild(new Option(id, id)));
        select.onchange = async () => {
          if (!select.value) return;
          try { await api(`${V1}/voices/${encodeURIComponent(select.value)}`, { method: "POST", body: { voice_id: v.id, engine: v.provider } }); loadVoices(); }
          catch (error) { pushError(error.message); }
        };
        const actions = el("div", "toolbar");
        actions.append(
          actionButton("Preview", "", async () => {
            try { await api(`${V1}/voices/preview`, { method: "POST", body: { voice_id: v.id } }); }
            catch (error) { pushError(error.message); }
          }),
          actionButton("Clone", "", async () => {
            const name = prompt(`Clone "${v.name}" as:`, `${v.name} custom`);
            if (!name) return;
            const rate = parseFloat(prompt("Speed (0.5 - 2.0):", "1.0") || "1");
            const pitch = parseFloat(prompt("Pitch (-10 - 10):", "0") || "0");
            try {
              await api(`${V1}/voices/clone`, { method: "POST", body: { base_voice_id: v.id, name, rate, pitch } });
              loadVoices();
            } catch (error) { pushError(error.message); }
          }),
        );
        if (String(v.id).startsWith("clone:")) {
          actions.append(actionButton("Delete", "danger", async () => {
            if (!confirm(`Delete cloned voice "${v.name}"?`)) return;
            try { await api(`${V1}/voices/clone/delete`, { method: "POST", body: { clone_id: v.id } }); loadVoices(); }
            catch (error) { pushError(error.message); }
          }));
        }
        return [mono(v.id), v.name, v.provider, v.language, v.style,
          badge(v.available ? "yes" : "no", v.available ? "ok" : "danger"), select, actions];
      })));
    body.appendChild(voicePanel.root);
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

/* ---- stt test */
async function loadStt() {
  const body = $("stt-body");
  try {
    const data = await api(`${V1}/stt/transcripts?limit=100`);
    const events = (data.events || []).slice().reverse();
    body.replaceChildren();
    const p = panel("Recent STT transcripts (newest first) — includes hypotheses ingested from this page or any recognizer");
    p.content.appendChild(events.length ? table(
      ["Time", "Engine", "Final", "Confidence", "Text", "Correlation"],
      events.map((e) => {
        const d = e.data || {};
        const isError = e.type === "STT_ENGINE_ERROR";
        const text = d.text || d.normalized_text || d.raw_text || (isError ? `⚠ ${d.error || "error"}` : "—");
        return [
          shortTs(e.ts), mono(d.engine || e.source_id || "—"),
          d.is_final ? badge("final", "ok") : badge("partial", "warn"),
          mono(d.confidence != null ? String(d.confidence) : "—"),
          isError ? el("span", "hint", text) : (text || "—"), mono(d.correlation_id || e.correlation_id || e.id),
        ];
      })
    ) : el("div", "hint", "No transcripts yet. Ingest text below, or use the mic button to test with real speech recognition."));
    body.appendChild(p.root);
  } catch (error) { body.textContent = `error: ${error.message}`; }
  primeSttEngineRows();
}

/* ------------------------------------------------------ live per-engine STT */
// One row per STT engine (as reported live by the server) plus a pinned,
// highlighted "disambiguator" row for the final resolved result. Populated
// from /v1/status on page load, then updated live from the WS stt_transcripts
// stream in ingest() -- no polling, no page reload needed.
const sttEngineRows = new Map();

function sttEngineTbody() {
  let tbody = document.querySelector("#stt-engine-body tbody");
  if (tbody) return tbody;
  const host = $("stt-engine-body");
  host.replaceChildren();
  const t = table(["Engine", "Status", "Confidence", "Text", "Time"], []);
  host.appendChild(t);
  return t.querySelector("tbody");
}

function sttEngineRow(engine) {
  let row = sttEngineRows.get(engine);
  if (row) return row;
  const tbody = sttEngineTbody();
  const tr = el("tr");
  if (engine === "disambiguator") tr.classList.add("stt-final-row");
  const tdEngine = el("td"); tdEngine.appendChild(mono(engine));
  const tdStatus = el("td"); tdStatus.appendChild(badge("waiting…", ""));
  const tdConf = el("td"); tdConf.textContent = "—";
  const tdText = el("td"); tdText.textContent = "—";
  const tdTime = el("td"); tdTime.textContent = "—";
  tr.append(tdEngine, tdStatus, tdConf, tdText, tdTime);
  // Keep the disambiguator's final-result row pinned to the bottom, visually
  // separated from the individual per-engine hypotheses above it.
  if (engine === "disambiguator") tbody.appendChild(tr);
  else tbody.insertBefore(tr, tbody.querySelector(".stt-final-row"));
  row = { tr, tdStatus, tdConf, tdText, tdTime };
  sttEngineRows.set(engine, row);
  return row;
}

function primeSttEngineRows() {
  api(`${V1}/status`).then((status) => {
    const engines = (status.subsystems && status.subsystems.stt && status.subsystems.stt.engines) || [];
    $("stt-engine-hint").textContent = engines.length
      ? `Configured engines: ${engines.join(", ")}, then disambiguator. Speak into the mic (Devices page → Start listening) to see live results.`
      : "No STT engines configured.";
    engines.forEach((name) => sttEngineRow(name));
    sttEngineRow("disambiguator");
  }).catch(() => { $("stt-engine-hint").textContent = "Could not load engine list from /v1/status."; });
}

function handleSttTranscriptEvent(event) {
  const d = event.data || {};
  const engine = d.engine || event.source_id;
  if (!engine) return;
  const row = sttEngineRow(engine);
  const isDisambiguator = engine === "disambiguator";
  const isError = event.type === "STT_ENGINE_ERROR";
  const text = isDisambiguator ? d.resolved_text : (d.raw_text || d.normalized_text);
  row.tdTime.textContent = shortTs(event.ts);
  row.tdConf.textContent = d.confidence != null ? String(d.confidence) : "—";
  if (isError) {
    row.tdStatus.replaceChildren(badge("error", "danger"));
    row.tdText.textContent = `⚠ ${d.error || "engine error"}`;
  } else if (!text) {
    row.tdStatus.replaceChildren(badge(d.is_final === false ? "listening…" : "no speech", ""));
    row.tdText.textContent = "—";
  } else {
    row.tdStatus.replaceChildren(badge(
      isDisambiguator ? "FINAL" : (d.is_final === false ? "partial" : "final"),
      isDisambiguator ? "ok" : (d.is_final === false ? "warn" : "teal"),
    ));
    row.tdText.textContent = text;
  }
}

async function sttIngest(text, opts = {}) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  try {
    await api(`${V1}/stt/ingest`, {
      method: "POST",
      body: {
        engine: $("stt-engine").value || "external",
        text: trimmed,
        language: $("stt-lang").value || "en",
        confidence: opts.confidence != null ? opts.confidence : Number($("stt-conf").value || 0.9),
        is_final: opts.is_final !== undefined ? opts.is_final : $("stt-final").checked,
      },
    });
    if (opts.is_final) loadStt();
  } catch (error) {
    if (!opts.silent) pushError(error.message);
  }
}

function sttLiveLog(text) {
  const log = $("stt-live-log");
  if (!log) return;
  const line = el("div", "mono", `${new Date().toLocaleTimeString()}  ${text}`);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

// Two listening modes -- this is an explicit "allowed to stop itself or not"
// switch, since that's the actual source of confusion: the Web Speech API
// naturally ends a session after a silence/no-speech timeout even when
// `continuous` is set, and it does so silently unless we surface it.
//  - single:     the browser is ALLOWED to stop itself once it thinks you're
//                done talking (one utterance, no restart).
//  - continuous: NOT allowed to stop itself -- any end/error auto-restarts
//                (after a short delay, to dodge a start()-while-stopping
//                race) until you explicitly click "Stop listening".
// The speaking / done-talking indicator is shown in both modes.
let sttRecognizer = null;
let sttShouldContinue = false;
let sttRestartTimer = null;

const STT_ERROR_HINTS = {
  "no-speech": "no speech detected (silence timeout)",
  "audio-capture": "no microphone found / capture failed",
  "not-allowed": "microphone permission blocked",
  network: "network error",
  aborted: "aborted",
};

function setSpeechIndicator(text, kind) {
  const badge = $("stt-speech-state");
  badge.textContent = text;
  badge.className = `badge ${kind || ""}`;
}

// Short synthesized beep (Web Audio API -- no audio file needed) played the
// moment the recognizer thinks you've stopped talking (onspeechend).
let sttAudioCtx = null;
function playBeep() {
  try {
    sttAudioCtx = sttAudioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (sttAudioCtx.state === "suspended") sttAudioCtx.resume();
    const osc = sttAudioCtx.createOscillator();
    const gain = sttAudioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880; // A5 -- short, clearly audible "done" chirp
    const now = sttAudioCtx.currentTime;
    gain.gain.setValueAtTime(0.15, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.15);
    osc.connect(gain).connect(sttAudioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.15);
  } catch (error) { /* no audio output available -- non-fatal */ }
}

function startSttRecognizer(mode) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const status = $("stt-mic-status");
  const recognizer = new Recognition();
  recognizer.lang = $("stt-lang").value || "en-US";
  recognizer.continuous = mode !== "single";
  recognizer.interimResults = true;
  let lastError = null;
  recognizer.onstart = () => {
    lastError = null;
    status.textContent = mode === "single" ? "🔴 listening for one utterance…" : "🔴 listening (won't stop itself)…";
    $("stt-mic").textContent = "⏹ Stop listening";
    $("stt-live-partial").textContent = "…";
    setSpeechIndicator("🗣️ speaking…", "ok");
  };
  // The browser's own end-of-utterance detector -- the "thinks I'm done
  // talking" signal that was asked for. Fires before onend/onerror.
  recognizer.onspeechstart = () => setSpeechIndicator("🗣️ speaking…", "ok");
  recognizer.onspeechend = () => { setSpeechIndicator("⏸ done talking (silence detected)", "warn"); playBeep(); };
  recognizer.onerror = (e) => {
    lastError = e.error;
    status.textContent = `⚠ ${STT_ERROR_HINTS[e.error] || e.error}`;
  };
  recognizer.onend = () => {
    sttRecognizer = null;
    const reason = lastError ? (STT_ERROR_HINTS[lastError] || lastError) : "session ended";
    if (sttShouldContinue && mode !== "single") {
      // Continuous mode is not allowed to stop itself: restart after a short
      // delay (a bare restart from inside onend can throw "already started").
      status.textContent = `⏹ ${reason} — restarting (continuous mode)…`;
      setSpeechIndicator("⏸ restarting…", "warn");
      sttRestartTimer = setTimeout(() => { if (sttShouldContinue) startSttRecognizer(mode); }, 300);
      return;
    }
    sttShouldContinue = false;
    status.textContent = mode === "single" ? `stopped: ${reason}` : `stopped by user (${reason})`;
    $("stt-mic").textContent = "🎤 Start listening";
    $("stt-live-partial").textContent = "Click \"Start listening\" and speak…";
    setSpeechIndicator("", "");
  };
  recognizer.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const transcript = result[0].transcript;
      const confidence = result[0].confidence || 0.9;
      if (result.isFinal) {
        $("stt-live-partial").textContent = "…";
        sttLiveLog(transcript);
        $("stt-text").value = transcript;
        sttIngest(transcript, { is_final: true, confidence });
      } else {
        // Interim hypothesis: reflect it live as it changes, word by word.
        $("stt-live-partial").textContent = transcript;
        if ($("stt-send-partials").checked) sttIngest(transcript, { is_final: false, confidence, silent: true });
      }
    }
  };
  try {
    recognizer.start();
    sttRecognizer = recognizer;
  } catch (err) {
    // Almost always a start()-while-stopping race; retry shortly rather than
    // silently doing nothing (which is what looked like "it just stops").
    status.textContent = `⚠ could not restart (${err.message || err}) — retrying…`;
    sttRestartTimer = setTimeout(() => { if (sttShouldContinue) startSttRecognizer(mode); }, 300);
  }
}

function toggleSttMic() {
  const status = $("stt-mic-status");
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { status.textContent = "Browser Web Speech API not available (try Chrome/Edge over http://localhost)."; return; }
  if (sttRecognizer || sttShouldContinue) {
    // Also covers the ~300ms gap where a restart is pending but no recognizer
    // instance exists yet -- otherwise "Stop" could appear to do nothing.
    sttShouldContinue = false;
    clearTimeout(sttRestartTimer);
    if (sttRecognizer) sttRecognizer.stop();
    else {
      status.textContent = "stopped by user";
      $("stt-mic").textContent = "🎤 Start listening";
      $("stt-live-partial").textContent = "Click \"Start listening\" and speak…";
      setSpeechIndicator("", "");
    }
    return;
  }
  sttShouldContinue = true;
  startSttRecognizer($("stt-mic-mode").value || "continuous");
}

/* ---- accuracy */
async function loadAccuracy() {
  const body = $("ac-body");
  try {
    const data = await api(`${V1}/tts/accuracy`);
    const groups = data.groups || {};
    const rows = Object.entries(groups).map(([name, g]) => [
      mono(name), String(g.count), String(g.avg_wer), String(g.avg_cer),
      mono(g.worst_example ? `${g.worst_example.expected} → ${g.worst_example.got}` : "—"),
    ]);
    body.replaceChildren();
    const p = panel("Rolling accuracy by engine / final (WER + CER, sample size, worst example)");
    p.content.appendChild(rows.length ? table(["Group", "Samples", "Avg WER", "Avg CER", "Worst example"], rows)
      : el("div", "hint", "No accuracy samples yet. Use Measure to speak a reference phrase and capture the loopback echo."));
    body.appendChild(p.root);
    const note = el("div", "hint", "Semantic similarity is recorded as a secondary metric only; WER/CER remain authoritative.");
    body.appendChild(note);
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

/* ---- cursors */
async function loadCursors() {
  const body = $("cu-body");
  try {
    const data = await api(`${V1}/cursors`);
    body.replaceChildren();
    const p = panel("Durable cursors (per stream + consumer)");
    p.content.appendChild(table(
      ["Stream", "Consumer", "Seq", "Updated", "Reason", "Operator", "Reposition"],
      data.cursors.map((c) => [
        mono(c.stream), mono(c.consumer), String(c.seq), shortTs(c.updated_at), c.reason || "—", c.operator || "—",
        repositionControl(c),
      ])));
    if (!data.cursors.length) p.content.appendChild(el("div", "hint", "No cursors recorded yet."));
    body.appendChild(p.root);
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

function repositionControl(cursor) {
  const wrap = el("span");
  const input = el("input"); input.type = "number"; input.value = cursor.seq; input.style.width = "70px";
  const go = el("button", "danger", "Move");
  go.onclick = async () => {
    const target = Number(input.value);
    const rewind = target < cursor.seq;
    const message = rewind
      ? `Rewind ${cursor.stream}/${cursor.consumer} from ${cursor.seq} to ${target}?\n\nRISK: events will be REPLAYED (possible duplicate external actions).`
      : `Skip ${cursor.stream}/${cursor.consumer} forward from ${cursor.seq} to ${target}?\n\nRISK: events will be SKIPPED and never processed.`;
    if (target === cursor.seq || !confirm(message)) return;
    try {
      await api(`${V1}/cursors/${encodeURIComponent(cursor.stream)}/${encodeURIComponent(cursor.consumer)}/reposition`, {
        method: "POST",
        body: { seq: target, reason: "admin reposition", allow_replay: rewind, allow_skip: !rewind },
      });
      loadCursors();
    } catch (error) { pushError(error.message); }
  };
  wrap.append(input, go);
  return wrap;
}

/* ---- prompt */
async function loadPrompt() {
  try {
    const [current, history] = await Promise.all([api(`${V1}/prompt`), api(`${V1}/prompt/history`)]);
    $("pr-text").value = current.text || "";
    const rows = (history.history || []).slice().reverse().map((h) => [
      String(h.version), mono(h.hash || "—"), h.operator || "—", shortTs(h.saved_at), h.note || "—",
      actionButton("Rollback", "danger", async () => {
        if (!confirm(`Roll back to version ${h.version}? This creates a NEW version; history is preserved.`)) return;
        try { await api(`${V1}/prompt/rollback`, { method: "POST", body: { version: h.version } }); loadPrompt(); }
        catch (error) { pushError(error.message); }
      }),
    ]);
    $("pr-history").replaceChildren(rows.length ? table(["Version", "Hash", "Operator", "Saved", "Note", ""], rows)
      : el("div", "hint", "No versions recorded yet."));
  } catch (error) { pushError(error.message); }
}

function renderDiff(text) {
  const container = el("div");
  if (!text) { container.appendChild(el("div", "hint", "No differences.")); return container; }
  text.split("\n").forEach((line) => {
    let cls = "diff-line";
    if (line.startsWith("+")) cls += " diff-add";
    else if (line.startsWith("-")) cls += " diff-del";
    else if (line.startsWith("@@")) cls += " diff-meta";
    container.appendChild(el("div", cls, line));
  });
  return container;
}

/* ---- system */
async function loadSystem() {
  const body = $("sy-body");
  try {
    const [diag, config, caps, audit] = await Promise.all([
      api(`${V1}/diagnostics`), api(`${V1}/config`), api(`${V1}/capabilities`), api(`${V1}/audit?limit=100`).catch(() => ({ events: [] })),
    ]);
    state.config = config;
    body.replaceChildren();
    if ((config.warnings || []).length) {
      const banner = el("div", "warn-banner", "⚠ " + config.warnings.join(" · "));
      body.appendChild(banner);
    }
    const health = panel("Server & connection health");
    health.content.appendChild(kv({
      uptime_seconds: diag.uptime_seconds, subscriptions: diag.broker.subscriptions,
      dropped: diag.broker.dropped, delivered: diag.broker.delivered,
      workers: diag.workers, device_generation: diag.devices, transport: state.transport,
    }));
    body.appendChild(health.root);

    const streams = panel("Durable JSONL streams");
    streams.content.appendChild(table(["Stream", "File", "Seq", "Gen", "Active bytes", "Segments"],
      diag.streams.map((s) => [mono(s.stream), mono(s.filename), String(s.seq), String(s.gen), String(s.active_bytes), String(s.segments)])));
    body.appendChild(streams.root);

    const cfg = panel("Configuration (secrets never shown)");
    cfg.content.appendChild(kv(config));
    body.appendChild(cfg.root);

    const capsPanel = panel("Capabilities");
    capsPanel.content.appendChild(kv(caps.features));
    body.appendChild(capsPanel.root);

    const auditPanel = panel("Audit history");
    auditPanel.content.appendChild(table(["Time", "Action", "Detail"],
      (audit.events || []).slice().reverse().map((e) => [shortTs(e.ts), e.data.action || e.type, mono(JSON.stringify(e.data).slice(0, 140))])));
    body.appendChild(auditPanel.root);
  } catch (error) { body.textContent = `error: ${error.message}`; }
}

/* --------------------------------------------------------------- UI helpers */
function panel(title) {
  const root = el("div", "panel");
  root.appendChild(el("div", "panel-title", title));
  const content = el("div", "panel-content");
  root.appendChild(content);
  return { root, content };
}
function mono(text) { return el("span", "mono", esc(text)); }
function badge(text, kind) { return el("span", `badge ${kind || ""}`, esc(text)); }
function actionButton(label, kind, handler) {
  const button = el("button", kind, label);
  button.onclick = handler;
  return button;
}

/* ---------------------------------------------------------- toggle buttons */
function isPressed(id) {
  const node = $(id);
  return !node || node.getAttribute("aria-pressed") === "true";
}

function wireToggle(id, onChange) {
  const node = $(id);
  if (!node) return;
  const saved = localStorage.getItem(`ws_collab_toggle_${id}`);
  if (saved !== null) node.setAttribute("aria-pressed", saved);
  node.addEventListener("click", () => {
    const next = node.getAttribute("aria-pressed") !== "true";
    node.setAttribute("aria-pressed", String(next));
    localStorage.setItem(`ws_collab_toggle_${id}`, String(next));
    onChange();
  });
}
function table(headers, rows) {
  const t = el("table");
  const thead = el("thead"); const hr = el("tr");
  headers.forEach((h) => hr.appendChild(el("th", null, h)));
  thead.appendChild(hr); t.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((cell) => {
      const td = el("td");
      if (cell instanceof Node) td.appendChild(cell); else td.textContent = esc(cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  t.appendChild(tbody);
  return t;
}
function kv(object) {
  return table(["Key", "Value"], Object.entries(object).map(([k, v]) => [mono(k), mono(typeof v === "object" ? JSON.stringify(v) : String(v))]));
}

/* ------------------------------------------------------------- backfill/export */
async function backfill(streams, view) {
  for (const stream of streams) {
    const buffer = bufferFor(stream);   // ensure the buffer exists even if empty
    try {
      const page = await api(`${V1}/streams/${encodeURIComponent(stream)}/tail?count=300`);
      (page.events || []).forEach((event) => {
        if (!state.seen[stream].has(event.seq)) {
          state.seen[stream].add(event.seq);
          buffer.push(event);
        }
      });
      buffer.sort((a, b) => (a.seq || 0) - (b.seq || 0));
    } catch (error) { pushError(`backfill ${stream}: ${error.message}`); }
  }
  const merged = streams.flatMap((s) => state.buffers[s] || []).sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  view.rebuild(merged);
}

function exportView(view, name) {
  const lines = view.rows.map((e) => JSON.stringify(e)).join("\n");
  const blob = new Blob([lines], { type: "application/x-ndjson" });
  const link = el("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${name}-${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`;
  link.click();
  URL.revokeObjectURL(link.href);
}

/* ------------------------------------------------------------------- boot */
function buildFilters() {
  const search = $("tr-search").value.toLowerCase();
  const finalsOnly = $("tr-finals").checked;
  const hidePartials = $("tr-hide-partials").checked;
  const hideEcho = $("tr-hide-echo").checked;
  const hideRoutine = $("tr-hide-routine").checked;
  const minConf = Number($("tr-minconf").value || 0);
  return (event) => {
    if (finalsOnly && !["HEARD_SPEECH", "TRANSCRIPT_RESOLVED", "STT_FINAL_RESULT", "CONVERSATION_MESSAGE"].includes(event.type)) return false;
    if (hidePartials && event.type === "STT_PARTIAL_RESULT") return false;
    if (hideRoutine && ROUTINE_TYPES.has(event.type)) return false;
    const data = event.data || {};
    const isEcho = event.type === "TTS_AUDIO_DETECTED_BY_MICROPHONE" || event.type === "TRANSCRIPT_FILTERED"
      || (data.classification && data.classification.is_echo);
    if (hideEcho && isEcho) return false;
    if (minConf > 0) {
      const conf = data.confidence ?? (data.resolved && data.resolved.confidence);
      if (conf !== undefined && conf !== null && conf < minConf) return false;
    }
    if (search) {
      const haystack = `${event.type} ${event.source_id} ${JSON.stringify(data)}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  };
}

function refreshTranscriptFilter() {
  const view = state.views.transcript;
  view.filter = buildFilters();
  const merged = TRANSCRIPT_STREAMS.flatMap((s) => state.buffers[s] || []).sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  view.rebuild(merged);
}

function initViews() {
  createView({
    id: "transcript", scrollId: "tr-scroll", spacerId: "tr-spacer", viewportId: "tr-viewport",
    jumpId: "tr-jump", unseenId: "tr-unseen", countsId: "tr-counts",
    streams: TRANSCRIPT_STREAMS, filter: buildFilters(),
    capFn: () => Number($("tr-cap").value || 2000),
  });
  createView({
    id: "conversation", scrollId: "cv-scroll", spacerId: "cv-spacer", viewportId: "cv-viewport",
    jumpId: "cv-jump", unseenId: "cv-unseen", streams: ["conversation"],
    filter: (event) => {
      const search = $("cv-search").value.toLowerCase();
      if (!search) return true;
      return JSON.stringify(event).toLowerCase().includes(search);
    },
  });
  const streamSelect = $("st-stream");
  STREAMS.forEach((s) => streamSelect.appendChild(new Option(s, s)));
  createView({
    id: "streams", scrollId: "st-scroll", spacerId: "st-spacer", viewportId: "st-viewport",
    jumpId: "st-jump", unseenId: "st-unseen", countsId: "st-counts", streams: [streamSelect.value],
    filter: (event) => {
      if (event.stream !== $("st-stream").value) return false;
      const type = $("st-type").value;
      if (type && !event.type.toLowerCase().includes(type.toLowerCase())) return false;
      const search = $("st-search").value.toLowerCase();
      return !search || JSON.stringify(event).toLowerCase().includes(search);
    },
    render: (event) => ($("st-raw").checked ? rawRow(event) : defaultRender(event)),
  });
}

function rawRow(event) {
  const row = el("div", "event-row");
  row.appendChild(el("span", "ev-text mono", JSON.stringify(event)));
  row.addEventListener("click", () => selectEvent(event, row));
  return row;
}

function wireEvents() {
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.addEventListener("click", () => showPage(node.dataset.page));
    node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showPage(node.dataset.page); } });
  });
  const streamsNav = document.getElementById("nav-streams");
  if (streamsNav) {
    streamsNav.addEventListener("click", () => {
      const sub = document.getElementById("nav-streams-sub");
      const caret = document.getElementById("nav-streams-caret");
      if (!sub) return;
      const collapsed = sub.hasAttribute("hidden");
      if (collapsed) { sub.removeAttribute("hidden"); if (caret) caret.textContent = "▾"; }
      else { sub.setAttribute("hidden", ""); if (caret) caret.textContent = "▸"; }
    });
  }
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      state.inspectorTab = tab.dataset.tab;
      renderInspector();
    });
  });
  $("nav-toggle").onclick = () => $("sidebar").classList.toggle("collapsed");
  $("inspector-toggle").onclick = () => $("inspector").classList.toggle("hidden");
  $("logout").onclick = logout;
  $("insp-copy").onclick = () => navigator.clipboard.writeText($("insp-content").textContent);

  // Transport badge: inspect endpoints, and reconnect on demand when down.
  $("transport").onclick = (event) => {
    event.stopPropagation();
    toggleEndpointPopover();
  };
  $("endpoint-reconnect").onclick = (event) => {
    event.stopPropagation();
    reconnectNow();
    refreshEndpointData();
  };
  $("endpoint-popover").onclick = (event) => event.stopPropagation();
  document.addEventListener("click", () => toggleEndpointPopover(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggleEndpointPopover(false);
  });

  // transcript controls
  ["tr-search", "tr-finals", "tr-hide-partials", "tr-hide-echo", "tr-hide-routine", "tr-minconf", "tr-cap"]
    .forEach((id) => $(id).addEventListener("input", refreshTranscriptFilter));
  $("tr-pause").onclick = () => {
    const view = state.views.transcript;
    view.setPaused(!view.paused);
    $("tr-pause").textContent = view.paused ? "Resume" : "Pause";
    $("tr-pause").classList.toggle("primary", !view.paused);
  };
  $("tr-backfill").onclick = () => backfill(TRANSCRIPT_STREAMS, state.views.transcript);
  $("tr-export").onclick = () => exportView(state.views.transcript, "transcript");
  $("tr-clear").onclick = () => state.views.transcript.clearView();

  // conversation
  $("cv-search").addEventListener("input", () => {
    state.views.conversation.rebuild(state.buffers.conversation || []);
  });
  $("cv-pause").onclick = () => {
    const view = state.views.conversation;
    view.setPaused(!view.paused);
    $("cv-pause").textContent = view.paused ? "Resume" : "Pause";
  };
  $("cv-send").onclick = async () => {
    const text = $("cv-text").value.trim();
    if (!text) return;
    try {
      await api(`${V1}/conversation/events`, { method: "POST", body: { text }, headers: { "Idempotency-Key": `admin-${Date.now()}` } });
      $("cv-text").value = "";
    } catch (error) { pushError(error.message); }
  };

  // streams page
  ["st-stream", "st-search", "st-type", "st-raw"].forEach((id) =>
    $(id).addEventListener("input", () => {
      const view = state.views.streams;
      const cur = $("st-stream").value;
      view.streams = [cur];
      view.rebuild(state.buffers[cur] || []);
      if (state.streamStats && state.streamStats[cur]) state.streamStats[cur].unread = 0;
      document.querySelectorAll("#nav-streams-sub .nav-subitem").forEach((n) =>
        n.classList.toggle("active", n.dataset.stream === cur));
      renderStreamMenu();
      if (state.streamMode === "tile") renderTiles();
    }));
  $("st-pause").onclick = () => {
    const view = state.views.streams;
    view.setPaused(!view.paused);
    $("st-pause").textContent = view.paused ? "Resume" : "Pause";
  };
  $("st-backfill").onclick = () => backfill([$("st-stream").value], state.views.streams);
  $("st-export").onclick = () => exportView(state.views.streams, $("st-stream").value);
  $("st-clear").onclick = () => state.views.streams.clearView();
  $("st-mode").onclick = () => setStreamMode(state.streamMode === "tile" ? "list" : "tile");
  $("st-format").addEventListener("change", () => {
    state.streamFormat = $("st-format").value;
    if (state.streamMode === "tile") renderTiles();
  });

  // Optional fallback: the experimental React streams viewer (kept, unmaintained).
  // The button lets an operator offer it as a fallback, or turn it off entirely.
  const REACT_FALLBACK_KEY = "wsc.reactFallback";
  const applyReactFallback = () => {
    const on = localStorage.getItem(REACT_FALLBACK_KEY) === "on";
    const btn = $("st-fallback");
    const open = $("st-open-react");
    if (btn) btn.textContent = on ? "React viewer: fallback" : "React viewer: off";
    if (open) open.style.display = on ? "" : "none";
  };
  if ($("st-fallback")) {
    $("st-fallback").onclick = () => {
      const on = localStorage.getItem(REACT_FALLBACK_KEY) === "on";
      localStorage.setItem(REACT_FALLBACK_KEY, on ? "off" : "on");
      applyReactFallback();
    };
  }
  if ($("st-open-react")) $("st-open-react").onclick = () => { location.href = "react/"; };
  applyReactFallback();

  // page actions
  $("wk-refresh").onclick = loadWorkers;
  $("wk-monitor").onclick = async () => { try { await api(`${V1}/workers/monitor`, { method: "POST", body: {} }); loadWorkers(); } catch (e) { pushError(e.message); } };
  $("al-refresh").onclick = loadAlerts;
  $("dv-refresh").onclick = async () => { try { await api(`${V1}/audio/devices/refresh`, { method: "POST", body: {} }); } catch (e) { pushError(e.message); } loadDevices(); };
  $("dv-start").onclick = async () => { try { await api(`${V1}/audio/capture/start`, { method: "POST", body: {} }); } catch (e) { pushError(e.message); } loadDevices(); };
  $("dv-stop").onclick = async () => { try { await api(`${V1}/audio/capture/stop`, { method: "POST", body: {} }); } catch (e) { pushError(e.message); } loadDevices(); };
  $("dv-inject").onclick = async () => {
    const text = prompt("Utterance text to inject through the pipeline:", "run the two reports");
    if (!text) return;
    try { await api(`${V1}/audio/utterance`, { method: "POST", body: { text, source_kind: "operator" } }); }
    catch (error) { pushError(error.message); }
  };
  document.querySelectorAll(".filter-bar button.tristate[data-cat]")
    .forEach((btn) => wireCatFilter(btn, loadDevices));
  $("dv-search").addEventListener("input", loadDevices);
  $("vc-refresh").onclick = loadVoices;
  $("vc-assign").onclick = async () => {
    try { await api(`${V1}/voices/assign`, { method: "POST", body: { policy: $("vc-policy").value } }); loadVoices(); }
    catch (error) { pushError(error.message); }
  };
  $("stt-refresh").onclick = loadStt;
  $("stt-send").onclick = () => sttIngest($("stt-text").value);
  $("stt-text").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sttIngest($("stt-text").value); } });
  $("stt-mic").onclick = toggleSttMic;
  $("ac-refresh").onclick = loadAccuracy;
  $("ac-measure").onclick = async () => {
    try { await api(`${V1}/tts/measure`, { method: "POST", body: { agent_id: $("ac-agent").value, text: $("ac-text").value } }); loadAccuracy(); }
    catch (error) { pushError(error.message); }
  };
  $("cu-refresh").onclick = loadCursors;
  $("pr-reload").onclick = loadPrompt;
  $("pr-diff").onclick = async () => {
    try {
      const result = await api(`${V1}/prompt/preview-diff`, { method: "POST", body: { text: $("pr-text").value } });
      $("pr-diffbody").replaceChildren(renderDiff(result.diff));
    } catch (error) { pushError(error.message); }
  };
  $("pr-save").onclick = async () => {
    if (!confirm("Save a new prompt version? The previous version is preserved and can be rolled back.")) return;
    try {
      await api(`${V1}/prompt`, { method: "POST", body: { text: $("pr-text").value, note: $("pr-note").value } });
      $("pr-note").value = "";
      loadPrompt();
    } catch (error) { pushError(error.message); }
  };
  $("sy-refresh").onclick = loadSystem;

  window.addEventListener("resize", () => Object.values(state.views).forEach((v) => v.draw()));
}

async function refreshStatusBar() {
  $("top-clock").textContent = new Date().toLocaleTimeString();
  try {
    const [diag, capture] = await Promise.all([api(`${V1}/diagnostics`), api(`${V1}/audio/capture`)]);
    checkBootId(diag.boot_id);
    $("sb-backend").textContent = `backend ${capture.backend}`;
    $("sb-state").textContent = `state ${state.config ? state.config.state_dir : "—"}`;
    $("sb-agents").textContent = `agents ${(state.config && state.config.agents ? state.config.agents.length : 0)}`;
    $("sb-workers").textContent = `workers ${diag.workers}`;
    // Keep the Workers nav badge live from any page, so an agent that joins
    // (registers/among the fleet) is visible without opening the Workers page.
    if ($("badge-workers")) $("badge-workers").textContent = diag.workers;
    $("sb-listen").textContent = capture.listening ? `listening ${capture.device_id}` : "listening off";
    $("sb-tts").textContent = diag.tts.is_speaking ? `tts speaking (${diag.tts.queue.length} queued)` : `tts idle (${diag.tts.queue.length} queued)`;
    $("sb-clients").textContent = `clients ${diag.broker.subscriptions}`;
    $("badge-transcript").textContent = state.views.transcript ? state.views.transcript.rows.length : 0;
    renderStreamMenu();
  } catch (error) {
    if (error.status === 401) logout();
  }
}

function logout() {
  sessionStorage.removeItem("ws_collab_token");
  state.token = "";
  if (state.ws) { try { state.ws.close(); } catch {} }
  stopRestFallback();
  location.reload();
}

async function boot() {
  $("login").hidden = true;
  $("app").hidden = false;
  try {
    state.config = await api(`${V1}/config`);
    state.caps = await api(`${V1}/capabilities`);
    checkBootId(state.caps && state.caps.boot_id);
    adoptStreams(state.caps);
    state.endpoints = await api(`${V1}/endpoints`).catch(() => null);
  } catch (error) {
    if (error.status === 401) { logout(); return; }
    pushError(error.message);
  }
  initViews();
  wireEvents();
  connectWs();
  setTimeout(() => { if (!state.wsReady) startRestFallback(); }, 2500);
  await backfill(TRANSCRIPT_STREAMS, state.views.transcript);
  refreshStatusBar();
  setInterval(refreshStatusBar, 5000);
  showPage(pageFromHash());
  window.addEventListener("hashchange", () => showPage(pageFromHash()));
}

async function signIn(token) {
  token = (token || "").trim();
  if (!token) return false;
  state.token = token;
  try {
    await api(`${V1}/auth/whoami`);
    sessionStorage.setItem("ws_collab_token", token);
    boot();
    return true;
  } catch (error) {
    $("login-error").textContent = error.status === 401 ? "Invalid token." : error.message;
    state.token = "";
    return false;
  }
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  signIn($("login-token").value);
});

/* Accept a token via ?token= so the page can be opened in one click during
 * loopback development. The token is removed from the URL and from history
 * immediately, so it is not left in the address bar, bookmarks, or referrers.
 * The token must still be valid -- this is a transport for it, not a bypass.
 */
function consumeTokenFromUrl() {
  const params = new URLSearchParams(location.search);
  const token = params.get("token");
  if (!token) return null;
  params.delete("token");
  const query = params.toString();
  history.replaceState(null, "", location.pathname + (query ? `?${query}` : "") + location.hash);
  return token;
}

/* When the server has authentication disabled (loopback-only deployments),
 * unauthenticated requests are treated as an implicit admin. Probe whoami
 * with no token first so the login form is skipped entirely in that case;
 * only fall back to the sign-in gate if the server actually rejects it.
 */
async function tryAnonymousBoot() {
  try {
    await api(`${V1}/auth/whoami`);
    boot();
    return true;
  } catch {
    return false;
  }
}

const urlToken = consumeTokenFromUrl();
if (urlToken) {
  signIn(urlToken);
} else if (state.token) {
  boot();
} else {
  tryAnonymousBoot().then((ok) => { if (!ok) { $("login").hidden = false; } });
}

})();
