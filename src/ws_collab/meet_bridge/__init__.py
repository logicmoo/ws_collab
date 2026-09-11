"""Google Meet caption bridge, native to ws_collab.

Drives a real Chrome tab over the DevTools Protocol (CDP), polls Meet's
live-caption DOM region, and relays finished caption lines into ws_collab's
durable conversation context. Meet captions are a meeting/chat resource, not a
local microphone STT engine.

This subpackage is a from-scratch reimplementation of the design that used to
live as a standalone script (``scripts/meet_caption_bridge.py``) in the outer
workbench monorepo, ported to be fully self-contained inside ws_collab: no
import of a sibling plugin's mailbox client, and talking to ws_collab's own
REST API instead. See ``docs/GOOGLE_MEET_BRIDGE.md`` for the full design,
setup, and the HOST+COMPANION two-bot rationale.

Submodules:

* :mod:`.tracker`       -- ``CaptionTracker``, the sentence-boundary caption
  buffering logic (thoroughly unit tested; see ``tests/test_meet_bridge_
  tracker.py``).
* :mod:`.cdp`            -- minimal Chrome DevTools Protocol client (no
  Playwright/Selenium) plus browser discovery/launch helpers.
* :mod:`.scripts_js`     -- the JavaScript snippets injected into the Meet tab
  (caption scraping, autojoin, synthetic mic, chat posting).
* :mod:`.audio_out`      -- Windows SAPI text-to-speech and virtual-cable audio
  playback helpers for the ``/say`` command.
* :mod:`.mailbox_client` -- a tiny native HTTP client for ws_collab's own
  ``/ws_collab/mailbox`` REST API (replaces the old cross-plugin mailbox import).
* :mod:`.bridge`         -- orchestration: CLI, the companion/out/poll loops,
  and the bridge's own status HTTP server.
"""

from __future__ import annotations
