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
CAPTIONS_JS = r"""
(() => {
  const labelSel = 'div[aria-label*="aption" i], div[role="region"][aria-label*="ubtitle" i], div[role="region"][aria-label*="aption" i]';
  const candidates = [...document.querySelectorAll(labelSel)];
  const liveOnes = candidates.filter((c) => c.hasAttribute("aria-live") || c.closest("[aria-live]"));
  let region = null, bestLen = -1;
  const pool = liveOnes.length ? liveOnes : candidates;
  for (const c of pool) {
    const len = (c.innerText || "").length;
    if (len > bestLen) { region = c; bestLen = len; }
  }
  if (!region) {
    const inCall = !!document.querySelector('button[aria-label*="captions" i], [data-is-muted]');
    return JSON.stringify({ ok: false, note: inCall ? "captions look OFF - press c in the Meet" : "not in a call yet?" });
  }
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
  const liveKeys = [];
  rowEls.forEach((rowEl) => {
    let info = seen.get(rowEl);
    if (!info) {
      info = { key: `row-${seen.size}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}` };
      seen.set(rowEl, info);
    }
    liveKeys.push(info.key);
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
  return JSON.stringify({ ok: true, rows, liveKeys, note: fallbackNote });
})()
"""


# Unattended join: on the pre-join screen turn the camera OFF, set the mic,
# and click Join; once in the call, auto-admit knockers, answer Google's
# solo-meeting "Are you still there?" check, and keep captions switched on.
# mic policy: "keep"     -- never touch the mic (the host: manual mutes rule)
#             "muted"    -- companion at rest: force/keep it muted
#             "speaking" -- companion during SAPI playback: ensure unmuted
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
