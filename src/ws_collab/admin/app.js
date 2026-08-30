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
const ADMIN_UI_STATE_BASE = `${V1}/admin/ui-state`;
const ROW_H = 22;
const MAX_BUFFER = 5000;

/* The server owns the blocking Chrome/CDP worker and proxies its API so the
 * workbench never depends on a separately reachable unauthenticated port. */
const MEET_BRIDGE_BASE = `${V1}/meet/bridge`;
/* Known driver meetings (host+companion secret-servant rooms this bridge
 * has been pointed at) — shown as placeholder entries even when the bridge
 * isn't currently attached to them (e.g. Google ended one and auto-recreate
 * moved on, or it just hasn't switched there yet), so they stay visible
 * instead of the page going blank the moment they're not the live one.
 * Just the one real, currently-live servant room — a second placeholder
 * ("vfi-zywr-ezz") used to live here too but was never actually joined,
 * which was exactly why "Connectors" appeared to report 4 rows instead of
 * 2 (each non-current entry always renders 2 placeholder HOST+COMPANION
 * rows). Override with `WS_COLLAB_DEFAULT_MEET_URLS` (array). */
const DEFAULT_DRIVER_MEETING_URLS = window.WS_COLLAB_DEFAULT_MEET_URLS || [
  "https://meet.google.com/bgb-xqts-xjt",
];
/* Meetings the bridge would just JOIN AS A GUEST (no host+companion
 * relay-in pair of its own) — CLIENT/GUEST mode, documented in
 * docs/GOOGLE_MEET_BRIDGE.md as "designed but not built" server-side.
 * Listed here so the operator's intended client meeting stays visible as a
 * placeholder (an honest "not implemented yet" GUEST_CLIENT row) even
 * though joining it for real isn't wired up server-side yet.
 * Override with `WS_COLLAB_CLIENT_MEET_URLS` (array). */
const DEFAULT_CLIENT_MEETING_URLS = window.WS_COLLAB_CLIENT_MEET_URLS || [
  "https://meet.google.com/qmj-bkbk-mik",
];

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
  meetAssignmentScope: "",
  meetKnownUrls: [...DEFAULT_DRIVER_MEETING_URLS, ...DEFAULT_CLIENT_MEETING_URLS],
  meetGlobalAssignments: null,
  meetCompanion: {
    meetingUrl: "",
    config: null,
    configMeetingUrl: "",
    health: null,
    loading: false,
  },
  silences: {
    run: null,
    running: false,
    polling: false,
    lastCaptionData: null,
    pendingRender: false,
    pendingRows: 0,
  },
  pageApiSnapshots: {},
  restoredPageStates: {},
  pendingPageRestore: {},
  errors: [],
};

// Stream names are NOT hard-coded here: they are discovered from
// /ws_collab/v1/capabilities so the server remains the single source of truth.
// STREAM_ROLES maps behaviour (which streams feed which view) to capability
// flags rather than to literal names, so renaming a stream never breaks the UI.
let STREAMS = [];
let TRANSCRIPT_STREAMS = [];
let BROWSER_NAV_STREAMS = [];
const ROUTINE_TYPES = new Set([
  "LISTENING_STARTED", "LISTENING_STOPPED", "SPEECH_DETECTED", "WORKER_STATUS",
  "INPUT_MUTED_DURING_TTS", "SECURITY_AUDIT",
]);

function adoptStreams(capabilities) {
  STREAMS = Object.keys((capabilities && capabilities.streams) || {});
  const roles = (capabilities && capabilities.stream_roles) || {};
  TRANSCRIPT_STREAMS = (roles.speech_pipeline || []).filter((s) => STREAMS.includes(s));
  const browserNavRole = roles.browser_navigation || [];
  BROWSER_NAV_STREAMS = (Array.isArray(browserNavRole) ? browserNavRole : [browserNavRole]).filter((s) => STREAMS.includes(s));
  if (state.views.browserNav) state.views.browserNav.streams = BROWSER_NAV_STREAMS;
  populateStreamMenu();
}

function meetEmailName(value) {
  const match = /([A-Z0-9._%+-]+)@[A-Z0-9.-]+/i.exec(String(value || ""));
  return match ? match[1] : null;
}

function meetStableSso(value) {
  const match = /\bsso[-_]\d+\b/i.exec(String(value || ""));
  return match ? match[0].toLowerCase().replace("-", "_") : null;
}

function meetBareName(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const first = text.split(/\s*[([]/)[0].trim();
  return first && !/authuser|resolution/i.test(first) ? first : null;
}

function meetShortIdentity(value) {
  return meetEmailName(value) || meetStableSso(value) || meetBareName(value);
}

function ssoLabel(profile, account) {
  if (account) {
    const label = meetShortIdentity(account.email) || meetShortIdentity(account.label) || meetStableSso(account.id);
    if (label) return label;
  }
  if (!profile) return "\u2014";
  const raw = typeof profile === "string" ? profile : (profile.email || profile.label || profile.id);
  return meetShortIdentity(raw) || (profile.known === false ? "unknown" : "\u2014");
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

/* ------------------------------------------------------ persistent UI state */
let pageStateSaveTimer = null;

function isSensitiveStateName(name) {
  return /(authorization|cookie|credential|password|secret|sessionstorage|token|api[_-]?key)/i.test(name || "");
}

function preferenceSnapshot() {
  const preferences = {};
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index);
    if (!key || isSensitiveStateName(key)) continue;
    if (key.startsWith("ws_collab_") || key.startsWith("wsc.")) {
      preferences[key] = localStorage.getItem(key);
    }
  }
  return preferences;
}

function controlStateKey(node, index) {
  if (node.id) return `id:${node.id}`;
  if (node.dataset && node.dataset.role) {
    return `role:${node.dataset.role}:${node.dataset.meetingUrl || ""}`;
  }
  if (node.dataset && node.dataset.url) return `url:${node.dataset.url}`;
  if (node.name) return `name:${node.name}`;
  return `index:${index}`;
}

function controlSnapshot(section) {
  return [...section.querySelectorAll("input, select, textarea, details, button[aria-pressed], button[data-state]")]
    .filter((node) => node.type !== "password" && !isSensitiveStateName(node.id || node.name || ""))
    .map((node, index) => {
      const row = { key: controlStateKey(node, index), tag: node.tagName.toLowerCase() };
      if ("value" in node && node.type !== "file") row.value = node.value;
      if ("checked" in node) row.checked = !!node.checked;
      if (node.tagName === "DETAILS") row.open = !!node.open;
      if (node.hasAttribute("aria-pressed")) row.pressed = node.getAttribute("aria-pressed");
      if (node.dataset && node.dataset.state) row.data_state = node.dataset.state;
      return row;
    });
}

function scrollSnapshot(section) {
  const result = {};
  [...section.querySelectorAll("[id]")].forEach((node) => {
    if (node.scrollTop || node.scrollLeft) {
      result[node.id] = { top: node.scrollTop, left: node.scrollLeft };
    }
  });
  return result;
}

function capturePageState(page) {
  const section = document.querySelector(`.page[data-page="${page}"]`);
  if (!section) return {};
  return {
    controls: controlSnapshot(section),
    preferences: preferenceSnapshot(),
    view: {
      meet_assignment_scope: page === "meet" ? state.meetAssignmentScope : undefined,
      scroll: scrollSnapshot(section),
    },
    display: { html: section.innerHTML, text: section.innerText },
    api_snapshots: state.pageApiSnapshots[page] || {},
    saved_at: new Date().toISOString(),
  };
}

function applyControlState(page, saved) {
  const section = document.querySelector(`.page[data-page="${page}"]`);
  if (!section || !saved || !Array.isArray(saved.controls)) return;
  const nodes = [...section.querySelectorAll("input, select, textarea, details, button[aria-pressed], button[data-state]")];
  const byKey = new Map(nodes.map((node, index) => [controlStateKey(node, index), node]));
  saved.controls.forEach((row) => {
    const node = byKey.get(row.key);
    if (!node || node.type === "password" || isSensitiveStateName(node.id || node.name || "")) return;
    if (node.dataset && node.dataset.role) return;
    if (row.value !== undefined && "value" in node && node.type !== "file") {
      const savedValue = node.id === "stt-engine" && row.value === "external" ? "manual" : row.value;
      const canApply = node.tagName !== "SELECT" || [...node.options].some((option) => option.value === savedValue);
      if (canApply && node.value !== savedValue) node.value = savedValue;
    }
    if (row.checked !== undefined && "checked" in node && node.checked !== !!row.checked) {
      node.checked = !!row.checked;
    }
    if (row.open !== undefined && node.tagName === "DETAILS" && node.open !== !!row.open) {
      node.open = !!row.open;
    }
    if (row.pressed !== undefined && node.getAttribute("aria-pressed") !== row.pressed) {
      node.setAttribute("aria-pressed", row.pressed);
    }
    if (row.data_state !== undefined && node.dataset && node.dataset.state !== row.data_state) {
      node.dataset.state = row.data_state;
    }
  });
  Object.entries((saved.view && saved.view.scroll) || {}).forEach(([id, position]) => {
    const node = document.getElementById(id);
    if (node && section.contains(node)) {
      node.scrollTop = Number(position.top || 0);
      node.scrollLeft = Number(position.left || 0);
    }
  });
}

function stateRequest(path, options = {}) {
  const headers = Object.assign({ "Authorization": `Bearer ${state.token}` }, options.headers || {});
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  return fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    keepalive: !!options.keepalive,
  });
}

async function restorePageState(page) {
  if (state.restoredPageStates[page]) {
    applyControlState(page, state.restoredPageStates[page]);
    return;
  }
  try {
    const response = await stateRequest(`${ADMIN_UI_STATE_BASE}/${encodeURIComponent(page)}`);
    if (!response.ok) return;
    const payload = await response.json();
    const saved = payload && payload.state;
    if (!saved) return;
    state.restoredPageStates[page] = saved;
    state.pendingPageRestore[page] = true;
    Object.entries(saved.preferences || {}).forEach(([key, value]) => {
      if (!isSensitiveStateName(key) && (key.startsWith("ws_collab_") || key.startsWith("wsc."))) {
        localStorage.setItem(key, value);
      }
    });
    state.pageApiSnapshots[page] = saved.api_snapshots || {};
    if (page === "meet" && saved.view && saved.view.meet_assignment_scope) {
      state.meetAssignmentScope = saved.view.meet_assignment_scope;
    }
    applyControlState(page, saved);
  } catch (_error) {
    // Persistence is auxiliary; a storage error must not block the live page.
  }
}

function savePageState(page, keepalive = false) {
  if (!page) return Promise.resolve();
  const pageState = capturePageState(page);
  state.restoredPageStates[page] = pageState;
  return stateRequest(`${ADMIN_UI_STATE_BASE}/${encodeURIComponent(page)}`, {
    method: "POST",
    body: { state: pageState },
    keepalive,
  }).catch(() => null);
}

function schedulePageStateSave() {
  clearTimeout(pageStateSaveTimer);
  pageStateSaveTimer = setTimeout(() => savePageState(state.page), 750);
}

function recordApiSnapshot(path, payload) {
  if (!path.startsWith(V1) || path.startsWith(ADMIN_UI_STATE_BASE) || payload === undefined) return;
  let key = path;
  try { key = new URL(path, location.origin).pathname; } catch (_error) {}
  const snapshots = state.pageApiSnapshots[state.page] || {};
  try {
    snapshots[key] = JSON.parse(JSON.stringify(payload));
    state.pageApiSnapshots[state.page] = snapshots;
    schedulePageStateSave();
  } catch (_error) {
    // Only JSON-shaped responses can be persisted as page snapshots.
  }
}

function installPageStatePersistence() {
  document.addEventListener("input", schedulePageStateSave);
  document.addEventListener("change", schedulePageStateSave);
  document.addEventListener("click", schedulePageStateSave);
  const observer = new MutationObserver((mutations) => {
    if (mutations.some((mutation) => mutation.target.closest && mutation.target.closest(".page.active"))) {
      if (state.pendingPageRestore[state.page]) {
        applyControlState(state.page, state.restoredPageStates[state.page]);
        state.pendingPageRestore[state.page] = false;
      }
      schedulePageStateSave();
    }
  });
  observer.observe($("app"), {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["open", "aria-pressed", "data-state"],
  });
  window.addEventListener("beforeunload", () => savePageState(state.page, true));
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
  recordApiSnapshot(path, payload);
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
    manualPaused: false,
    hoverPaused: false,
    scrollPaused: false,
    get paused() { return this.manualPaused || this.hoverPaused || this.scrollPaused || hasSelectionIn(opts.scrollId); },
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
    append(event, noDraw = false) {
      if (!this.filter(event)) { this.hidden += 1; if (!noDraw) this.updateCounts(); return; }
      if (opts.newestFirst) this.rows.unshift(event);
      else this.rows.push(event);
      if (this.rows.length > MAX_BUFFER) {
        if (opts.newestFirst) this.rows.pop();
        else this.rows.shift();
      }
      if (this.follow) {
        if (!noDraw) { this.draw(); this.scrollToEnd(); }
      } else {
        this.unseen += 1;
        if (!noDraw) { jump.classList.add("visible"); if (unseenLabel) unseenLabel.textContent = this.unseen; this.draw(); }
      }
      if (!noDraw) this.updateCounts();
    },
    rebuild(source) {
      this.rows = source.filter(this.filter);
      if (opts.newestFirst) {
        this.rows.sort((a, b) => {
          const at = Date.parse(a.ts || "") || 0;
          const bt = Date.parse(b.ts || "") || 0;
          if (at !== bt) return bt - at;
          return (b.seq || 0) - (a.seq || 0);
        });
      }
      this.hidden = source.length - this.rows.length;
      this.draw();
      this.updateCounts();
    },
    updateCounts() {
      if (opts.countsId) $(opts.countsId).textContent = `${this.rows.length} shown · ${this.hidden} hidden`;
    },
    draw() {
      const cap = opts.capFn ? opts.capFn() : MAX_BUFFER;
      const rows = opts.newestFirst ? this.rows.slice(0, cap) : this.rows.slice(-cap);
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
    scrollToEnd() { scroll.scrollTop = opts.newestFirst ? 0 : scroll.scrollHeight; },
    setPaused(value) {
      const wasPaused = this.paused;
      this.manualPaused = value;
      this.updatePauseState(wasPaused);
    },
    setHoverPaused(value) {
      const wasPaused = this.paused;
      this.hoverPaused = value;
      this.updatePauseState(wasPaused);
    },
    setScrollPaused(value) {
      const wasPaused = this.paused;
      this.scrollPaused = value;
      this.updatePauseState(wasPaused);
    },
    updatePauseState(wasPaused) {
      if (wasPaused && !this.paused && this.pending.length > 0) {
        this.pending.forEach((e) => this.append(e, true));
        this.pending = [];
        this.updateCounts();
        if (this.follow) {
          this.draw();
          this.scrollToEnd();
        } else {
          jump.classList.add("visible");
          if (unseenLabel) unseenLabel.textContent = this.unseen;
          this.draw();
        }
      }
    },
    clearView() { this.rows = []; this.hidden = 0; this.draw(); this.updateCounts(); },
  };

  scroll.addEventListener("mouseenter", () => {
    view.setHoverPaused(true);
  });
  scroll.addEventListener("mouseleave", () => {
    view.setHoverPaused(false);
  });

  scroll.addEventListener("scroll", () => {
    const atBottom = opts.newestFirst ? scroll.scrollTop < 40 : scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
    // Automatic pause when the operator scrolls upward; never force the view down.
    view.follow = atBottom;
    if (atBottom) {
      view.unseen = 0;
      jump.classList.remove("visible");
      view.setScrollPaused(false);
    } else {
      view.setScrollPaused(true);
    }
    view.draw();
  });
  jump.addEventListener("click", () => {
    view.follow = true;
    view.unseen = 0;
    jump.classList.remove("visible");
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
    view.setHoverPaused(false); // force unpause on click so it jumps immediately
    view.setScrollPaused(false);
    view.draw();
    view.scrollToEnd();
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

function renderBrowserNavRow(event) {
  const data = event.data || {};
  const profile = data.chrome_profile || {};
  const row = el("div", "event-row browser-nav-row");
  row.dataset.source = event.source_kind || "system";
  if (["denied", "blocked", "unexpected-auth-landing"].includes(data.outcome)) row.classList.add("browser-nav-danger");
  if (data.outcome === "awaiting-consent") row.classList.add("browser-nav-awaiting");
  if (["approved", "consent-disabled"].includes(data.outcome)) row.classList.add("browser-nav-approved");
  if (data.identity_mismatch) row.classList.add("browser-nav-mismatch");
  const detail = data.detail || "";
  row.title = [
    detail,
    data.caller ? `caller: ${data.caller}` : "",
    data.cdp_endpoint ? `cdp: ${data.cdp_endpoint}` : "",
    data.sso_intent ? `SSO intent: ${data.sso_intent}` : "",
    data.identity_provider ? `provider: ${data.identity_provider}` : "",
    data.tab_id ? `tab: ${data.tab_id}` : "",
    data.error ? `error: ${data.error}` : "",
  ].filter(Boolean).join("\n");
  row.append(
    el("span", "ev-ts", shortTs(data.ts || event.ts)),
    el("span", "mono", `${data.pid || "?"} / ${data.instance || "?"}`),
    el("span", null, data.role || "unknown"),
    el("span", null, data.backend || "unknown"),
    mono(`${data.resolved_endpoint || data.cdp_endpoint || "?"} / ${profile.slug || profile.display || profile.path || "unknown"}`),
    mono(data.url || ""),
    el("span", null, `${data.reason || "unknown"}${detail ? ` — ${detail}` : ""}`),
    el("span", null, `${data.intended_identity || "—"} / ${data.effective_identity || "—"}`),
    el("span", null, data.identity_mismatch ? "mismatch" : (data.ambient_identity ? "ambient" : (data.identity_mode || "—"))),
    badge(data.outcome || "unknown", ["failed", "denied", "blocked", "unexpected-auth-landing"].includes(data.outcome) || data.identity_mismatch ? "danger" : (["approved", "consent-disabled", "opened", "navigated", "foregrounded", "reused-existing-tab"].includes(data.outcome) ? "ok" : "")),
  );
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
async function showPage(page) {
  const previousPage = state.page;
  if (previousPage && previousPage !== page) await savePageState(previousPage);
  state.page = page;
  await restorePageState(page);
  document.querySelectorAll(".page").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.page === page));
  const pageTitles = { browser: "SSO / Browser" };
  $("top-title").textContent = pageTitles[page] || (page.charAt(0).toUpperCase() + page.slice(1));
  // Keep the URL in sync so any page can be deep-linked and reloaded in place.
  if (location.hash.slice(1) !== page) history.replaceState(null, "", `#${page}`);
  const loaders = {
    workers: loadWorkers, alerts: loadAlerts, devices: loadDevices, voices: loadVoices,
    accuracy: loadAccuracy, cursors: loadCursors, prompt: loadPrompt, system: loadSystem,
    meet: loadMeetWithPolling, silences: loadSilencesWithPolling, stt: loadStt, processes: loadProcesses, browser: loadBrowserSettings,
  };
  if (loaders[page]) {
    const p = loaders[page]();
    if (page === "stt" && (window.SpeechRecognition || window.webkitSpeechRecognition)) {
      if (!sttShouldContinue && !sttRecognizer) {
        toggleSttMic();
      }
    }
  } else {
    applyControlState(page, state.restoredPageStates[page]);
    state.pendingPageRestore[page] = false;
  }
  Object.values(state.views).forEach((v) => v.draw());
}

function pageFromHash() {
  const requested = location.hash.slice(1) === "sso" ? "browser" : location.hash.slice(1);
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
  // A "virtual" direction (a cable's two sides, both collapsed into this one
  // bucket) has no case above, so it fell through to "unknown" here — which,
  // combined with the "Unknown" filter defaulting to hidden, silently
  // dropped EVERY virtual device from the table by default. Use the real
  // capture/playback capability (independent of the collapsed direction
  // label) to place it correctly instead.
  if (device.direction === "virtual") {
    if (device.supports_input) return "input";
    if (device.supports_output) return "output";
  }
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
  cap_in: "show", cap_out: "show", row_hidden: "hide",
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

/* ---- per-row manual Enabled/Disabled toggle: UI-only display control, kept
 * separate from the routing/engine device lists (this never changes which
 * devices appear in the "capture"/"speak through" dropdowns elsewhere on the
 * page — it only hides the row from THIS table). Persisted per device id. */
function rowEnabled(deviceId) {
  return localStorage.getItem(`ws_collab_devrow_${deviceId}`) !== "disabled";
}
function setRowEnabled(deviceId, enabled) {
  if (enabled) localStorage.removeItem(`ws_collab_devrow_${deviceId}`);
  else localStorage.setItem(`ws_collab_devrow_${deviceId}`, "disabled");
}

/* ---- column sort state for the devices table (persisted across reloads) */
function getDeviceSort() {
  try { return JSON.parse(localStorage.getItem("ws_collab_devsort") || "null") || { key: null, dir: 1 }; }
  catch { return { key: null, dir: 1 }; }
}
function setDeviceSort(sort) { localStorage.setItem("ws_collab_devsort", JSON.stringify(sort)); }

/* ---- persisted, drag-to-resize column widths for the devices table ---- */
function getColumnWidth(label) {
  const raw = localStorage.getItem(`ws_collab_devcolwidth_${label}`);
  return raw ? parseInt(raw, 10) : null;
}
function setColumnWidth(label, px) {
  localStorage.setItem(`ws_collab_devcolwidth_${label}`, String(Math.max(24, Math.round(px))));
}

/* Renders a <tr> of <th> cells; any column with a `key` is clickable and
 * cycles ascending -> descending -> unsorted, persisting the choice. Every
 * header also gets a drag handle on its right edge to resize that column
 * (width persisted per label, applied via the matching <col> in `colEls`). */
function sortableHeaderRow(columns, sort, onChange, colEls) {
  const hr = el("tr");
  columns.forEach((col, index) => {
    const th = el("th");
    th.style.position = "relative";
    if (!col.key) {
      th.textContent = col.label;
    } else {
      const active = sort.key === col.key;
      const arrow = active ? (sort.dir === 1 ? " \u25B2" : " \u25BC") : "";
      const button = el("button", "sort-header", col.label + arrow);
      button.type = "button";
      button.title = "Click to sort by this column";
      button.onclick = () => {
        let next;
        if (!active) next = { key: col.key, dir: 1 };
        else if (sort.dir === 1) next = { key: col.key, dir: -1 };
        else next = { key: null, dir: 1 };
        setDeviceSort(next);
        onChange();
      };
      th.appendChild(button);
    }
    if (colEls && colEls[index]) {
      const handle = el("span", "col-resize-handle");
      handle.title = "Drag to resize this column";
      handle.addEventListener("mousedown", (event) => {
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = colEls[index].getBoundingClientRect().width;
        const onMove = (moveEvent) => {
          colEls[index].style.width = `${Math.max(24, startWidth + (moveEvent.clientX - startX))}px`;
        };
        const onUp = () => {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          setColumnWidth(col.label, colEls[index].getBoundingClientRect().width);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
      th.appendChild(handle);
    }
    hr.appendChild(th);
  });
  return hr;
}

/* Resolves a column's sort key to a comparable value for one device. Most
 * keys are a direct Device field; a few columns show something DERIVED
 * (a class-membership checkmark, a joined rates list, the composite
 * "default" flags string, or an action's own eligibility) that has no
 * single matching Device field, so those are computed here instead —
 * every column stays sortable, not just the ones with a 1:1 field. */
function deviceSortValue(d, key) {
  switch (key) {
    case "class_virtual": return (d.classes || []).includes("virtual");
    case "class_physical": return (d.classes || []).includes("physical");
    case "class_loopback": return (d.classes || []).includes("loopback");
    case "rates": return (d.sample_rates || []).join(",");
    case "default": return [d.is_default_input && "in", d.is_default_output && "out", d.is_default_comm && "comm"].filter(Boolean).join("/");
    case "row_enabled": return rowEnabled(d.id);
    case "select_eligible": return (deviceGroup(d) === "input" || d.direction === "output" || d.direction === "virtual") && !!d.available;
    default: return d[key];
  }
}

/* Sorts decorated rows ({device, state, reason}) by a device sort key. */
function sortDeviceRows(rows, sort) {
  if (!sort.key) return rows;
  const { key, dir } = sort;
  // A field like `classes` can be a list (a device may genuinely carry more
  // than one classification at once). Join multi-value lists into a
  // sortable string as the general case; only fall back to the bare single
  // value last, as a special case for the (common) single-class device.
  const sortValue = (v) => {
    if (!Array.isArray(v)) return v;
    if (v.length !== 1) return v.slice().sort().join(",");
    return v[0];
  };
  return rows.slice().sort((a, b) => {
    const av = sortValue(deviceSortValue(a.device, key));
    const bv = sortValue(deviceSortValue(b.device, key));
    if (typeof av === "boolean" || typeof bv === "boolean") return (Number(!!av) - Number(!!bv)) * dir;
    if (typeof av === "number" || typeof bv === "number") return ((av || 0) - (bv || 0)) * dir;
    return String(av ?? "").localeCompare(String(bv ?? "")) * dir;
  });
}

function connectedLight(available) {
  const dot = el("span", `status-dot ${available ? "ok" : "danger"}`, "");
  dot.title = available ? "Connected" : "Not connected / unavailable";
  return dot;
}

function checkMark(truthy) {
  return el("span", "mono", truthy ? "\u2713" : "\u2014");
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
  // Raw capture/playback capability (independent of the collapsed
  // "direction" category above — e.g. a virtual cable's two sides both
  // report direction "virtual" but only one of them actually captures).
  if (device.supports_input) cats.push("cap_in");
  if (device.supports_output) cats.push("cap_out");
  // The manual per-row Enabled/Disabled toggle (UI-only, see rowEnabled()).
  if (!rowEnabled(device.id)) cats.push("row_hidden");
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

/* Persisted "which host API variant is shown" choice per device identity
 * (name+direction) — a device registers once per host API (MME,
 * DirectSound, WASAPI, WDM-KS), each with its own channel count, sample
 * rates, and latency (these differ almost every time — checked live: 23/23
 * duplicate-name devices differ in latency+rates, 22/23 in channels), so
 * there's rarely a single "the same" row to collapse to. Instead, group
 * one row per identity with a clickable pill per host API; whichever pill
 * is selected drives that row's Channels/Rates/Latency/actions. */
function getSelectedHostApi(key) {
  return localStorage.getItem(`ws_collab_devhostapi_${key}`);
}
function setSelectedHostApi(key, hostApi) {
  localStorage.setItem(`ws_collab_devhostapi_${key}`, hostApi);
}

function groupDevicesByIdentity(rows) {
  const groups = [];
  const byKey = new Map();
  rows.forEach((row) => {
    const key = `${row.device.name}|${row.device.direction}`;
    let group = byKey.get(key);
    if (!group) { group = { key, variants: [] }; byKey.set(key, group); groups.push(group); }
    group.variants.push(row);
  });
  return groups.map(({ key, variants }) => {
    if (variants.length === 1) return { ...variants[0], variants };
    const saved = getSelectedHostApi(key);
    const selected = variants.find((v) => v.device.host_api === saved) || variants[0];
    return { ...selected, variants, identityKey: key };
  });
}

async function loadDevices() {
  try {
    const [devices, capture, routing, secondary] = await Promise.all([
      api(`${V1}/audio/devices`), api(`${V1}/audio/capture`), api(`${V1}/audio/routing`), api(`${V1}/audio/secondary-capture`),
    ]);

    const capPanel = panel("Capture state");
    const sens = capture.mic_sensitivity;
    capPanel.content.appendChild(kv({
      listening: capture.listening, privacy: capture.privacy_indicator,
      device: capture.device_name || capture.device_id,
      backend: capture.backend, live_capture: capture.live_capture,
      echo_policy: capture.echo_policy, meter: capture.meter_level, peak: capture.peak_level,
      clipping: capture.clipping, captured: capture.captured,
      dropped_echo: capture.dropped_echo, dropped_frames: capture.dropped_frames,
      // Adaptive VAD: the gate quietly gets more sensitive the longer it hears
      // nothing, hunting for a signal, then snaps back once something crosses it.
      mic_sensitivity: sens ? `${sens.current_threshold} / baseline ${sens.base_threshold}${sens.hunting ? " (hunting for a signal…)" : ""}` : "—",
      error: capture.error || "—",
    }));
    $("dv-capture").replaceChildren(capPanel.root);

    const secondaryPanel = panel("Secondary capture — Meet companion incoming audio");
    const secondaryControls = el("div", "toolbar");
    const captureDevicesForSecondary = (devices.devices || []).filter((d) => ["input", "loopback", "virtual"].includes(d.direction) && d.available);
    const secondarySelect = el("select");
    captureDevicesForSecondary.forEach((d) => secondarySelect.appendChild(new Option(`${d.name} · ${d.host_api} [${d.direction}]`, d.id)));
    if (secondary.device_id) secondarySelect.value = secondary.device_id;
    secondaryControls.append(
      secondarySelect,
      actionButton("Start secondary capture", "", async () => {
        try { await api(`${V1}/audio/secondary-capture/start`, { method: "POST", body: { device_id: secondarySelect.value } }); } catch (error) { pushError(error.message); }
        loadDevices();
      }),
      actionButton("Stop secondary capture", "", async () => {
        try { await api(`${V1}/audio/secondary-capture/stop`, { method: "POST", body: {} }); } catch (error) { pushError(error.message); }
        loadDevices();
      }),
    );
    secondaryPanel.content.append(secondaryControls, kv({
      listening: secondary.listening,
      device: secondary.device_name || secondary.device_id || "—",
      backend: secondary.backend,
      live_capture: secondary.live_capture,
      meter: secondary.meter_level,
      peak: secondary.peak_level,
      clipping: secondary.clipping,
      captured: secondary.captured,
      input_mode: secondary.input_mode,
      browser_connected: secondary.browser_connected,
      browser_muted: secondary.browser_muted,
      browser_stream: secondary.browser_stream_id || "—",
      chunks_forwarded: secondary.chunks_forwarded,
      frames_forwarded: secondary.frames_forwarded,
      bytes_forwarded: secondary.bytes_forwarded,
      segments_forwarded: secondary.segments_forwarded,
      queued_chunks: `${secondary.queued_chunks} / ${secondary.queue_capacity}`,
      dropped_frames: secondary.dropped_frames,
      dropped_chunks: secondary.dropped_chunks,
      dropped_bytes: secondary.dropped_bytes,
      artifact_chunks_suppressed: secondary.dropped_artifact_chunks,
      disconnects: secondary.browser_disconnects,
      reconnects: secondary.browser_reconnects,
      error: secondary.error || "—",
    }));
    $("dv-secondary-capture").replaceChildren(secondaryPanel.root);

    const all = devices.devices || [];
    const decorated = all.map((d) => {
      const verdict = deviceVerdict(d);
      return { device: d, state: verdict.state, reason: verdict.reason };
    });
    const shownCount = decorated.filter((r) => r.state === "show").length;
    const neutralCount = decorated.filter((r) => r.state === "neutral").length;
    // "row_hidden" (the per-row Enabled/Disabled toggle) is now just another
    // category, so it folds into the same hide/show/neutral pipeline as
    // every other filter — the "Hidden by you" filter button can bring
    // individually-disabled rows back into view (show/neutral) same as any
    // other category.
    const visible = sortDeviceRows(decorated.filter((r) => r.state !== "hide"), getDeviceSort());
    $("dv-counts").textContent =
      `${shownCount} shown · ${neutralCount} neutral · ${all.length - visible.length} hidden`;

    const target = $("dv-table");
    target.replaceChildren();
    if (!visible.length) {
      target.appendChild(el("div", "hint", "No devices match the current filters."));
    } else {
      const COLUMNS = [
        { label: "ID", key: "id", width: 70 }, { label: "Name", key: "name", width: 280 },
        { label: "I", key: "supports_input", width: 32 }, { label: "O", key: "supports_output", width: 32 },
        { label: "V", key: "class_virtual", width: 32 }, { label: "P", key: "class_physical", width: 32 },
        { label: "L", key: "class_loopback", width: 32 },
        { label: "Class", key: "classes", width: 150 },
        { label: "Host API", key: "host_api", width: 80 }, { label: "Ch", key: "channels", width: 40 },
        { label: "Rates", key: "rates", width: 100 }, { label: "Latency", key: "latency_ms", width: 80 },
        { label: "Default", key: "default", width: 80 }, { label: "Available", key: "available", width: 90 },
        { label: "Select", key: "select_eligible", width: 70 }, { label: "Disabled", key: "row_enabled", width: 80 },
        { label: "Hide", key: "row_enabled", width: 60 }, { label: "Connected", key: "available", width: 100 },
      ];
      const sort = getDeviceSort();
      const renderRows = groupDevicesByIdentity(visible);
      const t = el("table");
      t.classList.add("dv-resizable-table");
      const colgroup = el("colgroup");
      // Every column always gets a starting width — the saved (user-resized)
      // one if present, else its own sane default — so `table-layout: fixed`
      // never falls back to an even 1/18th split, which squeezed columns
      // like Name down to ~50px and truncated device names unreadably.
      const colEls = COLUMNS.map((col) => {
        const c = el("col");
        c.style.width = `${getColumnWidth(col.label) || col.width || 80}px`;
        colgroup.appendChild(c);
        return c;
      });
      t.appendChild(colgroup);
      const thead = el("thead");
      thead.appendChild(sortableHeaderRow(COLUMNS, sort, loadDevices, colEls));
      t.appendChild(thead);
      const tbody = el("tbody");
      renderRows.forEach(({ device: d, state, reason, variants, identityKey }) => {
        const enabledKey = identityKey || d.id;
        const enabled = rowEnabled(enabledKey);
        // `d.classes` is the real, possibly multi-valued classification
        // (a device can be BOTH "physical" and "loopback" at once, e.g. a
        // real Stereo Mix device) — never a single mutually-exclusive
        // bucket. `deviceClass(d)` (used only by the existing category
        // filter buttons/legacy grouping) still returns one bucket for
        // backward compatibility with that older logic.
        const classes = d.classes || [];
        const tr = el("tr");
        if (state === "neutral") tr.classList.add("filtered-out");
        // Host API cell: a plain label when there's only one, otherwise one
        // clickable pill per host API this identity registers under —
        // clicking a pill switches which variant's Channels/Rates/Latency/
        // actions this row shows (they legitimately differ per host API;
        // see groupDevicesByIdentity for how often that's actually true).
        const hostApiCell = el("span");
        if (variants && variants.length > 1) {
          variants.forEach((v) => {
            const isSelected = v.device.host_api === d.host_api;
            const pill = actionButton(v.device.host_api, isSelected ? "primary" : "", () => {
              setSelectedHostApi(identityKey, v.device.host_api);
              loadDevices();
            });
            pill.title = isSelected ? "Currently shown variant" : `Show the ${v.device.host_api} variant instead`;
            hostApiCell.appendChild(pill);
          });
        } else {
          hostApiCell.appendChild(mono(d.host_api || "—"));
        }
        const cells = [
          mono(d.id), d.name,
          checkMark(d.supports_input), checkMark(d.supports_output),
          checkMark(classes.includes("virtual")), checkMark(classes.includes("physical")), checkMark(classes.includes("loopback")),
          (() => {
            const wrap = el("span");
            classes.forEach((c) => wrap.appendChild(badge(c, c === "physical" ? "ok" : c === "loopback" ? "purple" : "")));
            return wrap;
          })(),
          hostApiCell, String(d.channels),
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
          // Toggle: flips between Enabled <-> Disabled, re-enterable from
          // here once a hidden row is brought back via the "Hidden by you"
          // filter button. Keyed by device identity (name+direction) when
          // this row groups multiple host APIs, so Disable/Hide applies to
          // the logical device regardless of which pill is selected.
          actionButton(enabled ? "Enabled" : "Disabled", "row-enabled-toggle", () => {
            setRowEnabled(enabledKey, !enabled);
            loadDevices();
          }),
          // One-click shortcut that immediately hides the row (same
          // underlying flag as the Disabled toggle above, offered as its
          // own explicit action for a faster "get this out of my way").
          enabled
            ? actionButton("Hide", "", () => { setRowEnabled(enabledKey, false); loadDevices(); })
            : "—",
          connectedLight(d.available),
        ];
        cells.forEach((cell) => {
          const td = el("td");
          if (cell instanceof Node) td.appendChild(cell); else td.textContent = esc(cell);
          tr.appendChild(td);
        });
        const enabledButton = tr.querySelector("button.row-enabled-toggle");
        if (enabledButton) enabledButton.dataset.enabled = String(enabled);
        tbody.appendChild(tr);
      });
      t.appendChild(tbody);
      target.appendChild(t);
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

/* ---- server-managed Google Meet bridge */
async function postMeetCommand(command) {
  const resultEl = $("meet-command-result");
  resultEl.textContent = `${command} — sending…`;
  try {
    const result = await api(`${MEET_BRIDGE_BASE}/command`, { method: "POST", body: { command } });
    resultEl.textContent = `${command} → ${result.verdict}`;
    if (result.started) setTimeout(loadMeet, 2000);
  } catch (error) {
    resultEl.textContent = `${command} → error: ${error.message}`;
  }
  loadMeet();
}

async function postMeetSso(path, body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  const resultEl = $("br-result");
  const label = body.add_account ? "add-account" : body.account_id || "profile";
  resultEl.textContent = `${path} - sending...`;
  try {
    const result = await api(`${V1}${path}`, { method: "POST", body });
    resultEl.textContent = result.warning
      ? `${label} - ok (${result.warning})`
      : `${label} - ok`;
  } catch (error) {
    resultEl.textContent = `${label} - error: ${error.message}`;
  }
  loadBrowserSettings();
}

async function scanMeetSsoAccounts() {
  const result = $("br-result");
  result.textContent = "scanning live Google sessions...";
  try {
    const ssoState = await api(`${V1}/meet/sso/scan`, { method: "POST", body: {} });
    await loadBrowserSettings(ssoState);
    result.textContent = ssoState.ready_for_meet
      ? `scan complete: ${ssoState.signed_in_count} signed-in accounts; ready for Meet role assignment`
      : `scan complete: ${ssoState.signed_in_count || 0}/2 signed-in accounts`;
  } catch (error) {
    result.textContent = `scan error: ${error.message}`;
  }
}

async function loadBrowserSettings(scannedSsoState = null) {
  const body = $("br-settings");
  const result = $("br-result");
  try {
    const data = await api(`${V1}/meet/browser-settings`);
    let accounts = data.accounts || [];
    let ssoState = scannedSsoState || { accounts };
    if (!scannedSsoState) {
      try {
        ssoState = await api(`${V1}/meet/sso/accounts`);
        accounts = ssoState.accounts || accounts;
      } catch (_error) {
        // A running pre-upgrade server already includes accounts in its browser
        // settings response; use them until that process is manually restarted.
      }
    } else {
      accounts = scannedSsoState.accounts || accounts;
    }
    const backend = el("select");
    ["windows", "wsl"].forEach((value) => backend.appendChild(new Option(value, value)));
    backend.value = data.browser_backend || "windows";
    const profile = el("input");
    profile.type = "text";
    profile.style.width = "100%";
    profile.value = data.profile_path || "";
    const requireConsent = el("input");
    requireConsent.type = "checkbox";
    requireConsent.id = "br-require-sso-consent";
    requireConsent.checked = data.require_sso_consent === true;
    const requireConsentLabel = el("label");
    requireConsentLabel.htmlFor = requireConsent.id;
    requireConsentLabel.append(
      requireConsent,
      el("span", null, " Require confirmation before opening identity-provider sign-in pages"),
    );
    const preview = el("div", "mono");
    const browserPanel = panel("Browser");
    const ssoPanel = panel("SSO");
    const paintPreview = () => {
      const cmd = [
        "ws-collab-meet-bridge",
        "--profile",
        profile.value || "",
        "--browser-backend",
        backend.value,
        "--companion",
      ];
      const rendered = cmd.map((part) => (part.includes(" ") ? `"${part}"` : part)).join(" ");
      preview.replaceChildren(
        mono(rendered),
        el("span", null, " "),
        copyTextLink("copy", rendered, "Copy next-launch command"),
      );
    };
    backend.onchange = paintPreview;
    profile.oninput = paintPreview;
    browserPanel.content.append(
      el("div", "hint", "Browser backend and profile changes apply on the next bridge launch. The confirmation setting applies to subsequent navigations in running server-managed bridges."),
      table(
        ["Setting", "Value"],
        [
          ["Browser backend", backend],
          ["Profile path", profile],
          ["Authentication confirmation", requireConsentLabel],
        ],
      ),
      el("div", "hint", "When confirmation is off, explicitly intended SSO actions may navigate without a dialog. Typed SSO intent is still required, and every navigation is still logged."),
      el("div", "hint", "Browser launch base (choose role accounts on the Google Meet page before starting)"),
      preview,
    );

    const ssoActions = el("div", "toolbar");
    ssoActions.append(
      actionButton("Scan signed-in accounts", "", scanMeetSsoAccounts),
      actionButton("Sign in another account", "primary", () => postMeetSso("/meet/sso/open", { add_account: true })),
      actionButton("Forget browser profile", "danger", () => postMeetSso(
        "/meet/sso/forget",
        {},
        "Forget every saved Google sign-in in the Meet browser profile? This deletes the profile directory on disk.",
      )),
    );
    ssoPanel.content.append(
      el(
        "div",
        "hint",
        ssoState.ready_for_meet
          ? `${ssoState.signed_in_count} live Google sessions confirmed. Meet drivers may start after roles are assigned on the Google Meet page.`
          : `${ssoState.signed_in_count || 0}/2 live Google sessions confirmed. Meet drivers remain blocked.`,
      ),
      el("div", "hint", "The browser uses one Chrome profile/process. Signed-in Google accounts are tracked by stable local IDs even if Google changes their authuser slots."),
      ssoActions,
      table(
        ["Local ID", "Email", "Authuser", "Live session", "Actions"],
        accounts.length
          ? accounts.map((account) => {
              const actions = el("span", "meet-row-actions");
              actions.append(actionButton("Open", "mini", () => postMeetSso("/meet/sso/open", { account_id: account.id })));
              return [
                account.id || "-",
                account.email || account.label || "unknown",
                account.authuser == null ? "-" : String(account.authuser),
                account.signed_in ? "Signed in" : "Not confirmed",
                actions,
              ];
            })
          : [["-", "No signed-in accounts are known yet. Use 'Sign in another account', then refresh.", "-", "-", "-"]],
      ),
    );

    $("br-save").onclick = async () => {
      try {
        const saved = await api(`${V1}/meet/browser-settings`, {
          method: "POST",
          body: {
            browser_backend: backend.value,
            profile_path: profile.value,
            require_sso_consent: requireConsent.checked,
          },
        });
        result.textContent = "saved";
        profile.value = saved.profile_path || profile.value;
        requireConsent.checked = saved.require_sso_consent === true;
        paintPreview();
        loadBrowserSettings();
      } catch (error) {
        result.textContent = `error: ${error.message}`;
      }
    };

    paintPreview();
    body.replaceChildren(browserPanel.root, ssoPanel.root);
    if (state.views.browserNav && BROWSER_NAV_STREAMS.length) {
      backfill(BROWSER_NAV_STREAMS, state.views.browserNav);
    }
  } catch (error) {
    body.replaceChildren(el("div", "hint", `error loading browser settings: ${error.message}`));
  }
}

function meetAccountLabel(account) {
  if (!account) return "Unassigned";
  return meetShortIdentity(account.email) || meetShortIdentity(account.label) || meetStableSso(account.id) || "Unassigned";
}

function meetAssignmentKey(url) {
  try {
    const parsed = new URL(url);
    const room = parsed.pathname.split("/").filter(Boolean)[0];
    return room ? `https://meet.google.com/${room.toLowerCase()}` : url;
  } catch (_error) {
    return url;
  }
}

function rememberMeetKnownUrls(urls) {
  const known = new Set(state.meetKnownUrls.map(meetAssignmentKey).filter(Boolean));
  (urls || []).forEach((url) => {
    const key = meetAssignmentKey(url);
    if (key) known.add(key);
  });
  state.meetKnownUrls = [...known];
}

function meetRoleOverride(url, role) {
  const data = state.meetGlobalAssignments || {};
  const overrides = (data.meeting_role_account_maps || {})[meetAssignmentKey(url)];
  if (!overrides || typeof overrides !== "object" || !(role in overrides)) return "__default__";
  return overrides[role] || "__default__";
}

function meetEffectiveRoleMap(url) {
  const data = state.meetGlobalAssignments || {};
  const effective = { ...(data.role_account_map || {}) };
  const overrides = (data.meeting_role_account_maps || {})[meetAssignmentKey(url)];
  if (overrides && typeof overrides === "object") {
    Object.entries(overrides).forEach(([role, accountId]) => {
      if (accountId) effective[role] = accountId;
    });
  }
  return effective;
}

function meetSsoCombo(url, role) {
  const data = state.meetGlobalAssignments;
  if (!data) return "\u2014";
  const accounts = data.accounts || [];
  const byId = Object.fromEntries(accounts.map((account) => [account.id, account]));
  const defaultAccount = byId[(data.role_account_map || {})[role]];
  const select = el("select");
  select.dataset.role = role;
  select.dataset.meetingUrl = meetAssignmentKey(url);
  select.appendChild(new Option(`(default) ${meetAccountLabel(defaultAccount)}`, "__default__"));
  accounts.forEach((account) => select.appendChild(new Option(meetAccountLabel(account), account.id)));
  select.value = meetRoleOverride(url, role);
  select.title = `Select the ${role.toUpperCase()} SSO for this meeting.`;
  select.onclick = (event) => event.stopPropagation();
  select.onchange = async () => {
    const previous = meetRoleOverride(url, role);
    const changes = { [role]: select.value };
    select.disabled = true;
    try {
      await api(`${V1}/meet/role-assignments`, {
        method: "POST",
        body: { role_account_map: changes, meeting_url: url },
      });
      state.meetGlobalAssignments = await api(`${V1}/meet/role-assignments`);
      if (state.meetAssignmentScope === meetAssignmentKey(url)) loadMeetRoleAssignments();
    } catch (error) {
      select.value = previous;
      pushError(`Could not assign ${role.toUpperCase()} SSO: ${error.message}`);
    } finally {
      select.disabled = false;
    }
  };
  return select;
}

async function loadMeetRoleAssignments() {
  const body = $("meet-role-settings");
  if (!body) return;
  try {
    const meetingUrl = state.meetAssignmentScope;
    const query = meetingUrl ? `?meeting_url=${encodeURIComponent(meetingUrl)}` : "";
    const data = await api(`${V1}/meet/role-assignments${query}`);
    rememberMeetKnownUrls([
      ...(data.known_meeting_urls || []),
      ...Object.keys(data.meeting_role_account_maps || {}),
      data.meeting_url,
    ]);
    if (!meetingUrl) state.meetGlobalAssignments = data;
    else if (!state.meetGlobalAssignments) {
      state.meetGlobalAssignments = { ...data, role_account_map: data.global_role_account_map || {} };
    }
    const accounts = data.accounts || [];
    const selections = {};
    const rows = (data.role_assignments || []).map((row) => {
      const select = el("select");
      select.dataset.role = row.role;
      select.dataset.meetingUrl = meetingUrl;
      if (meetingUrl) {
        const byId = Object.fromEntries(accounts.map((account) => [account.id, account]));
        const defaultAccount = byId[(data.global_role_account_map || {})[row.role]];
        select.appendChild(new Option(`(default) ${meetAccountLabel(defaultAccount)}`, "__default__"));
      } else {
        select.appendChild(new Option("Unassigned", ""));
      }
      accounts.forEach((account) => {
        select.appendChild(new Option(meetAccountLabel(account), account.id));
      });
      select.value = meetingUrl
        ? (data.role_overrides && data.role_overrides[row.role]) || "__default__"
        : row.account_id || "";
      selections[row.role] = select;
      const actions = el("span", "meet-row-actions");
      actions.append(actionButton("Foreground", "mini", async () => {
        const accountId = select.value === "__default__" ? row.account_id : select.value;
        const roleLabel = (row.role || "role").toUpperCase();
        if (!accountId) {
          result.textContent = `${roleLabel}: no SSO account is assigned`;
          return;
        }
        result.textContent = `${roleLabel}: checking for an existing browser page...`;
        try {
          const foregrounded = await api(`${V1}/meet/sso/foreground`, {
            method: "POST",
            body: { account_id: accountId },
          });
          result.textContent = `${roleLabel}: ${foregrounded.verdict}`;
        } catch (error) {
          result.textContent = `${roleLabel}: foreground error: ${error.message}`;
        }
      }));
      return [
        (row.role || "-").toUpperCase(),
        select,
        row.account_id ? (row.tab_exists ? "Exists" : "None") : "Unassigned",
        actions,
      ];
    });
    const result = el("span", "mono hint");
    const save = actionButton("Save role assignments", "primary", async () => {
      try {
        const saved = await api(`${V1}/meet/role-assignments`, {
          method: "POST",
          body: {
            role_account_map: Object.fromEntries(
              Object.entries(selections)
                .map(([role, select]) => [role, select.value]),
            ),
            meeting_url: meetingUrl,
          },
        });
        if (!meetingUrl) state.meetGlobalAssignments = saved;
        else state.meetGlobalAssignments = await api(`${V1}/meet/role-assignments`);
        result.textContent = meetingUrl
          ? "saved for this meeting"
          : "saved as global defaults for unconfigured meetings";
        loadMeetRoleAssignments();
      } catch (error) {
        result.textContent = `error: ${error.message}`;
      }
    });
    const toolbar = el("div", "toolbar");
    toolbar.append(save, result);
    if (meetingUrl) {
      toolbar.insertBefore(actionButton("Use global defaults", "mini", async () => {
        try {
          await api(`${V1}/meet/role-assignments?meeting_url=${encodeURIComponent(meetingUrl)}`, {
            method: "DELETE",
          });
          state.meetGlobalAssignments = await api(`${V1}/meet/role-assignments`);
          result.textContent = "meeting override removed";
          loadMeetRoleAssignments();
        } catch (error) {
          result.textContent = `error: ${error.message}`;
        }
      }), result);
    }
    const scopeRow = el("div", "field");
    scopeRow.appendChild(el("label", null, "Assignment scope"));
    const scopeSelect = el("select");
    scopeSelect.appendChild(new Option("Global defaults", ""));
    [...new Set(state.meetKnownUrls)].forEach((url) => {
      scopeSelect.appendChild(new Option(`${driverName(url)} - ${url}`, url));
    });
    scopeSelect.value = meetingUrl;
    scopeSelect.onchange = () => {
      state.meetAssignmentScope = scopeSelect.value;
      loadMeetRoleAssignments();
    };
    scopeRow.appendChild(scopeSelect);
    const scopeHint = meetingUrl
      ? ((data.inherited_roles || []).length
        ? `This meeting inherits ${data.inherited_roles.map((role) => role.toUpperCase()).join(" and ")} from the global defaults.`
        : "This meeting has its own role assignments.")
      : "Every meeting without its own override inherits these global defaults.";
    body.replaceChildren(
      el("div", "hint", "Meet roles select SSO accounts. Browser sign-in remains account-only."),
      scopeRow,
      el("div", "hint", scopeHint),
      table(["Meet role", "SSO account", "Browser page", "Actions"], rows),
      toolbar,
      el("div", "hint", "Role arguments for the next launch"),
      mono(data.role_arguments || "ws-collab-meet-bridge --companion"),
    );
  } catch (error) {
    body.replaceChildren(el("div", "hint", `error loading Meet role assignments: ${error.message}`));
  }
}

const COMPANION_FIELD_MAP = {
  intervalSeconds: "meet-companion-interval",
  afterSeconds: "meet-companion-after",
  silenceMs: "meet-companion-silence",
  minGapSeconds: "meet-companion-gap",
  maxWaitSeconds: "meet-companion-max-wait",
  audioRmsThreshold: "meet-companion-rms",
  clickMs: "meet-companion-duration",
  gain: "meet-companion-gain",
  f0Hz: "meet-companion-f0",
  f1Hz: "meet-companion-f1",
  f2Hz: "meet-companion-f2",
};

function companionMeetingKey(value) {
  const text = String(value || "").trim();
  const room = meetRoomId(text) || (/^[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}$/i.test(text) ? text.toLowerCase() : "");
  return room ? `https://meet.google.com/${room}` : "";
}

function setCompanionResult(message, kind = "") {
  const result = $("meet-companion-result");
  if (!result) return;
  result.textContent = message;
  result.classList.toggle("error", kind === "error");
  result.classList.toggle("ok", kind === "ok");
}

function syncCompanionMeetingOptions(currentMeeting = "") {
  const select = $("meet-companion-meeting");
  if (!select) return;
  const current = companionMeetingKey(currentMeeting);
  if (current) rememberMeetKnownUrls([current]);
  const urls = [...new Set(state.meetKnownUrls.map(companionMeetingKey).filter(Boolean))];
  const previous = companionMeetingKey(state.meetCompanion.meetingUrl);
  const selected = previous || current;
  const signature = `${selected}|${current}|${urls.join("|")}`;
  if (select.dataset.signature !== signature) {
    select.replaceChildren(new Option("Select a specific meeting…", ""));
    urls.forEach((url) => {
      const room = meetRoomId(url) || url;
      select.appendChild(new Option(`${room}${url === current ? " (current)" : ""}`, url));
    });
    select.dataset.signature = signature;
  }
  select.value = selected;
  if (selected && selected !== state.meetCompanion.meetingUrl) {
    state.meetCompanion.meetingUrl = selected;
    loadCompanionInterjectorConfig();
  }
}

function paintCompanionConfig(config) {
  const source = $("meet-companion-source");
  const fields = $("meet-companion-fields");
  if (!source || !fields) return;
  if (!config) {
    source.textContent = state.meetCompanion.meetingUrl ? "Loading…" : "No meeting selected";
    source.className = "badge";
    fields.disabled = true;
    return;
  }
  $("meet-companion-enabled").checked = !!config.enabled;
  $("meet-companion-mode").value = config.mode || "reactive";
  $("meet-companion-trigger").value = config.trigger || "caption";
  $("meet-companion-sound").value = ["uh", "uhuh", "hmm"].includes(config.phrase)
    ? config.phrase
    : (["uh", "uhuh", "hmm"].includes(config.sound) ? config.sound : "uh");
  Object.entries(COMPANION_FIELD_MAP).forEach(([key, id]) => {
    $(id).value = config[key] ?? "";
  });
  const inherited = config.source !== "override";
  source.textContent = inherited ? "Inherited global defaults" : "Persisted meeting override";
  source.className = `badge ${inherited ? "" : "ok"}`.trim();
  fields.disabled = false;
  $("meet-companion-reset").disabled = inherited;
  updateCompanionConditionalFields();
}

function updateCompanionConditionalFields() {
  const onSilence = $("meet-companion-mode").value === "reactive";
  document.querySelectorAll(".companion-silence-field").forEach((field) => {
    field.hidden = !onSilence;
    field.querySelectorAll("input,select").forEach((control) => { control.disabled = !onSilence; });
  });
  document.querySelectorAll(".companion-interval-field").forEach((field) => {
    field.hidden = onSilence;
    field.querySelectorAll("input,select").forEach((control) => { control.disabled = onSilence; });
  });
  $("meet-companion-rearm-note").hidden = !onSilence;
}

async function loadCompanionInterjectorConfig() {
  const meetingUrl = companionMeetingKey(state.meetCompanion.meetingUrl);
  if (!meetingUrl) {
    state.meetCompanion.config = null;
    state.meetCompanion.configMeetingUrl = "";
    paintCompanionConfig(null);
    renderCompanionMetrics(state.meetCompanion.health);
    return;
  }
  state.meetCompanion.loading = true;
  paintCompanionConfig(null);
  setCompanionResult("Loading effective settings…");
  try {
    const data = await api(`${V1}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`);
    if (companionMeetingKey(state.meetCompanion.meetingUrl) !== meetingUrl) return;
    rememberMeetKnownUrls(data.knownMeetingUrls || []);
    state.meetCompanion.config = data;
    state.meetCompanion.configMeetingUrl = meetingUrl;
    paintCompanionConfig(data);
    setCompanionResult(
      data.source === "override"
        ? "Showing this meeting’s persisted override."
        : "Showing inherited defaults; saving creates an override.",
    );
  } catch (error) {
    if (companionMeetingKey(state.meetCompanion.meetingUrl) !== meetingUrl) return;
    state.meetCompanion.config = null;
    state.meetCompanion.configMeetingUrl = "";
    paintCompanionConfig(null);
    $("meet-companion-source").textContent = "Settings unavailable";
    $("meet-companion-source").className = "badge danger";
    setCompanionResult(`Could not load settings: ${error.message}`, "error");
  } finally {
    state.meetCompanion.loading = false;
  }
}

function companionPayload(meetingUrl) {
  const form = $("meet-companion-form");
  if (!form.checkValidity()) {
    form.reportValidity();
    throw new Error("Correct the highlighted setting values before saving.");
  }
  const values = {};
  Object.entries(COMPANION_FIELD_MAP).forEach(([key, id]) => {
    values[key] = $(id).valueAsNumber;
  });
  if (!(values.f0Hz < values.f1Hz && values.f1Hz < values.f2Hz)) {
    throw new Error("Frequencies must be ordered f0 < F1 < F2.");
  }
  return {
    meeting_url: meetingUrl,
    enabled: $("meet-companion-enabled").checked,
    interval_seconds: values.intervalSeconds,
    mode: $("meet-companion-mode").value,
    trigger: $("meet-companion-trigger").value,
    after_seconds: values.afterSeconds,
    silence_ms: values.silenceMs,
    min_gap_seconds: values.minGapSeconds,
    max_wait_seconds: values.maxWaitSeconds,
    audio_rms_threshold: values.audioRmsThreshold,
    click_ms: values.clickMs,
    gain: values.gain,
    phrase: $("meet-companion-sound").value,
    sound: "uh",
    f0_hz: values.f0Hz,
    f1_hz: values.f1Hz,
    f2_hz: values.f2Hz,
  };
}

async function saveCompanionInterjector(event) {
  event.preventDefault();
  const meetingUrl = companionMeetingKey(state.meetCompanion.meetingUrl);
  if (!meetingUrl) {
    setCompanionResult("Select a specific meeting before saving.", "error");
    return;
  }
  let body;
  try {
    body = companionPayload(meetingUrl);
  } catch (error) {
    setCompanionResult(error.message, "error");
    return;
  }
  $("meet-companion-fields").disabled = true;
  setCompanionResult(`Saving override for ${meetRoomId(meetingUrl)}…`);
  try {
    const data = await api(`${V1}/meet/companion-click`, { method: "POST", body });
    if (companionMeetingKey(state.meetCompanion.meetingUrl) !== meetingUrl) return;
    state.meetCompanion.config = data;
    state.meetCompanion.configMeetingUrl = meetingUrl;
    paintCompanionConfig(data);
    setCompanionResult(`Saved override for ${meetRoomId(meetingUrl)}.`, "ok");
  } catch (error) {
    setCompanionResult(`Could not save ${meetRoomId(meetingUrl)}: ${error.message}`, "error");
    paintCompanionConfig(state.meetCompanion.configMeetingUrl === meetingUrl ? state.meetCompanion.config : null);
  }
}

async function resetCompanionInterjector() {
  const meetingUrl = companionMeetingKey(state.meetCompanion.meetingUrl);
  if (!meetingUrl) {
    setCompanionResult("Select a specific meeting before resetting.", "error");
    return;
  }
  const room = meetRoomId(meetingUrl);
  if (!confirm(`Remove the companion interjector override for ${room} and use inherited defaults?`)) return;
  $("meet-companion-fields").disabled = true;
  setCompanionResult(`Removing override for ${room}…`);
  try {
    const data = await api(`${V1}/meet/companion-click?meeting_url=${encodeURIComponent(meetingUrl)}`, {
      method: "DELETE",
    });
    if (companionMeetingKey(state.meetCompanion.meetingUrl) !== meetingUrl) return;
    state.meetCompanion.config = data;
    state.meetCompanion.configMeetingUrl = meetingUrl;
    paintCompanionConfig(data);
    setCompanionResult(`Override removed for ${room}; inherited defaults are active.`, "ok");
  } catch (error) {
    setCompanionResult(`Could not reset ${room}: ${error.message}`, "error");
    paintCompanionConfig(state.meetCompanion.configMeetingUrl === meetingUrl ? state.meetCompanion.config : null);
  }
}

function companionMetricValue(value, suffix = "") {
  return value === null || value === undefined || value === "" ? "\u2014" : `${value}${suffix}`;
}

function renderCompanionMetrics(health) {
  const target = $("meet-companion-metrics");
  if (!target) return;
  state.meetCompanion.health = health || null;
  const selected = companionMeetingKey(state.meetCompanion.meetingUrl);
  const active = companionMeetingKey(health && health.meetingUrl);
  const sameMeeting = !!selected && selected === active;
  const candidate = health && health.companionClick && typeof health.companionClick === "object"
    ? health.companionClick
    : null;
  const runtimeMeeting = companionMeetingKey(candidate && candidate.meetingUrl);
  const runtime = sameMeeting && candidate && (!runtimeMeeting || runtimeMeeting === selected) ? candidate : null;
  const outbound = sameMeeting && health && health.companionAudio && typeof health.companionAudio === "object"
    ? health.companionAudio
    : null;
  const queue = runtime && runtime.queue && typeof runtime.queue === "object" ? runtime.queue : outbound;
  let status = "Select a meeting";
  if (selected && !health) status = "Bridge unavailable";
  else if (selected && !sameMeeting) status = active ? `Not current (${meetRoomId(active)})` : "Not currently joined";
  else if (sameMeeting && !runtime) status = "Runtime metrics unavailable (older worker)";
  else if (runtime && !runtime.enabled) status = "Disabled";
  else if (runtime && runtime.lastError) status = "Error";
  else if (runtime && runtime.installed) status = "Eligible · monitoring";
  else if (runtime) status = "Enabled · waiting for companion";
  const lastTime = runtime && (runtime.lastClickIso
    || (Number.isFinite(runtime.lastClickAt) ? new Date(runtime.lastClickAt * 1000).toISOString() : null));
  target.replaceChildren(
    silencesMetric("Status / eligibility", status),
    silencesMetric("Backchannels sent", companionMetricValue(runtime && runtime.clicksSent)),
    silencesMetric("Suppressed / skipped", companionMetricValue(runtime && runtime.suppressed)),
    silencesMetric("Row breaks observed", companionMetricValue(runtime && runtime.rowBreaksObserved)),
    silencesMetric("Phrase last sent", companionMetricValue(runtime && (runtime.lastPhrase || runtime.phrase))),
    silencesMetric("Last trigger mode", companionMetricValue(runtime && runtime.lastTriggerMode)),
    silencesMetric("Last trigger reason", companionMetricValue(runtime && (runtime.lastTriggerReason || runtime.lastTrigger))),
    silencesMetric("Last trigger time", companionMetricValue(runtime && (runtime.lastTriggerIso || lastTime))),
    silencesMetric("Current silence", companionMetricValue(runtime && (runtime.currentSilenceMs ?? runtime.lastSilenceMs), " ms")),
    silencesMetric("Last monologue", companionMetricValue(runtime && runtime.lastMonologueSeconds, " s")),
    silencesMetric("Eligibility", companionMetricValue(runtime && runtime.eligibility)),
    silencesMetric("Companion readiness", runtime
      ? ((runtime.companionReady ?? (outbound && outbound.companionReady)) ? "Ready" : "Not ready")
      : "\u2014"),
    silencesMetric("Output queue", queue
      ? `${queue.speaking ? `speaking ${queue.currentKind || ""}` : "idle"} · ${queue.queued ?? 0} queued`
      : "\u2014"),
  );
  if (runtime && runtime.lastError) setCompanionResult(`Worker error: ${runtime.lastError}`, "error");
}

async function postProcessCommand(command) {
  const resultEl = $("ps-command-result");
  resultEl.textContent = `${command} — sending…`;
  try {
    const result = await api(`${MEET_BRIDGE_BASE}/command`, { method: "POST", body: { command } });
    resultEl.textContent = `${command} → ${result.verdict}`;
  } catch (error) {
    resultEl.textContent = `${command} → error: ${error.message}`;
  }
  loadProcesses();
}

async function loadProcesses() {
  const body = $("ps-body");
  const statusLine = $("ps-status-line");
  try {
    const health = await api(`${MEET_BRIDGE_BASE}/status`);
    statusLine.textContent = health.meetingUrl ? `bridge online — ${health.meetingUrl}` : "bridge online — no meeting";
    const rows = (health.processes || []).map((proc) => {
      const actions = el("span", "meet-row-actions");
      actions.append(
        actionButton("Foreground", "mini", () => postProcessCommand(`/foreground ${proc.role}`)),
        actionButton("Kill process", "mini danger", () => postProcessCommand(`/kill-process ${proc.role}`)),
      );
      const alive = proc.alive === true ? badge("alive", "ok")
        : proc.alive === false ? badge("dead", "danger")
          : "\u2014";
      return [
        (proc.role || "—").toUpperCase(),
        proc.pid == null ? "—" : String(proc.pid),
        proc.backend || "windows",
        proc.port == null ? "—" : String(proc.port),
        proc.profile || "—",
        alive,
        actions,
      ];
    });
    body.replaceChildren(rows.length
      ? table(["Role", "PID", "Backend", "Port", "Profile", "Alive", "Actions"], rows)
      : el("div", "hint", "No bridge-launched browser process is currently tracked. In --attach-only mode this is expected."));
  } catch (error) {
    statusLine.textContent = `bridge offline (${error.message}) — run "ws-collab-meet-bridge"`;
    body.replaceChildren(el("div", "hint", "Bridge unreachable. Start it with \"ws-collab-meet-bridge\"; this page only shows child processes the bridge launched itself."));
  }
}

/* Builds the "Us" rows for a meeting: HOST is real hardware and always
 * listed first (never automated, so its device is just "real"); COMPANION
 * always follows — every driver is structurally a HOST+COMPANION pair, so
 * both rows are always shown, even for a not-current/placeholder driver
 * meeting where there's no LIVE per-client data (falls back to
 * meetingState[roomId] — "as of last time we were here" — when the bridge
 * has ever actually been in that room; plain dashes only when it truly
 * never has). Every row shows the meeting URL + connection state, an SSO
 * column whose combo stores a stable sso_N override for this meeting (or uses
 * the global default), and
 * Actions: for the CURRENT
 * meeting, role-scoped Foreground (raise that identity's browser window)
 * + Disconnect (hang up just that tab) buttons; for a not-current meeting,
 * Join/Rejoin instead (no live tab to foreground/disconnect there). Device
 * detail (Mic/Speak) is only meaningful for the CURRENT meeting — the
 * bridge doesn't retain live per-participant device state for meetings
 * it's left, only the coarser profile/state snapshot. Whether a mic is
 * "physical" or not is already obvious from its Mic text ("real
 * microphone" vs. a device name) — no separate checkbox needed for that. */
function meetUsRows(isCurrent, clients, url, hostProfile, roomSnapshot, kind) {
  const rejoin = (state) => actionButton(state === "in-call" ? "Rejoin" : "Join", "", () => postMeetCommand(`/join ${url}`));
  const captureListening = !!state.meetCaptureListening;
  const actionsFor = (role) => {
    const wrap = el("span", "meet-row-actions");
    if (role === "host") wrap.append(actionButton(captureListening ? "Mute" : "Unmute", "mini", () => toggleMeetCapture(captureListening)));
    wrap.append(
      actionButton("Foreground", "mini", () => postMeetCommand(`/foreground ${role}`)),
      actionButton("Disconnect", "mini danger", () => postMeetCommand(`/disconnect ${role}`)),
    );
    return wrap;
  };
  const ssoLink = (profile) => {
    const account = typeof profile === "object" ? profile.account : null;
    const link = el("a", null, ssoLabel(profile, account));
    link.href = "#browser";
    return link;
  };
  if (kind === "client") {
    const ssoVal = isCurrent ? null : (roomSnapshot && roomSnapshot.hostProfile);
    const action = isCurrent ? actionsFor("guest") : actionButton("Join as guest (not implemented yet)", "mini", () => postMeetCommand("/foreground guest"));
    const rows = [["GUEST_CLIENT", ssoLink(ssoVal), isCurrent ? "not implemented yet" : "not current", meetCopyLink(url), "\u2014", "\u2014", action]];
    return { rows, note: "CLIENT/GUEST mode is designed but not built server-side yet — Foreground/Disconnect will honestly report “not implemented yet”; there is no live guest tab to join/leave." };
  }
  if (!isCurrent) {
    const snapClients = (roomSnapshot && roomSnapshot.clients) || [];
    const snapCompanion = snapClients.find((c) => c.role === "companion");
    const asOf = roomSnapshot && roomSnapshot.updatedAt
      ? ` (as of ${shortTs(new Date(roomSnapshot.updatedAt * 1000).toISOString())})` : "";
    const rows = [
      ["HOST", meetSsoCombo(url, "host"), "not current" + asOf, meetCopyLink(url), "\u2014", "\u2014", rejoin("not current")],
      ["COMPANION", meetSsoCombo(url, "companion"), "not current" + asOf, meetCopyLink(url), "\u2014", "\u2014", rejoin("not current")],
    ];
    return { rows, note: roomSnapshot
      ? "Not the current meeting — SSO/state is the last known snapshot from when the bridge was last here; Join re-attaches the live driver. This driver slot is also available to relay a different real-world audio source into a Meet room here instead — a Discord Voice Channel, Zoom call, or plain audio call, for example — but that is not built yet; every driver today only probes this machine's Physical Computer mic/speakers."
      : "Not the current meeting — never seen live yet, so no snapshot exists; Join attaches the live driver here. This driver slot is also available to relay a different real-world audio source into a Meet room here instead — a Discord Voice Channel, Zoom call, or plain audio call, for example — but that is not built yet; every driver today only probes this machine's Physical Computer mic/speakers." };
  }
  const rows = [["HOST", meetSsoCombo(url, "host"), "in-call", meetCopyLink(url), meetDevicesLink(), meetDevicesLink(), actionsFor("host")]];
  const companion = (clients || []).find((c) => c.role === "companion");
  if (companion) {
    rows.push(["COMPANION", meetSsoCombo(url, "companion"), companion.state || "\u2014", meetCopyLink(url), companion.mic || "\u2014", companion.speak || "\u2014", actionsFor("companion")]);
  } else {
    rows.push(["COMPANION", meetSsoCombo(url, "companion"), "not armed (no --companion)", meetCopyLink(url), "\u2014", "\u2014", "\u2014"]);
  }
  // Any OTHER controlled identity beyond host/companion (e.g. a future
  // CLIENT/GUEST sharing this driver) still gets listed, in whatever order
  // the bridge reported it. Foreground/Disconnect honestly report
  // "not implemented yet" server-side until that identity is real, rather
  // than silently no-op-ing or erroring obscurely.
  (clients || []).filter((c) => c.role !== "companion").forEach((c) => rows.push([
    (c.role || "").toUpperCase(), ssoLink({ label: c.profile, account: c.account }), c.state || "\u2014", meetCopyLink(url), c.mic || "\u2014", c.speak || "\u2014", actionsFor(c.role),
  ]));
  return { rows, note: null };
}

/* ws_collab's own agent voices (the Agent Voices page) are a SEPARATE TTS
 * path from the bridge's own SAPI call in say_into_meeting() — an agent's
 * speech only reaches a Meet call today if something explicitly relayed it
 * through /say (or the google-meet mailbox), never automatically. This
 * table is read-only, informational (real profiles from `${V1}/voices`,
 * enriched server-side with actual TtsEngine/WorkerMonitor activity, never
 * fabricated) -- editing happens on the Agent Voices page, which is where
 * the Agent link goes. Listens/Speaks/Enabled are read-only checkboxes,
 * not toggles, for the same reason. */
function readOnlyCheck(on, title) {
  const box = el("input");
  box.type = "checkbox";
  box.checked = !!on;
  box.disabled = true;
  if (title) box.title = title;
  return box;
}
function meetStatusBadgeKind(status) {
  if (status === "speaking") return "ok";
  if (status === "muted" || status === "unassigned") return "warn";
  if (status && status !== "idle" && status !== "ok") return "danger";
  return "";
}
function meetVirtualAgentRows(agentProfiles, companionSpeak, recipients) {
  // Speaks: when relayed, it goes out through whatever /say itself uses --
  // the companion's synthetic mic patch, or a real virtual-cable device
  // when --tts-output-device is configured -- the exact same `speak`
  // string already shown on the companion's Connectors row.
  const speakTitle = companionSpeak
    ? `Reaches this meeting only via /say, out through ${companionSpeak}`
    : "Reaches this meeting only via /say \u2014 not auto-wired";
  // Listens: there is no per-agent subscription concept yet -- captions
  // are forwarded bridge-wide to a fixed set of mailboxes. Honest bridge-
  // wide signal (same value every row) rather than a fabricated per-agent one.
  const listensOn = (recipients || []).length > 0;
  const listensTitle = listensOn
    ? `Bridge-wide: finished captions are forwarded to ${(recipients || []).join(", ")} (not modeled per-agent yet)`
    : "No caption recipients configured on the bridge";
  const fmtWhen = (at) => (at ? shortTs(new Date(at * 1000).toISOString()) : "\u2014");
  return (agentProfiles || []).map((p) => {
    const link = el("a", null, p.agent_id);
    link.href = "#voices";
    link.title = "Open Agent Voices to edit this profile";
    return [
      link,
      p.voice_id || "(unset)",
      readOnlyCheck(listensOn, listensTitle),
      readOnlyCheck(p.speaking_permission !== false, speakTitle),
      readOnlyCheck(!!p.voice_id, p.voice_id ? "Has a voice assigned" : "No voice assigned yet \u2014 see Agent Voices"),
      fmtWhen(p.last_seen_at),
      fmtWhen(p.last_spoken_at),
      p.last_spoken_text || "\u2014",
      badge(p.status || "\u2014", meetStatusBadgeKind(p.status)),
    ];
  });
}

/* Persisted "show this section type" preference, keyed by section label —
 * a SINGLE global checkbox per type (not one per meeting) controls that
 * section across every driver meeting at once: unchecking "Captions" hides
 * every meeting's captions block simultaneously, no per-meeting repeats. */
function getMeetSectionOpen(key) {
  const raw = localStorage.getItem(`ws_collab_meet_section_open_${key}`);
  return raw === null ? null : raw === "1";
}
function setMeetSectionOpen(key, isOpen) {
  localStorage.setItem(`ws_collab_meet_section_open_${key}`, isOpen ? "1" : "0");
}
function getMeetKindFilter(kind) {
  const raw = localStorage.getItem(`ws_collab_meet_kind_filter_${kind}`);
  return raw === null ? true : raw === "1";
}
function setMeetKindFilter(kind, isOpen) {
  localStorage.setItem(`ws_collab_meet_kind_filter_${kind}`, isOpen ? "1" : "0");
}

/* One checkbox+label in the global toggle row, driving every content node
 * in `contentNodes` (one per meeting, same section type) at once via the
 * `hidden` attribute — unchecked means zero space for ALL of them, no
 * separate heading anywhere either, since this checkbox's own label is the
 * only heading the section type gets. Persisted so the choice survives the
 * next Refresh's full re-render. */
function meetSectionToggle(label, count, defaultOn, contentNodes) {
  const saved = getMeetSectionOpen(label);
  const on = saved === null ? defaultOn : saved;
  contentNodes.forEach((node) => { node.hidden = !on; });
  const wrap = el("label", "meet-section-toggle");
  const box = el("input");
  box.type = "checkbox";
  box.checked = on;
  box.onchange = () => {
    setMeetSectionOpen(label, box.checked);
    contentNodes.forEach((node) => { node.hidden = !box.checked; });
  };
  wrap.appendChild(box);
  wrap.appendChild(document.createTextNode(" " + (count == null ? label : `${label} (${count})`)));
  return wrap;
}

function meetKindToggle(kind, label) {
  const wrap = el("label", "meet-section-toggle");
  const box = el("input");
  box.type = "checkbox";
  box.checked = getMeetKindFilter(kind);
  box.onchange = () => {
    setMeetKindFilter(kind, box.checked);
    loadMeet();
  };
  wrap.appendChild(box);
  wrap.appendChild(document.createTextNode(` ${label}`));
  return wrap;
}


/* Driver naming convention: "google-meet-stt-<room-id>" — the room id is
 * the 3-4-3 code Meet assigns (e.g. "vfi-zywr-ezz"). This is the name the
 * team uses for a HOST+COMPANION pair bound to one meeting, so it doubles
 * as a stable identity for a driver even while the URL itself is opaque.
 * meetRoomId() is the same extraction on its own — used to key/look up
 * meetingState (the bridge's live per-room snapshot dict) by room id. */
function meetRoomId(url) {
  const match = /meet\.google\.com\/([a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4})/i.exec(url || "");
  return match ? match[1].toLowerCase() : null;
}
function driverName(url) {
  const room = meetRoomId(url);
  return room ? `google-meet-stt-${room}` : "google-meet-stt-(unknown)";
}

/* Badge color for the debug table's per-line Source column -- purely
 * cosmetic grouping (host/companion/bridge), no behavior depends on it. */
function meetRoleBadgeKind(role) {
  if (role === "host") return "ok";
  if (role === "companion") return "";
  return "warn";
}

/* What real-world audio source a DRIVER meeting probes — "Physical
 * Computer" (this machine's real mic/speakers, what HOST always is today)
 * or a future "Discord Voice Channel — <server/channel name>" driver
 * connected the same way, not yet built — vs. "(not connected)" for a
 * configured-but-inactive placeholder. This is the driver/client split:
 * a DRIVER relays audio IN from elsewhere, with the Meet room just the
 * venue ws_collab reads captions from — the probe is that elsewhere. A
 * CLIENT (not yet built) has no relay: the joined meeting itself IS the
 * probe location, since the client is simply present in it directly.
 * Only reported for the CURRENT live driver — a placeholder entry has no
 * real probe until something actually connects it. */
function probeLocation(isCurrent, bridgeOnline) {
  if (isCurrent && bridgeOnline) return "Physical Computer";
  return "(not connected)";
}

/* Persisted per-section-type autoscroll preference (localStorage, default
 * ON) -- mirrors getMeetSectionOpen/setMeetSectionOpen's convention, but for
 * "follow new content" rather than "show this section at all". */
function getMeetAutoscroll(label) {
  const raw = localStorage.getItem(`ws_collab_meet_autoscroll_${label}`);
  return raw === null ? true : raw === "1";
}
function setMeetAutoscroll(label, on) {
  localStorage.setItem(`ws_collab_meet_autoscroll_${label}`, on ? "1" : "0");
}

/* Persisted per-section-type visible-row count (localStorage, default 10).
 * Drives the box's height directly (rows * row-height); the operator can
 * still drag it taller/shorter afterward (native CSS resize: vertical) --
 * that manual override is intentionally overwritten the next time this
 * number is changed, since "set it to N rows" is an explicit request. */
const MEET_ROW_H = 24; // matches the app-wide dense-table row height elsewhere
function getMeetRowCount(label) {
  const stored = localStorage.getItem(`ws_collab_meet_rows_${label}`);
  if (stored === "all") return "all";
  const raw = parseInt(stored, 10);
  return Number.isFinite(raw) && raw >= 2 ? raw : 10;
}
function setMeetRowCount(label, rows) {
  localStorage.setItem(`ws_collab_meet_rows_${label}`, rows === "all" ? "all" : String(Math.max(2, parseInt(rows, 10) || 10)));
}
function meetBoxHeightPx(rows) { return MEET_ROW_H + rows * MEET_ROW_H; }
/* Applies the persisted row count to EVERY currently-rendered box of this
 * section type at once (there is one box per meeting group, all sharing
 * one preference) -- a live DOM query rather than a captured array, since
 * a fresh array only exists per render and any toolbar (any meeting) may
 * be the one the operator adjusts. Growing a box just makes the page
 * taller/scroll further, exactly like any other panel content -- nothing
 * special is needed for that beyond the height change itself. */
function applyMeetRowCount(label) {
  const rows = getMeetRowCount(label);
  document.querySelectorAll(`.meet-scroll-box[data-section-label="${label}"]`).forEach((box) => {
    if (rows === "all") box.style.removeProperty("height");
    else box.style.height = `${meetBoxHeightPx(rows)}px`;
    if (getMeetAutoscroll(label)) box.scrollTop = box.scrollHeight;
  });
}

/* In-memory "cleared at" cutoffs -- Clear blanks ONE meeting's ONE section
 * until genuinely new data arrives after the click, by filtering out
 * anything at or before that moment; keyed by `${meetingUrl}::${label}` so
 * clearing "Emit" doesn't also clear "Phrases"/"Transcribe", and clearing
 * one meeting's box doesn't affect another's. Deliberately NOT persisted
 * (a page reload un-clears) -- this is a transient view action, not a
 * durable preference the way the autoscroll toggle is. */
const meetClearedAt = new Map();
function meetClearCutoff(key) { return meetClearedAt.get(key) || 0; }
const MEET_SCROLL_LABELS = ["Emit", "Phrases", "Transcribe"];

function clearAllMeetSections() {
  const now = Date.now() / 1000;
  document.querySelectorAll(".meet-tree-meeting[data-url]").forEach((meeting) => {
    const url = meeting.dataset.url;
    if (!url) return;
    MEET_SCROLL_LABELS.forEach((label) => meetClearedAt.set(`${url}::${label}`, now));
  });
  loadMeet();
}

function setAllMeetRowCounts(value) {
  const rows = Math.max(2, parseInt(value, 10) || 10);
  MEET_SCROLL_LABELS.forEach((label) => {
    setMeetRowCount(label, rows);
    applyMeetRowCount(label);
  });
}

function setAllMeetAutoscroll(on) {
  MEET_SCROLL_LABELS.forEach((label) => {
    setMeetAutoscroll(label, on);
    if (on) applyMeetRowCount(label);
  });
}

function meetCopyLink(url) {
  const room = meetRoomId(url) || url;
  const link = el("a", "mono", room);
  link.href = "#";
  link.title = "Click to copy the full meeting URL to your clipboard.";
  link.onclick = (e) => {
    e.preventDefault();
    navigator.clipboard.writeText(url).then(() => {
      const original = link.textContent;
      link.textContent = "copied!";
      setTimeout(() => { link.textContent = original; }, 1000);
    }).catch(() => {});
  };
  return link;
}

function copyTextLink(label, text, title) {
  const link = el("a", "mono", label);
  link.href = "#";
  if (title) link.title = title;
  link.onclick = (e) => {
    e.preventDefault();
    navigator.clipboard.writeText(text).then(() => {
      const original = link.textContent;
      link.textContent = "copied!";
      setTimeout(() => { link.textContent = original; }, 1000);
    }).catch(() => {});
  };
  return link;
}

function meetDevicesLink() {
  const link = el("a", null, "COMPUTER");
  link.href = "#devices";
  return link;
}

async function toggleMeetCapture(listening) {
  try {
    await api(`${V1}/audio/capture/${listening ? "stop" : "start"}`, { method: "POST", body: {} });
  } catch (error) {
    pushError(error.message);
  }
  loadMeet();
}

/* One streaming section: a small toolbar (Clear + an Autoscroll on/off
 * toggle, default ON per the operator's request) above a fixed-height
 * (~10 rows) scrollable box the operator can still drag taller (native CSS
 * `resize: vertical`). Returns {wrap, box}: `wrap` (toolbar + box together)
 * is what the global show/hide checkbox (meetSectionToggle) should track;
 * `box` is where rows/text actually go and whose scrollTop autoscroll
 * manages -- kept separate so hiding a section doesn't orphan its toolbar. */
function meetScrollSection(clearKey, label) {
  const wrap = el("div", "meet-scroll-wrap");
  const toolbar = el("div", "meet-scroll-toolbar");
  const clearBtn = actionButton("Clear", "mini", () => {
    meetClearedAt.set(clearKey, Date.now() / 1000);
    loadMeet();
  });
  const autoBtn = actionButton("", "mini toggle");
  const paintAuto = (on) => {
    autoBtn.textContent = on ? "Autoscroll: ON" : "Autoscroll: OFF";
    autoBtn.classList.toggle("on", on);
  };
  paintAuto(getMeetAutoscroll(label));
  autoBtn.onclick = () => {
    const next = !getMeetAutoscroll(label);
    setMeetAutoscroll(label, next);
    paintAuto(next);
    if (next) box.scrollTop = box.scrollHeight;
  };
  // Visible-row-count input -- same global-per-type convention as
  // Autoscroll (one shared preference, a toolbar instance per meeting).
  // Applied to every currently-rendered box of this type at once via
  // applyMeetRowCount(), not just this one, so changing it in any one
  // meeting's toolbar keeps every meeting's same-type box in sync.
  const presetValues = [3, 10, 20, "all"];
  const showLabel = el("span", "mini-label", "Show");
  const exactLabel = el("span", "mini-label", "Exact");
  const exactInput = el("input", "mini-input");
  exactInput.type = "number";
  exactInput.min = "2";
  exactInput.step = "1";
  exactInput.title = "Custom visible-row count before the box starts scrolling internally; leave blank while using presets.";
  const presetButtons = presetValues.map((value) => actionButton(value === "all" ? "ALL" : String(value), "mini toggle", () => {
    setMeetRowCount(label, value);
    applyMeetRowCount(label);
    paintRows();
  }));
  const paintRows = () => {
    const current = getMeetRowCount(label);
    presetButtons.forEach((btn, idx) => btn.classList.toggle("on", presetValues[idx] === current));
    exactInput.value = typeof current === "number" && !presetValues.includes(current) ? String(current) : "";
  };
  exactInput.onchange = () => {
    const parsed = Math.max(2, parseInt(exactInput.value, 10) || 10);
    setMeetRowCount(label, parsed);
    applyMeetRowCount(label);
    paintRows();
  };
  paintRows();
  toolbar.append(clearBtn, autoBtn, showLabel, ...presetButtons, exactLabel, exactInput);
  const box = el("div", "meet-scroll-box");
  box.dataset.sectionLabel = label;
  const initialRows = getMeetRowCount(label);
  if (initialRows !== "all") box.style.height = `${meetBoxHeightPx(initialRows)}px`;
  wrap.append(toolbar, box);
  return { wrap, box };
}

function meetCaptionRole(c) {
  if (c && c.role) return String(c.role);
  const key = c && c.key ? String(c.key) : "";
  const idx = key.indexOf(":");
  return idx > 0 ? key.slice(0, idx) : "";
}

function meetCaptionKeyLabel(value) {
  const text = esc(value);
  if (!text) return "\u2014";
  const idx = text.indexOf(":");
  if (idx > 0) {
    const role = text.slice(0, idx);
    const rest = text.slice(idx + 1);
    return `${role}:${rest.length > 12 ? `\u2026${rest.slice(-12)}` : rest}`;
  }
  return text.length > 14 ? `\u2026${text.slice(-14)}` : text;
}

function meetCaptionKeyNode(value) {
  if (!value) return "\u2014";
  const node = mono(meetCaptionKeyLabel(value));
  node.title = esc(value);
  return node;
}

function meetCaptionTextCell(c) {
  const wrap = el("span", "meet-caption-text", c && c.text ? c.text : "");
  const raw = c && c.rawText ? String(c.rawText) : "";
  if (raw) {
    const title = `Raw DOM text: ${raw}`;
    wrap.title = title;
    const marker = el("span", "badge", "raw");
    marker.title = title;
    marker.style.marginLeft = "6px";
    wrap.appendChild(marker);
  }
  return wrap;
}

function meetTransportLine(source) {
  const transport = source && source.captionTransport ? source.captionTransport : "\u2014";
  const frames = source && source.pushFrameCount != null ? source.pushFrameCount : 0;
  return `${transport} (push frames ${frames})`;
}

function meetTransportByRoleLine(source) {
  const byRole = (source && source.captionTransportByRole) || {};
  const roles = Object.keys(byRole).sort();
  if (!roles.length) return "\u2014";
  return roles.map((role) => {
    const info = byRole[role] || {};
    const transport = info.captionTransport || "\u2014";
    const frames = info.pushFrameCount != null ? info.pushFrameCount : 0;
    const last = shortTs(info.lastPushIso) || info.lastPushIso || "\u2014";
    return `${role}: ${transport}, push frames ${frames}, last ${last}`;
  }).join("; ");
}

function meetTranscriptSpeaker(c, rows) {
  const speaker = (c && c.speaker ? String(c.speaker) : "").trim();
  const role = meetCaptionRole(c);
  const bySpeaker = new Map();
  (rows || []).forEach((row) => {
    const name = (row && row.speaker ? String(row.speaker) : "").trim().toLowerCase();
    if (!name) return;
    if (!bySpeaker.has(name)) bySpeaker.set(name, new Set());
    bySpeaker.get(name).add(meetCaptionRole(row) || "");
  });
  const rolesForSpeaker = speaker ? bySpeaker.get(speaker.toLowerCase()) : null;
  const ambiguous = !speaker || /^you$/i.test(speaker) || /^speaker$/i.test(speaker) || (rolesForSpeaker && rolesForSpeaker.size > 1);
  if (ambiguous && role) return role;
  return speaker || role || "Speaker";
}

function renderMeetTree(container, groups, currentUrl, clients, agentProfiles, debugRows, bridgeOnline, emitCount, hostProfile, meetingState, recipients) {
  rememberMeetKnownUrls(groups.map(({ url }) => (url && url !== "(unknown meeting)" ? url : "")));
  groups = groups.filter(({ kind }) => getMeetKindFilter(kind || "driver"));
  const priorOpen = new Map();
  container.querySelectorAll(".meet-tree-meeting").forEach((d) => {
    if (d.dataset.url) priorOpen.set(d.dataset.url, d.open);
  });
  container.replaceChildren();
  if (!groups.length) {
    container.appendChild(el("div", "hint", "No captions seen yet — captions appear here once the bridge is in a meeting and someone speaks."));
    return;
  }
  // Collected across every meeting, one array per section type, so a single
  // global checkbox (built after this loop) can show/hide that type
  // everywhere at once instead of repeating 7 checkboxes per meeting.
  const usBodies = [], agentsBodies = [], presenceBodies = [], debugBodies = [], emitBodies = [], phrasesBodies = [], transcribeBodies = [];
  const emitBoxes = [], phrasesBoxes = [], transcribeBoxes = [];
  let totalAgentRows = 0, totalDebugRows = 0, totalEmitRows = 0, totalTranscriptLines = 0, totalConnectorRows = 0, totalPhraseRows = 0;
  const meetingEls = [];

  groups.forEach(({ url, captions, kind }) => {
    const isCurrent = url === currentUrl;
    const isClient = kind === "client";
    const meeting = el("details");
    meeting.dataset.url = url;
    meeting.open = priorOpen.has(url) ? priorOpen.get(url) : isCurrent;
    meeting.className = "meet-tree-meeting";
    const summary = el("summary");
    summary.appendChild(mono(`${driverName(url)}  `));
    const link = el("a", null, url);
    link.href = url; link.target = "_blank"; link.rel = "noopener";
    link.onclick = (e) => e.stopPropagation();
    summary.appendChild(link);
    summary.appendChild(mono(isCurrent ? (isClient ? "  · current" : "  · (COMPUTER)") : "  · (AVAILABLE)"));
    summary.appendChild(badge(isClient ? "CLIENT" : "DRIVER", isClient ? "warn" : ""));
    summary.appendChild(badge(probeLocation(isCurrent, bridgeOnline), isCurrent && bridgeOnline ? "ok" : ""));
    summary.appendChild(document.createTextNode(" "));
    const connectBtn = actionButton(isCurrent && bridgeOnline ? "Rejoin" : "Connect", "primary", (e) => {
      e.stopPropagation();
      postMeetCommand(`/join ${url}`);
    });
    connectBtn.title = "Connect the live driver to this meeting (same action as the Connector agents row buttons).";
    summary.appendChild(connectBtn);
    const accountsBtn = actionButton("Accounts", "mini", (e) => {
      e.stopPropagation();
      state.meetAssignmentScope = url;
      loadMeetRoleAssignments();
      $("meet-role-settings").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    accountsBtn.title = "Set HOST and COMPANION account overrides for this meeting.";
    summary.appendChild(accountsBtn);
    meeting.appendChild(summary);

    const us = meetUsRows(isCurrent, clients, url, hostProfile, (meetingState || {})[meetRoomId(url)], kind);
    const usBody = el("div");
    usBody.appendChild(table(["Who", "SSO", "State", "Meeting", "Mic", "Speak", "Actions"], us.rows));
    if (us.note) usBody.appendChild(el("div", "hint", us.note));
    totalConnectorRows += us.rows.length;

    const agentsBody = el("div");
    const liveCompanion = isCurrent ? (clients || []).find((c) => c.role === "companion") : null;
    const agentRows = meetVirtualAgentRows(agentProfiles, liveCompanion && liveCompanion.speak, isCurrent ? recipients : null);
    agentsBody.appendChild(agentRows.length
      ? table(["Agent", "Voice", "Listens", "Speaks", "Enabled", "Last Seen", "Last Spoke", "Text", "Status"], agentRows)
      : el("div", "hint", "No ws_collab agent voice profiles configured yet (see Agent Voices)."));

    const presenceBody = el("div", "hint",
      "Not yet implemented — the bridge doesn't scrape Google Meet's own People panel, so it " +
      "can't list human or other non-controlled presences beyond the identities it controls " +
      "(see \u201cConnectors\u201d above).");

    const debugBody = el("div");
    const rows = isCurrent ? (debugRows || []) : [];
    debugBody.appendChild(rows.length
      ? table(["Time", "Source", "Message"], rows.map((d) => [shortTs(d.iso) || d.iso || "", badge((d.role || "bridge").toUpperCase(), meetRoleBadgeKind(d.role)), d.text || ""]))
      : el("div", "hint", isCurrent
        ? "No debug messages yet — autojoin verdicts, mic-select attempts, and /say results appear here."
        : "Debug messages are only kept for the current meeting."));

    // Raw emits: exactly what the bridge's emit() sent for this meeting —
    // one row per `key`, showing its CURRENT text (since a key can be
    // EDITED in place, not just added), plus the full info every emit
    // carries: whether it's a settled phrase or still growing, and what
    // key (if any) it continues from. This is the direct, honest view of
    // the raw stream — the best way to confirm an emit actually went out
    // and what it currently says, with nothing reassembled or hidden.
    const sortedByKey = captions.slice().sort((a, b) => (a.at || 0) - (b.at || 0));
    const emitCutoff = meetClearCutoff(`${url}::Emit`);
    const emitRowsShown = sortedByKey.filter((c) => (c.at || 0) > emitCutoff);
    const { wrap: emitBody, box: emitBox } = meetScrollSection(`${url}::Emit`, "Emit");
    emitBox.appendChild(emitRowsShown.length ? table(["Key", "Time", "Role", "Speaker", "Text", "Final", "Replaces", "Dup"], emitRowsShown.map((c) => [
      meetCaptionKeyNode(c.key), shortTs(c.iso) || c.iso || "", meetCaptionRole(c) || "—", c.speaker || "", meetCaptionTextCell(c),
      c.final ? "yes" : "growing…", meetCaptionKeyNode(c.replaces), meetCaptionKeyNode(c.duplicateOf),
    ])) : el("div", "hint", "Cleared — new emits appear here as they happen."));

    // Phrases: JUST the settled sentences (final=true) out of the same raw
    // stream — every "Hello there." that will never be edited again, none
    // of the still-growing in-progress fragments Emit also shows. This is
    // what "phrases" means for this bridge: Meet gives one continuously
    // growing row per monologue, and every completed sentence peeled off
    // of it becomes one phrase here, in order.
    const phraseRows = sortedByKey.filter((c) => c.final && !c.duplicateOf);
    const phrasesCutoff = meetClearCutoff(`${url}::Phrases`);
    const phraseRowsShown = phraseRows.filter((c) => (c.at || 0) > phrasesCutoff);
    const { wrap: phrasesBody, box: phrasesBox } = meetScrollSection(`${url}::Phrases`, "Phrases");
    phrasesBox.appendChild(phraseRowsShown.length
      ? table(["Key", "Time", "Role", "Speaker", "Text"], phraseRowsShown.map((c) => [
        meetCaptionKeyNode(c.key), shortTs(c.iso) || c.iso || "", meetCaptionRole(c) || "—", c.speaker || "", meetCaptionTextCell(c),
      ]))
      : el("div", "hint", phraseRows.length ? "Cleared — new phrases appear here as sentences finish." : "No completed phrases yet — a phrase appears here the instant a sentence finishes."));

    // Transcribe: the SAME raw data, reassembled into one readable
    // transcript (creation order, each key's current text) — a concrete
    // example of what a consumer (an LLM agent, or ws_collab's own
    // disambiguator) does with the raw stream: turn "row X now says Y"
    // updates into a coherent read, without the bridge itself deciding
    // anything about finality. Counted in SENTENCES (not raw rows) since
    // that's what "reassembled" means — one still-growing row can already
    // contain many settled sentences, a different number than the raw
    // emit-event count shown by "Emit" above.
    const transcribeCutoff = meetClearCutoff(`${url}::Transcribe`);
    const transcriptRows = sortedByKey.filter((c) => c.final && !c.duplicateOf);
    const transcribeRowsShown = transcriptRows.filter((c) => (c.at || 0) > transcribeCutoff);
    const { wrap: transcribeBody, box: transcribeBox } = meetScrollSection(`${url}::Transcribe`, "Transcribe");
    const transcriptLines = transcribeRowsShown.map((c) => `${meetTranscriptSpeaker(c, transcriptRows)}: ${c.text || ""}`);
    transcribeBox.appendChild(el("pre", "mono", transcriptLines.join("\n") || (transcriptRows.length ? "(cleared — new finalized lines appear here as they're said)" : "(no finalized transcript yet — growing emits appear in Emit)")));

    meeting.appendChild(usBody);
    meeting.appendChild(agentsBody);
    meeting.appendChild(presenceBody);
    meeting.appendChild(debugBody);
    meeting.appendChild(emitBody);
    meeting.appendChild(phrasesBody);
    meeting.appendChild(transcribeBody);
    meetingEls.push(meeting);

    usBodies.push(usBody);
    agentsBodies.push(agentsBody);
    presenceBodies.push(presenceBody);
    debugBodies.push(debugBody);
    emitBodies.push(emitBody);
    phrasesBodies.push(phrasesBody);
    transcribeBodies.push(transcribeBody);
    emitBoxes.push(emitBox);
    phrasesBoxes.push(phrasesBox);
    transcribeBoxes.push(transcribeBox);
    totalEmitRows += sortedByKey.length;
    totalAgentRows += agentRows.length;
    totalDebugRows += isCurrent ? rows.length : 0;
    totalTranscriptLines += transcriptRows.length;
    totalPhraseRows += phraseRows.length;
  });

  // ONE global toggle row, above every meeting — checking/unchecking a
  // section type shows/hides it across ALL meetings at once, rather than
  // repeating the same 7 checkboxes once per meeting.
  const toggleRow = el("div", "meet-section-toggles");
  toggleRow.appendChild(meetSectionToggle("Connectors", totalConnectorRows, true, usBodies));
  toggleRow.appendChild(meetSectionToggle("Virtual agents", totalAgentRows, false, agentsBodies));
  toggleRow.appendChild(meetSectionToggle("Presences", "?", false, presenceBodies));
  toggleRow.appendChild(meetSectionToggle("Other things", totalDebugRows, false, debugBodies));
  toggleRow.appendChild(meetSectionToggle("Emit", totalEmitRows || emitCount || 0, true, emitBodies));
  toggleRow.appendChild(meetSectionToggle("Phrases", totalPhraseRows, true, phrasesBodies));
  toggleRow.appendChild(meetSectionToggle("Transcribe", totalTranscriptLines, true, transcribeBodies));
  container.appendChild(toggleRow);
  meetingEls.forEach((meeting) => container.appendChild(meeting));
  // Now that everything is actually in the document (scrollHeight needs
  // layout), default each of these three streaming sections to showing its
  // latest content -- gated per section-TYPE by the Autoscroll toggle
  // (default ON); the operator can still scroll up or drag-resize taller
  // regardless. A refresh (this function rebuilds the DOM from scratch
  // each time) snaps back to the bottom again when autoscroll is on,
  // matching "auto-scroll" intent.
  if (getMeetAutoscroll("Emit")) emitBoxes.forEach((box) => { box.scrollTop = box.scrollHeight; });
  if (getMeetAutoscroll("Phrases")) phrasesBoxes.forEach((box) => { box.scrollTop = box.scrollHeight; });
  if (getMeetAutoscroll("Transcribe")) transcribeBoxes.forEach((box) => { box.scrollTop = box.scrollHeight; });
}

async function loadMeet() {
  const statusLine = $("meet-status-line");
  const dot = $("meet-nav-dot");
  const autoBtn = actionButton("", "mini toggle");
  const paintAutoAll = (on) => {
    autoBtn.textContent = on ? "Autoscroll: ON" : "Autoscroll: OFF";
    autoBtn.classList.toggle("on", on);
  };
  const allAutoOn = MEET_SCROLL_LABELS.every((label) => getMeetAutoscroll(label));
  paintAutoAll(allAutoOn);
  autoBtn.onclick = () => {
    const next = !MEET_SCROLL_LABELS.every((label) => getMeetAutoscroll(label));
    setAllMeetAutoscroll(next);
    paintAutoAll(next);
  };
  const exactAll = el("input", "mini-input");
  exactAll.type = "number";
  exactAll.min = "2";
  exactAll.step = "1";
  exactAll.id = "meet-exact-all";
  exactAll.style.width = "72px";
  exactAll.onchange = () => {
    const rows = Math.max(2, parseInt(exactAll.value, 10) || 10);
    exactAll.value = String(rows);
    setAllMeetRowCounts(rows);
  };
  $("meet-kind-toolbar").replaceChildren(
    el("span", "mini-label", "Show:"),
    meetKindToggle("driver", "Driver"),
    meetKindToggle("client", "Client"),
    actionButton("Clear all", "mini", clearAllMeetSections),
    autoBtn,
    el("span", "mini-label", "Exact"),
    exactAll,
  );
  let health;
  try {
    const capture = await api(`${V1}/audio/capture`);
    state.meetCaptureListening = !!capture.listening;
  } catch (_error) {
    state.meetCaptureListening = false;
  }
  try {
    health = await api(`${MEET_BRIDGE_BASE}/status`);
  } catch (error) {
    statusLine.textContent = `bridge offline (${error.message}) — start it from the Processes page or ` +
      `run "ws-collab-meet-bridge" (or "python -m ws_collab.meet_bridge")`;
    if (dot) { dot.classList.remove("ok"); dot.classList.add("danger"); }
    $("meet-bridge-card").textContent = "Bridge unreachable.";
    syncCompanionMeetingOptions("");
    renderCompanionMetrics(null);
    // Still show the known default driver meetings as placeholders (all
    // correctly "not current" while the bridge itself is down) so the
    // intended meetings stay visible instead of the section going blank.
    renderMeetTree($("meet-driver-tree"), [
      ...state.meetKnownUrls
        .filter((url) => !DEFAULT_CLIENT_MEETING_URLS.map(meetAssignmentKey).includes(meetAssignmentKey(url)))
        .map((url) => ({ url, captions: [], kind: "driver" })),
      ...DEFAULT_CLIENT_MEETING_URLS.map((url) => ({ url, captions: [], kind: "client" })),
    ], null, [], [], [], false, 0, null, {}, []);
    return;
  }
  if (dot) { dot.classList.toggle("ok", !!health.ok); dot.classList.toggle("danger", !health.ok); }
  statusLine.textContent = health.meetingUrl ? `bridge online — ${health.meetingUrl}` : "bridge online — no meeting";
  syncCompanionMeetingOptions(health.meetingUrl);
  renderCompanionMetrics(health);
  const hostProfileLabel = ssoLabel(health.hostProfile);
  const paintBridgeCard = (captionData) => {
    const transportSource = captionData || health;
    const cardLines = [
      `service       ${health.service || "ws_collab_meet_bridge"}`,
      `meeting       ${health.meetingUrl || "\u2014"}`,
      `host account  ${hostProfileLabel}`,
      `browser backend  ${health.browserBackend || "windows"}`,
      `captions      ${captionData ? (captionData.captions || []).length : (health.captionCount ?? 0)} loaded, last at ${health.lastCaptionAt || (captionData && captionData.rawIso) || "\u2014"}`,
      `transport     ${meetTransportLine(transportSource)}`,
      `by role       ${meetTransportByRoleLine(transportSource)}`,
      `outbox        ${health.outbox || "\u2014"}`,
      `transcripts   \u2192 ${(health.recipients || []).join(", ") || "\u2014"}`,
    ];
    $("meet-bridge-card").textContent = cardLines.join("\n");
  };
  paintBridgeCard(null);

  let capData;
  try {
    capData = await api(`${MEET_BRIDGE_BASE}/captions?since=0`);
  } catch (error) {
    $("meet-driver-tree").textContent = `error loading captions: ${error.message}`;
    return;
  }
  paintBridgeCard(capData);
  let agentProfiles = [];
  try {
    agentProfiles = ((await api(`${V1}/voices`)).profiles || []);
  } catch (error) {
    // Non-fatal — the meeting tree still renders without the Virtual agents list.
  }
  const byMeeting = new Map();
  (capData.captions || []).forEach((c) => {
    const key = c.meetingUrl ? meetAssignmentKey(c.meetingUrl) : "(unknown meeting)";
    if (!byMeeting.has(key)) byMeeting.set(key, []);
    byMeeting.get(key).push(c);
  });
  // The CURRENT bridge meeting is always a driver meeting by definition —
  // show it even with zero captions so far (e.g. right after joining,
  // before anyone has spoken), not only once something has been said.
  const currentMeetingKey = health.meetingUrl ? meetAssignmentKey(health.meetingUrl) : "";
  if (currentMeetingKey && !byMeeting.has(currentMeetingKey)) byMeeting.set(currentMeetingKey, []);
  // Every known default driver meeting is shown too, even ones this bridge
  // isn't currently attached to (e.g. it moved to a different one, or
  // hasn't switched there yet) — Join re-attaches the live driver there.
  state.meetKnownUrls.forEach((url) => {
    const key = meetAssignmentKey(url);
    if (key && !byMeeting.has(key)) byMeeting.set(key, []);
  });
  const clientUrlSet = new Set(DEFAULT_CLIENT_MEETING_URLS.map(meetAssignmentKey));
  // Current meeting first, then whatever else the ring buffer still
  // remembers (most-recently-active-in-buffer order).
  const order = [currentMeetingKey, ...[...byMeeting.keys()].filter((u) => u !== currentMeetingKey)].filter(Boolean);
  const groups = order.filter((u) => byMeeting.has(u)).map((u) => ({ url: u, captions: byMeeting.get(u), kind: clientUrlSet.has(meetAssignmentKey(u)) ? "client" : "driver" }));

  const isMeetHovered = () => !!document.querySelector('.meet-scroll-box:hover');
  const hasSelectionInMeet = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return false;
    let node = sel.anchorNode;
    while (node) {
      if (node.classList && node.classList.contains("meet-scroll-box")) return true;
      node = node.parentNode;
    }
    return false;
  };
  const isMeetScrolledUp = () => {
    let scrolledUp = false;
    document.querySelectorAll('.meet-scroll-box').forEach(box => {
      if (box.scrollHeight - box.scrollTop - box.clientHeight >= 40) {
        scrolledUp = true;
      }
    });
    return scrolledUp;
  };

  if (!isMeetHovered() && !hasSelectionInMeet() && !isMeetScrolledUp()) {
    renderMeetTree($("meet-driver-tree"), groups, currentMeetingKey, health.clients, agentProfiles, health.debug, !!health.ok, health.emitCount, health.hostProfile, health.meetingState, health.recipients);
  }
}

let meetPolling = false;

function meetPollOnce() {
  const active = document.activeElement;
  const meetPage = document.querySelector('.page[data-page="meet"]');
  const typingInMeet = active
    && /^(INPUT|TEXTAREA)$/.test(active.tagName)
    && meetPage
    && meetPage.contains(active);
  const run = typingInMeet ? Promise.resolve() : loadMeet();
  run.finally(() => {
    // This does a heavier full tree rebuild, and intentionally skips ticks
    // while the operator is typing.
    if (state.page === "meet") setTimeout(meetPollOnce, 3000);
    else meetPolling = false;
  });
}

async function loadMeetWithPolling() {
  if (meetPolling) return;
  await loadMeetRoleAssignments();
  meetPolling = true;
  meetPollOnce();
}

function hasSelectionIn(containerId) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return false;
  let node = sel.anchorNode;
  while (node) {
    if (node.id === containerId) return true;
    node = node.parentNode;
  }
  return false;
}

/* ---- silences turn-taking test harness */
function silencesLogic() {
  return window.WsCollabSilencesLogic;
}

function silencesRun() {
  const logic = silencesLogic();
  if (!logic) return null;
  if (!state.silences.run) {
    state.silences.run = logic.createRun({
      testId: $("sil-test") ? $("sil-test").value : "count20",
      firstRole: $("sil-first-role") ? $("sil-first-role").value : "host",
      startedAtSec: Date.now() / 1000,
    });
  }
  return state.silences.run;
}

function silencesLatency(value) {
  return Number.isFinite(value) ? `${Math.round(value)} ms` : "\u2014";
}

function silencesMetric(label, value) {
  const node = el("div", "silence-metric");
  node.append(el("div", "silence-metric-label", label), el("div", "silence-metric-value", value));
  return node;
}

function silencesStatusBadge(status) {
  if (status === "correct") return badge("\u2713 correct", "ok");
  if (status === "mis-attributed") return badge("\u26a0 mis-attributed", "warn");
  return badge("\u2717 sequence", "danger");
}

function silencesAutoscrollOn() {
  return localStorage.getItem("ws_collab_silences_autoscroll") !== "0";
}

function setSilencesAutoscroll(on) {
  localStorage.setItem("ws_collab_silences_autoscroll", on ? "1" : "0");
  const button = $("sil-autoscroll");
  if (button) {
    button.textContent = on ? "Autoscroll: ON" : "Autoscroll: OFF";
    button.classList.toggle("on", on);
  }
}

function silencesScrolledUp() {
  const box = $("sil-turns");
  return !!box && box.scrollHeight - box.scrollTop - box.clientHeight >= 40;
}

function silencesShouldDeferRender() {
  const box = $("sil-turns");
  return !!box && (box.matches(":hover") || hasSelectionIn("sil-turns") || silencesScrolledUp());
}

function renderSilencesSummary(run) {
  const logic = silencesLogic();
  const summary = $("sil-summary");
  if (!logic || !summary || !run) return;
  const stats = logic.summarizeRun(run);
  summary.replaceChildren(
    silencesMetric("Turns completed", `${stats.completed}/${stats.total}`),
    silencesMetric("Scored turns", String(stats.scored)),
    silencesMetric("Correct", String(stats.correct)),
    silencesMetric("Errors", String(stats.errors)),
    silencesMetric("Mean latency", silencesLatency(stats.meanLatencyMs)),
    silencesMetric("Median latency", silencesLatency(stats.medianLatencyMs)),
    silencesMetric("Max latency", silencesLatency(stats.maxLatencyMs)),
  );
}

function renderSilencesExpected(run) {
  const logic = silencesLogic();
  const target = $("sil-expected-seq");
  if (!logic || !target || !run) return;
  const test = logic.TESTS[run.testId] || logic.TESTS.count20;
  const strip = el("div", "silence-token-strip");
  test.tokens.forEach((token, index) => {
    const cls = "silence-token" + (index < run.nextIndex ? " done" : (index === run.nextIndex ? " current" : ""));
    const node = el("span", cls);
    node.append(mono(token), el("span", "silence-token-role", logic.expectedRoleFor(index, run.firstRole)));
    strip.appendChild(node);
  });
  target.replaceChildren(strip);
}

function silencesTurnsTable(turns) {
  const t = table(["#", "Status", "Expected", "Observed", "Role", "Speaker", "Caption", "Latency", "Finding"], turns.map((turn) => [
    String(turn.turnNumber),
    silencesStatusBadge(turn.status),
    `${turn.expectedToken || "\u2014"} / ${turn.expectedRole || "\u2014"}`,
    turn.observedToken || "\u2014",
    turn.role || "\u2014",
    turn.speaker || "\u2014",
    turn.text || "",
    silencesLatency(turn.latencyMs),
    turn.detail,
  ]));
  [...t.querySelectorAll("tbody tr")].forEach((tr, index) => {
    tr.classList.add(`silence-turn-${turns[index].status}`);
    tr.title = turns[index].key || "";
  });
  return t;
}

function renderSilencesTurns(run, options = {}) {
  const box = $("sil-turns");
  const jump = $("sil-jump");
  const note = $("sil-paused-note");
  if (!box || !run) return;
  if (!options.force && run.turns.length && silencesShouldDeferRender()) {
    state.silences.pendingRender = true;
    state.silences.pendingRows = run.turns.length;
    if (jump) jump.hidden = false;
    if (note) note.textContent = "Turn list paused while hovered, selected, or scrolled up.";
    return;
  }
  state.silences.pendingRender = false;
  state.silences.pendingRows = 0;
  if (jump) jump.hidden = true;
  if (note) note.textContent = "";
  box.replaceChildren(run.turns.length
    ? silencesTurnsTable(run.turns)
    : el("div", "hint", "Press Start, then speak the sequence in alternating turns. Finalized captions appear here as they are scored."));
  if (silencesAutoscrollOn() || options.force) box.scrollTop = box.scrollHeight;
}

function renderSilencesPage(options = {}) {
  const logic = silencesLogic();
  if (!logic) {
    $("sil-status").textContent = "silences.js failed to load";
    return;
  }
  const run = silencesRun();
  $("sil-start").disabled = state.silences.running;
  $("sil-stop").disabled = !state.silences.running;
  $("sil-test").disabled = state.silences.running;
  $("sil-first-role").disabled = state.silences.running;
  setSilencesAutoscroll(silencesAutoscrollOn());
  const captionData = state.silences.lastCaptionData;
  $("sil-transport-line").textContent = captionData
    ? `captions ${(captionData.captions || []).length} · ${meetTransportLine(captionData)} · ${meetTransportByRoleLine(captionData)}`
    : "caption feed not sampled yet";
  renderSilencesSummary(run);
  renderSilencesExpected(run);
  renderSilencesTurns(run, options);
  if (!state.silences.running && logic.summarizeRun(run).completed >= logic.TESTS[run.testId].tokens.length) {
    $("sil-status").textContent = "complete";
  }
}

async function silencesPollCaptions() {
  const logic = silencesLogic();
  const run = silencesRun();
  if (!logic || !run || !state.silences.running) return;
  const query = new URLSearchParams({ since: String(run.startedAtSec), fromEnd: "100" });
  const data = await api(`${MEET_BRIDGE_BASE}/captions?${query}`);
  state.silences.lastCaptionData = data;
  const captions = ((data && data.captions) || []).slice().sort((a, b) => (a.at || 0) - (b.at || 0));
  let scored = 0;
  captions.forEach((caption) => {
    if (logic.scoreCaption(run, caption)) scored += 1;
  });
  const stats = logic.summarizeRun(run);
  if (stats.completed >= stats.total) state.silences.running = false;
  $("sil-status").textContent = state.silences.running
    ? `running · ${scored} new scored turn${scored === 1 ? "" : "s"}`
    : (stats.completed >= stats.total ? "complete" : "stopped");
  renderSilencesPage();
}

async function loadSilencesCompanionStatus() {
  let health = null;
  try {
    health = await api(`${MEET_BRIDGE_BASE}/status`);
  } catch (_error) {
    // Older/offline workers are represented by the absence-safe metrics below.
  }
  syncCompanionMeetingOptions(health && health.meetingUrl);
  renderCompanionMetrics(health);
  const meetingUrl = companionMeetingKey(state.meetCompanion.meetingUrl);
  if (meetingUrl && state.meetCompanion.configMeetingUrl !== meetingUrl && !state.meetCompanion.loading) {
    await loadCompanionInterjectorConfig();
  }
}

function resetSilencesRun() {
  const logic = silencesLogic();
  if (!logic) return;
  state.silences.running = false;
  state.silences.lastCaptionData = null;
  state.silences.pendingRender = false;
  state.silences.run = logic.createRun({
    testId: $("sil-test").value,
    firstRole: $("sil-first-role").value,
    startedAtSec: Date.now() / 1000,
  });
  $("sil-status").textContent = "ready";
  renderSilencesPage({ force: true });
}

function startSilencesRun() {
  const logic = silencesLogic();
  if (!logic) return;
  state.silences.running = true;
  state.silences.lastCaptionData = null;
  state.silences.run = logic.createRun({
    testId: $("sil-test").value,
    firstRole: $("sil-first-role").value,
    startedAtSec: Date.now() / 1000,
  });
  $("sil-status").textContent = "running";
  renderSilencesPage({ force: true });
  if (!state.silences.polling) {
    state.silences.polling = true;
    silencesPollOnce();
  }
}

function stopSilencesRun() {
  state.silences.running = false;
  $("sil-status").textContent = "stopped";
  renderSilencesPage();
}

function driveNextSilenceAgentTurn() {
  // SEAM for queued task agent-voices-via-companion: speak the current expected token here.
}

function silencesPollOnce() {
  if (state.page !== "silences") {
    state.silences.polling = false;
    renderSilencesPage();
    return;
  }
  const refresh = state.silences.running
    ? Promise.all([silencesPollCaptions(), loadSilencesCompanionStatus()])
    : loadSilencesCompanionStatus();
  refresh.catch((error) => {
    $("sil-status").textContent = `caption feed error: ${error.message}`;
    pushError(`silences: ${error.message}`);
  }).finally(() => {
    if (state.page === "silences") setTimeout(silencesPollOnce, 1000);
    else state.silences.polling = false;
  });
}

async function loadSilencesWithPolling() {
  silencesRun();
  renderSilencesPage();
  if (state.silences.polling) return;
  state.silences.polling = true;
  silencesPollOnce();
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
    const data = await api(`${V1}/streams/stt_transcripts/tail?count=100`);
    const events = (data.events || []).slice().sort((a, b) => {
      const at = Date.parse(a.ts || "") || 0;
      const bt = Date.parse(b.ts || "") || 0;
      if (at !== bt) return bt - at;
      return (b.seq || 0) - (a.seq || 0);
    });
    body.replaceChildren();
    const p = panel("Recent STT transcripts (newest first) — includes hypotheses ingested from this page or any recognizer");
    if (events.length) {
      const transcriptTable = table(
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
        }),
      );
      transcriptTable.classList.add("stt-transcript-table");
      p.content.appendChild(transcriptTable);
    } else {
      p.content.appendChild(el("div", "hint", "No transcripts yet. Ingest text below, or use the mic button to test with real speech recognition."));
    }
    body.appendChild(p.root);
  } catch (error) { body.textContent = `error: ${error.message}`; }
  primeSttEngineRows();
  startSttSensitivityPolling();
}

// Self-terminating poll of the server-side capture/VAD state -- shows the mic
// "hunting" for a signal (threshold dropping below baseline while it hears
// nothing) with a live up/down indicator, and keeps the echo-policy select in
// sync with whatever is actually active. Stops itself once you navigate away
// from the STT page; loadStt() restarts it when you come back.
let sttSensPolling = false;
let sttSensLastThreshold = null;

function sttSensitivityBadgeUpdate(sens) {
  const badge = $("stt-sensitivity-indicator");
  if (!sens) { badge.textContent = "not listening"; badge.className = "badge"; sttSensLastThreshold = null; return; }
  const pct = Math.round((1 - sens.current_threshold / sens.base_threshold) * 100);
  const droppedBack = sttSensLastThreshold != null && sens.current_threshold > sttSensLastThreshold + 1e-6;
  sttSensLastThreshold = sens.current_threshold;
  if (pct <= 0) {
    badge.textContent = droppedBack ? "🔻 back to baseline" : "● baseline sensitivity";
    badge.className = "badge ok";
  } else {
    badge.textContent = `🔺 turning up +${pct}% (hunting for a signal…)`;
    badge.className = "badge warn";
  }
}

function pollSttSensitivityOnce() {
  api(`${V1}/audio/capture`).then((capture) => {
    sttSensitivityBadgeUpdate(capture.mic_sensitivity);
    const policySelect = $("stt-echo-policy");
    if (capture.echo_policy && document.activeElement !== policySelect) policySelect.value = capture.echo_policy;
  }).catch(() => {});
  if (state.page === "stt") setTimeout(pollSttSensitivityOnce, 1500);
  else sttSensPolling = false;
}

function startSttSensitivityPolling() {
  if (sttSensPolling) return;
  sttSensPolling = true;
  pollSttSensitivityOnce();
}

/* ------------------------------------------------------ live per-engine STT */
// One row per STT engine (as reported live by the server) plus a pinned,
// highlighted "disambiguator" row for the final resolved result. Populated
// from /v1/status on page load, then updated live from the WS stt_transcripts
// stream in ingest() -- no polling, no page reload needed.
const sttEngineRows = new Map();

function sttEngineLabel(engine) {
  if (engine === "google_meet") return "google_meet (Meet driver)";
  if (engine === "web_speech") return "web_speech (browser)";
  if (engine === "external") return "external (legacy/API ingest)";
  if (engine === "manual") return "manual ingest";
  return engine;
}

function sttEngineTbody() {
  let tbody = document.querySelector("#stt-engine-body tbody");
  if (tbody) return tbody;
  const host = $("stt-engine-body");
  host.replaceChildren();
  const t = table(["Engine", "Status", "Confidence", "Text", "Time"], []);
  t.classList.add("stt-engine-table");
  host.appendChild(t);
  return t.querySelector("tbody");
}

function sttEngineRow(engine) {
  let row = sttEngineRows.get(engine);
  if (row) return row;
  const tbody = sttEngineTbody();
  const tr = el("tr");
  tr.dataset.engine = engine;
  if (engine === "disambiguator") tr.classList.add("stt-final-row");
  const tdEngine = el("td"); tdEngine.appendChild(mono(sttEngineLabel(engine)));
  const tdStatus = el("td"); tdStatus.appendChild(badge("waiting…", ""));
  const tdConf = el("td"); tdConf.textContent = "—";
  const tdText = el("td"); tdText.textContent = "—";
  const tdTime = el("td"); tdTime.textContent = "—";
  tr.append(tdEngine, tdStatus, tdConf, tdText, tdTime);
  // Keep the disambiguator's final-result row pinned to the bottom, visually
  // separated from the individual per-engine hypotheses above it.
  if (engine === "disambiguator") {
    tbody.appendChild(tr);
  } else {
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const nextRow = rows.find(r => r.classList.contains("stt-final-row") || (r.dataset.engine && r.dataset.engine.localeCompare(engine) > 0));
    if (nextRow) {
      tbody.insertBefore(tr, nextRow);
    } else {
      tbody.appendChild(tr);
    }
  }
  row = { tr, tdStatus, tdConf, tdText, tdTime };
  sttEngineRows.set(engine, row);
  return row;
}

function primeSttEngineRows() {
  api(`${V1}/status`).then((status) => {
    let engines = (status.subsystems && status.subsystems.stt && status.subsystems.stt.engines) || [];
    engines = [...engines].sort((a, b) => a.localeCompare(b));
    $("stt-engine-hint").textContent = engines.length
      ? `Configured server drivers: ${engines.join(", ")}, then disambiguator. google_meet reads the Meet bridge; browser Web Speech is a separate test and appears as web_speech.`
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
        engine: opts.engine || $("stt-engine").value || "manual",
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

let sttHoverPaused = false;
let sttScrollPaused = false;

function sttLiveLog(text) {
  const log = $("stt-live-log");
  if (!log) return;
  const line = el("div", "mono", `${new Date().toLocaleTimeString()}  ${text}`);
  log.appendChild(line);
  if (!sttHoverPaused && !sttScrollPaused && !hasSelectionIn("stt-live-log")) {
    log.scrollTop = log.scrollHeight;
  }
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
    $("stt-mic").textContent = "⏹ Stop browser Web Speech";
    $("stt-live-partial").textContent = "…";
    setSpeechIndicator("🗣️ speaking…", "ok");
  };
  // The browser's own end-of-utterance detector -- the "thinks I'm done
  // talking" signal that was asked for. Fires before onend/onerror.
  recognizer.onspeechstart = () => setSpeechIndicator("🗣️ speaking…", "ok");
  recognizer.onspeechend = () => {
    setSpeechIndicator("⏸ done talking (silence detected)", "warn");
    if ($("stt-beep-enabled").checked) playBeep();
  };
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
    $("stt-mic").textContent = "🎤 Start browser Web Speech";
    $("stt-live-partial").textContent = "Click \"Start browser Web Speech\" and speak…";
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
        sttIngest(transcript, { engine: "web_speech", is_final: true, confidence });
      } else {
        // Interim hypothesis: reflect it live as it changes, word by word.
        $("stt-live-partial").textContent = transcript;
        if ($("stt-send-partials").checked) {
          sttIngest(transcript, {
            engine: "web_speech",
            is_final: false,
            confidence,
            silent: true,
          });
        }
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
      $("stt-mic").textContent = "🎤 Start browser Web Speech";
      $("stt-live-partial").textContent = "Click \"Start browser Web Speech\" and speak…";
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
  let sortCol = -1;
  let sortDir = 1;

  const ths = headers.map((h, i) => {
    const th = el("th", null, h);
    th.style.cursor = "pointer";
    th.title = "Click to sort";
    th.addEventListener("click", () => {
      const tbody = t.querySelector("tbody");
      if (!tbody) return;
      if (sortCol === i) {
        sortDir *= -1;
      } else {
        sortCol = i;
        sortDir = 1;
      }

      const rowsArr = Array.from(tbody.querySelectorAll("tr"));
      const pinned = [];
      const sortable = [];
      rowsArr.forEach((r) => {
        if (r.classList.contains("stt-final-row") || r.classList.contains("pinned-row")) {
          pinned.push(r);
        } else {
          sortable.push(r);
        }
      });

      sortable.sort((a, b) => {
        const cellA = a.children[i];
        const cellB = b.children[i];
        const textA = cellA ? cellA.textContent.trim() : "";
        const textB = cellB ? cellB.textContent.trim() : "";

        const numA = parseFloat(textA.replace(/[^0-9.-]/g, ""));
        const numB = parseFloat(textB.replace(/[^0-9.-]/g, ""));
        if (!isNaN(numA) && !isNaN(numB) && /^[\d.,%ms-]+$/.test(textA) && /^[\d.,%ms-]+$/.test(textB)) {
           return (numA - numB) * sortDir;
        }
        return textA.localeCompare(textB) * sortDir;
      });

      sortable.forEach(r => tbody.appendChild(r));
      pinned.forEach(r => tbody.appendChild(r));

      ths.forEach((c, idx) => {
        c.textContent = headers[idx] + (idx === sortCol ? (sortDir === 1 ? " ▲" : " ▼") : "");
      });
    });
    return th;
  });

  ths.forEach((th) => hr.appendChild(th));
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
  createView({
    id: "browserNav", scrollId: "br-nav-scroll", spacerId: "br-nav-spacer", viewportId: "br-nav-viewport",
    jumpId: "br-nav-jump", unseenId: "br-nav-unseen", countsId: "br-nav-counts",
    streams: BROWSER_NAV_STREAMS,
    filter: (event) => (event.data || {}).phase !== "intent",
    render: renderBrowserNavRow,
    newestFirst: true,
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

  document.addEventListener("selectionchange", () => {
    Object.values(state.views).forEach((v) => {
      if (v.updatePauseState) v.updatePauseState(true);
    });
    if (state.silences.pendingRender && !hasSelectionIn("sil-turns") && !silencesScrolledUp()) {
      renderSilencesTurns(silencesRun(), { force: true });
    }
  });

  const sttLog = $("stt-live-log");
  if (sttLog) {
    sttLog.addEventListener("mouseenter", () => { sttHoverPaused = true; });
    sttLog.addEventListener("mouseleave", () => { sttHoverPaused = false; });
    sttLog.addEventListener("scroll", () => {
      sttScrollPaused = sttLog.scrollHeight - sttLog.scrollTop - sttLog.clientHeight >= 40;
    });
  }

  // transcript controls
  ["tr-search", "tr-finals", "tr-hide-partials", "tr-hide-echo", "tr-hide-routine", "tr-minconf", "tr-cap"]
    .forEach((id) => $(id).addEventListener("input", refreshTranscriptFilter));
  $("tr-pause").onclick = () => {
    const view = state.views.transcript;
    view.setPaused(!view.manualPaused);
    $("tr-pause").textContent = view.manualPaused ? "Resume" : "Pause";
    $("tr-pause").classList.toggle("primary", !view.manualPaused);
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
    view.setPaused(!view.manualPaused);
    $("cv-pause").textContent = view.manualPaused ? "Resume" : "Pause";
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
    view.setPaused(!view.manualPaused);
    $("st-pause").textContent = view.manualPaused ? "Resume" : "Pause";
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
  $("meet-refresh").onclick = () => {
    loadMeetRoleAssignments();
    loadCompanionInterjectorConfig();
    loadMeet();
  };
  $("meet-join-btn").onclick = () => {
    const url = $("meet-join-url").value.trim();
    if (!url) { pushError("Enter a meeting URL to join."); return; }
    postMeetCommand(`/join ${url}`);
  };
  $("meet-new-btn").onclick = () => postMeetCommand("/new");
  $("meet-say-btn").onclick = () => {
    const text = $("meet-say-text").value.trim();
    if (!text) { pushError("Enter text to speak into the meeting."); return; }
    postMeetCommand(`/say ${text}`);
  };
  $("meet-companion-meeting").onchange = () => {
    state.meetCompanion.meetingUrl = companionMeetingKey($("meet-companion-meeting").value);
    $("meet-companion-meeting").dataset.signature = "";
    syncCompanionMeetingOptions(state.meetCompanion.health && state.meetCompanion.health.meetingUrl);
    loadCompanionInterjectorConfig();
    renderCompanionMetrics(state.meetCompanion.health);
  };
  $("meet-companion-form").onsubmit = saveCompanionInterjector;
  $("meet-companion-reset").onclick = resetCompanionInterjector;
  $("meet-companion-mode").onchange = updateCompanionConditionalFields;
  $("sil-start").onclick = startSilencesRun;
  $("sil-stop").onclick = stopSilencesRun;
  $("sil-reset").onclick = resetSilencesRun;
  $("sil-test").onchange = resetSilencesRun;
  $("sil-first-role").onchange = resetSilencesRun;
  $("sil-autoscroll").onclick = () => {
    const next = !silencesAutoscrollOn();
    setSilencesAutoscroll(next);
    if (next) renderSilencesTurns(silencesRun(), { force: true });
  };
  $("sil-jump").onclick = () => {
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
    renderSilencesTurns(silencesRun(), { force: true });
  };
  $("sil-drive-next").onclick = driveNextSilenceAgentTurn;
  $("sil-turns").addEventListener("mouseleave", () => {
    if (state.silences.pendingRender && !silencesScrolledUp() && !hasSelectionIn("sil-turns")) {
      renderSilencesTurns(silencesRun(), { force: true });
    }
  });
  $("sil-turns").addEventListener("scroll", () => {
    if (!silencesScrolledUp() && state.silences.pendingRender && !hasSelectionIn("sil-turns")) {
      renderSilencesTurns(silencesRun(), { force: true });
    } else if (silencesScrolledUp() && state.silences.run && state.silences.run.turns.length) {
      $("sil-jump").hidden = false;
      $("sil-paused-note").textContent = "Turn list paused while hovered, selected, or scrolled up.";
    }
  });
  $("br-refresh").onclick = () => loadBrowserSettings();
  $("br-nav-pause").onclick = () => {
    const view = state.views.browserNav;
    view.setPaused(!view.manualPaused);
    $("br-nav-pause").textContent = view.manualPaused ? "Resume nav log" : "Pause nav log";
  };
  $("br-nav-backfill").onclick = () => backfill(BROWSER_NAV_STREAMS, state.views.browserNav);
  $("br-nav-export").onclick = () => exportView(state.views.browserNav, "browser-nav-intents");
  $("ps-refresh").onclick = loadProcesses;
  $("vc-refresh").onclick = loadVoices;
  $("vc-assign").onclick = async () => {
    try { await api(`${V1}/voices/assign`, { method: "POST", body: { policy: $("vc-policy").value } }); loadVoices(); }
    catch (error) { pushError(error.message); }
  };
  $("stt-refresh").onclick = loadStt;
  $("stt-send").onclick = () => sttIngest($("stt-text").value);
  $("stt-text").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sttIngest($("stt-text").value); } });
  $("stt-mic").onclick = toggleSttMic;
  $("stt-echo-policy").onchange = async () => {
    const policy = $("stt-echo-policy").value;
    const status = $("stt-policy-status");
    status.textContent = "applying…";
    try {
      await api(`${V1}/audio/echo-policy`, { method: "POST", body: { policy } });
      status.textContent = "✓ applied";
    } catch (error) {
      status.textContent = `⚠ ${error.message}`;
    }
  };
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
  installPageStatePersistence();
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
