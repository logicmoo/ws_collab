"""JavaScript snippets injected into the Meet tab over CDP.

These encode hard-won, live-tested knowledge of Google Meet's DOM (which
changes between Meet releases without notice) -- reproduced verbatim from the
original design rather than rewritten, since none of it can be safely
re-verified without live Meet access. See each snippet's own comment for the
specific behavior it depends on.
"""

from __future__ import annotations

# Meet's own CSS class names churn across releases, so guessing specific
# classes (the old approach) silently breaks and, worse, can silently DROP
# captions when a text-pattern heuristic misfires with no visible error.
# Instead: find the captions region using only its stable semantic
# aria-label/role. Prefer an `[aria-live]` match specifically -- that's the
# accessibility signal for a small, transient, live-updating region (the
# actual live-caption ticker), as opposed to Meet's separate, non-live
# scrolling transcript/history panel, which can ALSO match a broad
# aria-label search and (being a full growing history, not a live line)
# will always have far more text -- a "pick whichever has the most text"
# heuristic actively prefers the WRONG one. Only fall back to the biggest
# candidate if nothing is marked aria-live at all. Track each caption ROW
# by DOM ELEMENT IDENTITY (a stable key assigned the first time that exact
# node is seen, kept on `window` across polls) rather than by guessing from
# its text content -- a row is either a brand new DOM node (new utterance)
# or the SAME node still growing (interim speech), unambiguous and immune
# to class-name churn since it depends only on the region's own generic
# child structure and DOM identity, both stable.
CAPTION_PAYLOAD_HELPERS_JS = r"""
(() => {
  if (window.__wsCollabReadCaptionPayload) return;
  window.__wsCollabFindCaptionRegion = () => {
  const labelSel = 'div[aria-label*="aption" i], div[role="region"][aria-label*="ubtitle" i], div[role="region"][aria-label*="aption" i]';
  const candidates = [...document.querySelectorAll(labelSel)];
  const liveOnes = candidates.filter((c) => c.hasAttribute("aria-live") || c.closest("[aria-live]"));
  let region = null, bestLen = -1;
  const pool = liveOnes.length ? liveOnes : candidates;
  for (const c of pool) {
    const len = (c.innerText || "").length;
    if (len > bestLen) { region = c; bestLen = len; }
  }
  return region;
  };
  window.__wsCollabReadCaptionPayload = () => {
    const region = window.__wsCollabFindCaptionRegion();
    if (!region) {
      const inCall = !!document.querySelector('button[aria-label*="captions" i], [data-is-muted]');
      return { ok: false, note: inCall ? "captions look OFF - press c in the Meet" : "not in a call yet?", rawText: "", rawRows: [], rowCount: 0, childCount: 0 };
    }
  const normalizedText = (value) => (value || "").replace(/\s+/g, " ").trim();
  const diagnosticRawText = (el) => {
    const children = [...el.children];
    if (!children.length) return normalizedText(el.innerText || "");
    return children
      .map((child) => normalizedText(child.innerText || ""))
      .filter((text) => text)
      .join(" | ");
  };
  const diagnosticRegionRawText = (region) => {
    const children = [...region.children];
    if (!children.length) return normalizedText(region.innerText || "");
    return children
      .map((child) => diagnosticRawText(child))
      .filter((text) => text)
      .join(" \u2016 ");
  };
  const rawText = diagnosticRegionRawText(region);
  const childCount = region.children.length;
  window.__meetCaptionRows = window.__meetCaptionRows || new Map();
  const seen = window.__meetCaptionRows;
  let rowEls = [...region.children].filter(el => (el.innerText || "").trim().length > 0);
  // In Meet's "single continuously-growing row" mode the region can hold
  // its text directly (no per-utterance child elements at all) -- fall back
  // to treating the region itself as the one row rather than silently
  // dropping everything just because it has no useful children.
  let fallbackNote = null;
  if (!rowEls.length && (region.innerText || "").trim()) {
    rowEls = [region];
    fallbackNote = `no child rows; using region itself (childCount=${region.children.length})`;
  }
  const rows = [];
  const rawRows = [];
  const liveKeys = [];
  rowEls.forEach((rowEl) => {
    let info = seen.get(rowEl);
    if (!info) {
      info = { key: `row-${seen.size}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}` };
      seen.set(rowEl, info);
    }
    liveKeys.push(info.key);
    const rawRowText = diagnosticRawText(rowEl);
    rawRows.push({ key: info.key, rawText: rawRowText, childCount: rowEl.children.length });
    // Best-effort speaker split: if this row has more than one child
    // element, treat the first as the speaker name and the rest as the
    // caption text -- a generic structural guess, never a class-name
    // dependency. If it doesn't cleanly apply, fall back to the row's
    // whole text under a generic "Speaker" label rather than dropping it.
    let speaker = "Speaker";
    let text = (rowEl.innerText || "").trim();
    if (rowEl.children.length > 1) {
      const nameText = (rowEl.children[0].innerText || "").trim();
      const restText = [...rowEl.children].slice(1).map((c) => c.innerText || "").join(" ").replace(/\s+/g, " ").trim();
      if (nameText && restText && nameText.length < 60) { speaker = nameText; text = restText; }
    }
    text = text.replace(/\s+/g, " ").trim();
    if (text) rows.push({ key: info.key, speaker, text });
  });
  // Forget rows no longer present at all, so this Map never grows
  // unbounded across a long meeting.
  for (const el of [...seen.keys()]) { if (!rowEls.includes(el)) seen.delete(el); }
  return { ok: true, rows, liveKeys, note: fallbackNote, rawText, rawRows, rowCount: rowEls.length, childCount };
  };
})()
"""

CAPTIONS_JS = r"""
(() => {
  %s
  return JSON.stringify(window.__wsCollabReadCaptionPayload());
})()
""" % CAPTION_PAYLOAD_HELPERS_JS

CAPTION_OBSERVER_JS = r"""
(() => {
  %s
  const bindingName = "__wsCollabCaptionPush";
  const existing = window.__wsCollabCaptionObserver;
  if (existing && existing.installed) {
    existing.attach();
    existing.schedule("refresh");
    return "already-installed";
  }
  const state = {
    installed: true,
    region: null,
    observer: null,
    rootObserver: null,
    pending: false,
    timer: null,
    queue: [],
    maxQueue: 5,
    lastLogAt: 0,
    log(message) {
      const at = Date.now();
      if (at - this.lastLogAt < 5000) return;
      this.lastLogAt = at;
      try {
        if (window.__wsCollabCaptionPushDebug && window.console && console.debug) {
          console.debug("[ws_collab captions]", message);
        }
      } catch (_error) {}
    },
    enqueue(serialized) {
      this.queue.push(serialized);
      if (this.queue.length > this.maxQueue) {
        this.queue.splice(0, this.queue.length - this.maxQueue);
      }
    },
    deliver(serialized) {
      try {
        const binding = window[bindingName];
        if (typeof binding !== "function") return false;
        binding(serialized);
        return true;
      } catch (error) {
        this.log(error && error.message ? error.message : "binding call failed");
        return false;
      }
    },
    flush() {
      while (this.queue.length) {
        if (!this.deliver(this.queue[0])) return;
        this.queue.shift();
      }
    },
    attach() {
      try {
        const nextRegion = window.__wsCollabFindCaptionRegion();
        if (nextRegion === this.region && this.observer) return;
        if (this.observer) this.observer.disconnect();
        this.observer = null;
        this.region = nextRegion;
        if (nextRegion) {
          this.observer = new MutationObserver(() => {
            try { this.schedule("caption-mutation"); } catch (error) { this.log(error && error.message ? error.message : "mutation failed"); }
          });
          this.observer.observe(nextRegion, { childList: true, subtree: true, characterData: true });
        }
      } catch (error) {
        this.log(error && error.message ? error.message : "attach failed");
      }
    },
    emit(reason) {
      try {
        this.attach();
        const payload = window.__wsCollabReadCaptionPayload();
        this.enqueue(JSON.stringify(payload));
        this.flush();
      } catch (error) {
        this.log(error && error.message ? error.message : "emit failed");
      }
    },
    schedule(reason) {
      try {
        if (this.pending) return;
        this.pending = true;
        const fire = () => {
          this.timer = window.setTimeout(() => {
            try {
              this.pending = false;
              this.emit(reason);
            } catch (error) {
              this.pending = false;
              this.log(error && error.message ? error.message : "timer failed");
            }
          }, 50);
        };
        if (typeof window.requestAnimationFrame === "function") {
          window.requestAnimationFrame(() => {
            try { fire(); } catch (error) { this.pending = false; this.log(error && error.message ? error.message : "raf failed"); }
          });
        } else {
          fire();
        }
      } catch (error) {
        this.pending = false;
        this.log(error && error.message ? error.message : "schedule failed");
      }
    },
  };
  window.__wsCollabCaptionObserver = state;
  state.rootObserver = new MutationObserver(() => {
    try {
      state.attach();
      state.schedule("document-mutation");
    } catch (error) {
      state.log(error && error.message ? error.message : "document mutation failed");
    }
  });
  try {
    state.rootObserver.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-label", "aria-live", "role"],
    });
    state.attach();
    state.schedule("install");
  } catch (error) {
    state.log(error && error.message ? error.message : "install failed");
  }
  return "installed";
})()
""" % CAPTION_PAYLOAD_HELPERS_JS


# Unattended join: on the pre-join screen turn the camera OFF, set the mic,
# and click Join; once in the call, auto-admit knockers, answer Google's
# solo-meeting "Are you still there?" check, and keep captions switched on.
# mic policy: "keep"     -- never touch the mic (the host: manual mutes rule)
#             "muted"    -- companion at rest: force/keep it muted
#             "speaking" -- companion during SAPI/click playback: ensure unmuted
#                          (its mic is a fake audio device, never the room mic)
def autojoin_js(policy: str) -> str:
    want = {"keep": "null", "muted": "false", "speaking": "true"}[policy]
    return r"""
(() => {
  const WANT_MIC = %s;
  const byLabel = (pattern) =>
    [...document.querySelectorAll('button, [role="button"]')].find(
      (b) => pattern.test(b.getAttribute("aria-label") || ""));
  // Google's solo-meeting "Are you still there?" check: always stay.
  const stay = [...document.querySelectorAll("button")].find((b) =>
    /stay in the call|keep waiting|i'm here|im here/i.test((b.textContent || "") + (b.getAttribute("aria-label") || "")));
  if (stay) { stay.click(); return "stayed-in-call"; }
  if (document.querySelector('button[aria-label*="eave call" i]')) {
    // The toolbar "Admit N guest(s)" notification is itself a <div
    // role="button"> that only OPENS the People panel -- it is not the
    // actual admit action, and it sorts before the real button in DOM
    // order, so a prefix match on "admit" keeps re-clicking it forever
    // without ever admitting anyone. The real action is a plain <button>
    // reading exactly "Admit" (per-person, aria-label "Admit <name>") or
    // "Admit all" inside that panel -- match those exactly ($-anchored).
    // Google shows an "Admit all? / <name> / Cancel / Admit all"
    // confirmation dialog when a knocker shares a display name with
    // someone already in the call (true for a HOST+COMPANION pair using
    // the same operator's accounts). Only auto-confirm that ONE specific
    // dialog for a private room only intended participants know the URL
    // of -- stay scoped to it: only click a button inside the dialog
    // whose text is itself "Admit"/"Admit all" ($-anchored), never any
    // other dialog Meet might show (e.g. a future "Leave meeting?"
    // prompt), which still gets left untouched for a human to resolve.
    const openDialog = document.querySelector('[role="dialog"], [role="alertdialog"]');
    if (openDialog) {
      const confirmAdmit = [...openDialog.querySelectorAll('button, [role="button"]')].find((b) =>
        /^admit( all)?$/i.test((b.textContent || "").trim()));
      if (confirmAdmit) { confirmAdmit.click(); return "admitted-via-confirmation"; }
    } else {
      const admit = [...document.querySelectorAll('button, [role="button"]')].find((b) =>
        /^admit( all)?$/i.test((b.textContent || "").trim()));
      if (admit) { admit.click(); return "admitted"; }
      const chip = [...document.querySelectorAll('[role="button"]')].find((b) =>
        /^admit \d/i.test((b.textContent || "").trim()));
      if (chip) { chip.click(); return "admit-panel-opened"; }
    }
    if (WANT_MIC === false) {
      const mic = byLabel(/turn off microphone/i);
      if (mic) { mic.click(); return "muted"; }
    } else if (WANT_MIC === true) {
      const mic = byLabel(/turn on microphone/i);
      if (mic) { mic.click(); return "unmuted-for-speech"; }
    }
    const cc = byLabel(/turn on captions/i);
    if (cc) { cc.click(); return "captions-clicked"; }
    if (openDialog) { return "unrecognized-dialog-open"; }
    return "in-call";
  }
  if (WANT_MIC === false) {
    const micOff = byLabel(/turn off microphone/i);
    if (micOff) micOff.click();
  } else if (WANT_MIC === true) {
    const micOn = byLabel(/turn on microphone/i);
    if (micOn) micOn.click();
  }
  // WANT_MIC === null ("keep"): leave the mic exactly as it already is.
  const camOff = byLabel(/turn off camera/i);
  if (camOff) camOff.click();
  const join = [...document.querySelectorAll("button")].find((b) =>
    /join now|ask to join|join anyway|rejoin/i.test((b.textContent || "") + (b.getAttribute("aria-label") || "")));
  if (join && !join.disabled) { join.click(); return "join-clicked"; }
  return "waiting-prejoin";
})()
""" % want


# ---- OUT: post mailbox replies into the Meet chat --------------------------
SEND_CHAT_JS_TEMPLATE = r"""
(() => {
  const TEXT = %s;
  let input = document.querySelector('textarea[aria-label*="essage" i], input[aria-label*="essage" i], textarea[placeholder*="essage" i]');
  if (!input) {
    const opener = document.querySelector('button[aria-label*="chat" i], button[aria-label*="everyone" i]');
    if (opener) { opener.click(); return "opened-chat-retry"; }
    return "no-chat-input";
  }
  const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(input, TEXT);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  const send = [...document.querySelectorAll("button")].find(b => /send/i.test(b.getAttribute("aria-label") || "") && !b.disabled);
  if (send) { send.click(); return "sent"; }
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
  return "sent-enter";
})()
"""


# ---- Synthetic mic: SAPI speech INTO the meeting -- no virtual-cable driver.
#
# The companion tab's getUserMedia is patched so the "microphone" Meet sees
# is a WebAudio MediaStreamDestination we control. To talk, the bridge
# synthesizes a WAV with Windows SAPI, ships it into the tab over CDP as
# base64, and plays it into that destination. The REAL room mic is never
# touched by the companion.
GUM_PATCH_JS = r"""
(() => {
  if (window.__sapiPatched) return;
  window.__sapiPatched = true;
  const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    if (constraints && constraints.audio) {
      if (!window.__sapiCtx) {
        window.__sapiCtx = new AudioContext({ sampleRate: 48000 });
        window.__sapiSink = window.__sapiCtx.createMediaStreamDestination();
        // A silent keep-alive so the track always produces samples.
        const silence = window.__sapiCtx.createGain();
        silence.gain.value = 0.0001;
        const osc = window.__sapiCtx.createOscillator();
        osc.connect(silence).connect(window.__sapiSink);
        osc.start();
      }
      const stream = new MediaStream(window.__sapiSink.stream.getAudioTracks());
      if (constraints.video) {
        try {
          const cam = await real({ video: constraints.video });
          cam.getVideoTracks().forEach((track) => stream.addTrack(track));
        } catch (error) { /* camera denied/absent is fine */ }
      }
      return stream;
    }
    return real(constraints);
  };
})();
"""

SPEAK_INTO_MEETING_JS = r"""
(async () => {
  const B64 = %s;
  if (!window.__sapiCtx || !window.__sapiSink) return "no-synthetic-mic";
  const ctx = window.__sapiCtx;
  if (ctx.state === "suspended") await ctx.resume();
  const bytes = Uint8Array.from(atob(B64), (c) => c.charCodeAt(0));
  const buffer = await ctx.decodeAudioData(bytes.buffer);
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(window.__sapiSink);
  source.start();
  return "speaking:" + Math.round(buffer.duration * 1000);
})()
"""


COMPANION_CLICK_JS = r"""
(() => {
  const ENABLED = %s;
  const INTERVAL_MS = %s;
  const DURATION_SECONDS = %s;
  const AMPLITUDE = %s;
  const FIXED_INTERVAL = %s;
  const SOUND = %s;
  const F0 = %s;
  const F1 = %s;
  const F2 = %s;
  const now = Date.now();
  const describe = (state, status) => JSON.stringify({
    enabled: !!(state && state.enabled),
    intervalMs: state ? state.intervalMs : INTERVAL_MS,
    durationMs: Math.round((state ? state.durationSeconds : DURATION_SECONDS) * 1000),
    gain: state ? state.amplitude : AMPLITUDE,
    sound: state ? state.sound : SOUND,
    f0: state ? state.f0 : F0,
    f1: state ? state.f1 : F1,
    f2: state ? state.f2 : F2,
    fixedInterval: !!(state && state.fixedInterval),
    installedAt: state ? state.installedAt : null,
    lastClickAt: state ? state.lastClickAt : null,
    clickCount: state ? state.clickCount || 0 : 0,
    lastError: state ? state.lastError || null : null,
    status,
  });
  const stopTicker = (state) => {
    try {
      if (state && state.intervalId) window.clearInterval(state.intervalId);
    } catch (_error) {}
  };
  const previous = window.__wsCollabCompanionClick;
  if (!ENABLED) {
    if (previous) {
      stopTicker(previous);
      previous.enabled = false;
      previous.intervalId = null;
      previous.status = "disabled";
      return describe(previous, "disabled");
    }
    return describe({ enabled: false, intervalMs: INTERVAL_MS, installedAt: null, lastClickAt: null, clickCount: 0, lastError: null }, "disabled");
  }
  if (previous && previous.enabled && previous.intervalMs === INTERVAL_MS && (!FIXED_INTERVAL || previous.intervalId) &&
      previous.durationSeconds === DURATION_SECONDS && previous.amplitude === AMPLITUDE &&
      previous.fixedInterval === FIXED_INTERVAL && previous.sound === SOUND &&
      previous.f0 === F0 && previous.f1 === F1 && previous.f2 === F2) {
    return describe(previous, "already-installed");
  }
  if (previous) stopTicker(previous);
  const state = {
    enabled: true,
    intervalMs: INTERVAL_MS,
    intervalId: null,
    installedAt: now,
    lastClickAt: previous ? previous.lastClickAt || null : null,
    clickCount: previous ? previous.clickCount || 0 : 0,
    lastError: null,
    status: "installed",
    durationSeconds: DURATION_SECONDS,
    amplitude: AMPLITUDE,
    fixedInterval: FIXED_INTERVAL,
    sound: SOUND,
    f0: F0,
    f1: F1,
    f2: F2,
  };
  state.fire = async () => {
    try {
      if (!state.enabled) return;
      const ctx = window.__sapiCtx;
      const sink = window.__sapiSink;
      if (!ctx || !sink) {
        state.lastError = "synthetic-mic-unavailable";
        return;
      }
      if (ctx.state === "suspended") await ctx.resume();
      if (state.sound === "uh") {
        const startAt = ctx.currentTime + 0.01;
        const stopAt = startAt + state.durationSeconds;
        const osc = ctx.createOscillator();
        const f1 = ctx.createBiquadFilter();
        const f2 = ctx.createBiquadFilter();
        const g1 = ctx.createGain();
        const g2 = ctx.createGain();
        const out = ctx.createGain();
        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(state.f0, startAt);
        f1.type = "bandpass";
        f1.frequency.setValueAtTime(state.f1, startAt);
        f1.Q.setValueAtTime(5, startAt);
        f2.type = "bandpass";
        f2.frequency.setValueAtTime(state.f2, startAt);
        f2.Q.setValueAtTime(6, startAt);
        g1.gain.value = 0.95;
        g2.gain.value = 0.70;
        out.gain.setValueAtTime(0.0001, startAt);
        out.gain.linearRampToValueAtTime(state.amplitude, startAt + Math.min(0.018, state.durationSeconds * 0.3));
        out.gain.exponentialRampToValueAtTime(0.0001, stopAt);
        osc.connect(f1).connect(g1).connect(out);
        osc.connect(f2).connect(g2).connect(out);
        out.connect(sink);
        osc.onended = () => {
          try { osc.disconnect(); f1.disconnect(); f2.disconnect(); g1.disconnect(); g2.disconnect(); out.disconnect(); } catch (_error) {}
        };
        osc.start(startAt);
        osc.stop(stopAt + 0.01);
        state.lastClickAt = Date.now();
        state.clickCount = (state.clickCount || 0) + 1;
        state.lastError = null;
        return;
      }
      const sampleRate = ctx.sampleRate || 48000;
      const frames = Math.max(1, Math.floor(sampleRate * state.durationSeconds));
      const attackFrames = Math.max(1, Math.floor(sampleRate * 0.0015));
      const buffer = ctx.createBuffer(1, frames, sampleRate);
      const data = buffer.getChannelData(0);
      for (let i = 0; i < frames; i += 1) {
        const attack = Math.min(1, i / attackFrames);
        const decay = Math.exp(-3.0 * i / frames);
        const envelope = attack * decay;
        const tone = Math.sin(2 * Math.PI * 1800 * i / sampleRate) * 0.65;
        const noise = (Math.random() * 2 - 1) * 0.35;
        data[i] = (tone + noise) * state.amplitude * envelope;
      }
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(sink);
      source.onended = () => {
        try { source.disconnect(); } catch (_error) {}
      };
      source.start();
      state.lastClickAt = Date.now();
      state.clickCount = (state.clickCount || 0) + 1;
      state.lastError = null;
    } catch (error) {
      state.lastError = error && error.message ? error.message : String(error);
    }
  };
  window.__wsCollabCompanionClick = state;
  if (FIXED_INTERVAL) {
    state.intervalId = window.setInterval(() => {
      try { state.fire(); } catch (error) { state.lastError = error && error.message ? error.message : String(error); }
    }, INTERVAL_MS);
    try { state.fire(); } catch (error) { state.lastError = error && error.message ? error.message : String(error); }
  }
  return describe(state, previous ? "reinstalled" : "installed");
})()
"""


COMPANION_CLICK_ONCE_JS = r"""
(async () => {
  const state = window.__wsCollabCompanionClick;
  if (!state || !state.enabled || typeof state.fire !== "function") {
    return JSON.stringify({ ok: false, status: "not-installed" });
  }
  await state.fire();
  return JSON.stringify({
    ok: !state.lastError,
    status: state.lastError ? "error" : "clicked",
    lastClickAt: state.lastClickAt || null,
    clickCount: state.clickCount || 0,
    lastError: state.lastError || null,
  });
})()
"""


COMPANION_AUDIO_RMS_JS = r"""
(async () => {
  const THRESHOLD = %s;
  const now = Date.now();
  const state = window.__wsCollabIncomingAudioRms || {
    ctx: null,
    source: null,
    analyser: null,
    data: null,
    streamId: null,
    quietSince: null,
    rms: null,
    lastError: null,
  };
  window.__wsCollabIncomingAudioRms = state;
  state.threshold = THRESHOLD;
  const streamKey = (stream) => {
    try {
      return `${stream.id || "stream"}:${stream.getAudioTracks().map((track) => track.id || track.label || "track").join(",")}`;
    } catch (_error) {
      return null;
    }
  };
  const findIncomingStream = () => {
    const elements = Array.from(document.querySelectorAll("audio,video"));
    for (const element of elements) {
      let stream = element.srcObject && typeof element.srcObject.getAudioTracks === "function" ? element.srcObject : null;
      if (!stream && typeof element.captureStream === "function") {
        try { stream = element.captureStream(); } catch (_error) { stream = null; }
      }
      if (!stream || typeof stream.getAudioTracks !== "function") continue;
      const liveTracks = stream.getAudioTracks().filter((track) => track.readyState === "live");
      if (liveTracks.length) return new MediaStream(liveTracks);
    }
    return null;
  };
  try {
    const stream = findIncomingStream();
    if (!stream) throw new Error("incoming-audio-stream-unavailable");
    const key = streamKey(stream);
    if (!state.ctx) state.ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (state.ctx.state === "suspended") await state.ctx.resume();
    if (!state.analyser || state.streamId !== key) {
      try { if (state.source) state.source.disconnect(); } catch (_error) {}
      state.source = state.ctx.createMediaStreamSource(stream);
      state.analyser = state.ctx.createAnalyser();
      state.analyser.fftSize = 2048;
      state.data = new Uint8Array(state.analyser.fftSize);
      state.source.connect(state.analyser);
      state.streamId = key;
      state.quietSince = null;
    }
    state.analyser.getByteTimeDomainData(state.data);
    let sum = 0;
    for (let i = 0; i < state.data.length; i += 1) {
      const sample = (state.data[i] - 128) / 128;
      sum += sample * sample;
    }
    const rms = Math.sqrt(sum / Math.max(1, state.data.length));
    state.rms = rms;
    if (rms <= THRESHOLD) {
      if (!state.quietSince) state.quietSince = now;
    } else {
      state.quietSince = null;
    }
    state.lastError = null;
    return JSON.stringify({
      ok: true,
      status: "audio-rms",
      rms,
      threshold: THRESHOLD,
      quietMs: state.quietSince ? now - state.quietSince : 0,
      streamId: state.streamId,
    });
  } catch (error) {
    state.lastError = error && error.message ? error.message : String(error);
    return JSON.stringify({
      ok: false,
      status: "audio-unavailable",
      rms: state.rms,
      threshold: THRESHOLD,
      quietMs: state.quietSince ? now - state.quietSince : 0,
      lastError: state.lastError,
    });
  }
})()
"""


ROUTE_COMPANION_AUDIO_OUTPUT_JS = r"""
(async () => {
  const NEEDLE = %s;
  const result = {
    ok: false,
    status: "not-run",
    deviceId: null,
    deviceLabel: null,
    elementCount: 0,
    routedCount: 0,
    lastError: null,
  };
  const elements = Array.from(document.querySelectorAll("audio,video"));
  result.elementCount = elements.length;
  const muteAll = () => {
    for (const element of elements) {
      try { element.muted = true; element.volume = 0; } catch (_error) {}
    }
  };
  try {
    if (!NEEDLE) {
      muteAll();
      result.status = "disabled";
      return JSON.stringify(result);
    }
    const sample = elements.find((element) => typeof element.setSinkId === "function");
    if (!sample) {
      muteAll();
      result.status = "set-sink-id-unavailable";
      result.lastError = "browser does not expose HTMLMediaElement.setSinkId; companion audio remains muted";
      return JSON.stringify(result);
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== "function") {
      muteAll();
      result.status = "enumerate-devices-unavailable";
      result.lastError = "browser cannot enumerate audio output devices; companion audio remains muted";
      return JSON.stringify(result);
    }
    const outputs = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "audiooutput");
    const match = outputs.find((device) => (device.label || "").toLowerCase().includes(NEEDLE));
    if (!match) {
      muteAll();
      result.status = "output-device-not-found";
      result.lastError = `no browser audio output device label contains "${NEEDLE}"`;
      return JSON.stringify(result);
    }
    for (const element of elements) {
      if (typeof element.setSinkId !== "function") continue;
      await element.setSinkId(match.deviceId);
      element.muted = false;
      element.volume = 1;
      result.routedCount += 1;
    }
    result.ok = result.routedCount > 0;
    result.status = result.ok ? "routed" : "no-media-elements-routed";
    result.deviceId = match.deviceId;
    result.deviceLabel = match.label || null;
    return JSON.stringify(result);
  } catch (error) {
    muteAll();
    result.status = "routing-error";
    result.lastError = error && error.message ? error.message : String(error);
    return JSON.stringify(result);
  }
})()
"""


# Selects a device by name in Meet's own in-call Audio Settings mic dropdown
# (so Meet actually captures from a virtual cable's recording side instead of
# real hardware). Best-effort; only wired in when --mic-select-device is
# explicitly configured, so it is a no-op with zero risk when unset.
SELECT_MIC_DEVICE_JS = r"""
(() => {
  const NEEDLE = %s;
  const opener = [...document.querySelectorAll('button, [role="button"]')].find((b) =>
    /audio settings/i.test(b.getAttribute("aria-label") || ""));
  const micList = document.querySelector('select[aria-label*="microphone" i], [role="menu"][aria-label*="microphone" i]');
  if (!micList) {
    if (opener) { opener.click(); return "opened-audio-settings"; }
    return "no-audio-settings-control";
  }
  const options = [...micList.querySelectorAll('option, [role="menuitemradio"], [role="option"]')];
  const match = options.find((o) => (o.textContent || "").toLowerCase().includes(NEEDLE));
  if (!match) return "no-matching-mic-option";
  if (match.tagName === "OPTION") {
    micList.value = match.value;
    micList.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    match.click();
  }
  return "mic-selected:" + NEEDLE;
})()
"""
