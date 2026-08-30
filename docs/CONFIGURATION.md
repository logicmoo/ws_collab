# WS_COLLAB configuration

All settings are environment variables prefixed `WS_COLLAB_`. Nothing is
required: the defaults are loopback-only and authenticated. A full annotated
example lives in [`examples/config.example.env`](../examples/config.example.env).

## Networking

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_COLLAB_HOST` | `127.0.0.1` | Bind address |
| `WS_COLLAB_HTTP_PORT` | `8802` | HTTP port |
| `WS_COLLAB_HTTPS_PORT` | _(off)_ | HTTPS port; requires both TLS files |
| `WS_COLLAB_TLS_CERT_FILE` | — | Certificate path (validated at startup) |
| `WS_COLLAB_TLS_KEY_FILE` | — | Private key path (validated at startup) |
| `WS_COLLAB_BIND_ADDRESSES` | _(host)_ | Comma-separated restricted bind list |
| `WS_COLLAB_ADMIN_REMOTE` | `0` | Allow non-loopback admin access (requires TLS) |

Binding rules enforced at startup:

* Non-loopback binding **requires TLS** unless `WS_COLLAB_DEV_INSECURE=1`, which
  prints a prominent warning.
* Remote administration requires TLS.
* Missing or unreadable TLS files are a hard error.
* The startup report is printed only after sockets are actually listening, and
  lists every bound address, transport URL, admin URL, LAN URL, TLS/auth status,
  and any failed binding. Secrets are never printed.

## Writable state

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_COLLAB_STATE_DIR` | `collab_state` | **The only directory needing write access** |
| `WS_COLLAB_JSONL_DIR` | _(state dir)_ | Override just the event streams |
| `WS_COLLAB_ROTATE_MAX_BYTES` | `67108864` | Rotate a stream past this size |
| `WS_COLLAB_RETENTION_MAX_FILES` | `20` | Rotated segments to retain per stream |
| `WS_COLLAB_PROMPT_FILE` | `long_running_prompt.txt` | Worker prompt file |

## Security

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_COLLAB_TOKENS` | _(generated)_ | `token=role` pairs, comma-separated |
| `WS_COLLAB_ADMIN_TOKEN` | — | Shorthand for one admin token |
| `WS_COLLAB_SESSION_SECRET` | _(ephemeral)_ | Signs admin cookies; set it to survive restarts |
| `WS_COLLAB_TRUSTED_ORIGINS` | _(none)_ | Allowed browser origins |
| `WS_COLLAB_ALLOWLIST` | _(none)_ | CIDR client allowlist |
| `WS_COLLAB_REQUIRE_TLS` | `1` | Require TLS for non-loopback production use |
| `WS_COLLAB_DEV_INSECURE` | `0` | Explicit development exception (warns loudly) |
| `WS_COLLAB_RATE_LIMIT_RPS` | `50` | Per-client request rate |
| `WS_COLLAB_MAX_BODY_BYTES` | `1048576` | Maximum request body |
| `WS_COLLAB_MAX_WS_MESSAGE_BYTES` | `1048576` | Maximum WebSocket message |
| `WS_COLLAB_MAX_CONNECTIONS` | `256` | Concurrent WebSocket connections |

Roles are `viewer` < `worker` < `operator` < `admin`. If no token is configured a
random admin token is generated and written to
`collab_state/generated_admin_token.txt` (mode `600` where the OS supports it).
There is no authentication bypass, in any mode, including tests.

## Workers

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_COLLAB_WORKER_WARN_SECONDS` | `60` | Silence before `warn` |
| `WS_COLLAB_WORKER_OVERDUE_SECONDS` | `120` | Silence before `overdue` |
| `WS_COLLAB_WORKER_UNRESPONSIVE_SECONDS` | `300` | Silence before `unresponsive` |
| `WS_COLLAB_AGENT_<n>` | — | Enumerated agent identities |

## Audio, STT, TTS

See [AUDIO.md](AUDIO.md) for the full treatment.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WS_COLLAB_AUDIO_ENABLED` | `0` | Always-listening capture must be explicitly enabled |
| `WS_COLLAB_AUDIO_BACKEND` | `auto` | `auto` (real, falling back), `sounddevice`, or `fake` |
| `WS_COLLAB_AUDIO_INPUT_DEVICE` | _(default input)_ | Stable device id |
| `WS_COLLAB_ECHO_POLICY` | `listen_and_filter_tts` | Echo handling policy |
| `WS_COLLAB_STT_ENGINES` | `whisper:tiny.en,whisper:base.en,vosk` | Comma-separated engine names |
| `WS_COLLAB_STT_TIMEOUT_MS` | `120000` | Per-engine timeout (a real model may load on first use) |
| `WS_COLLAB_STT_CONCURRENCY` | `3` | Bounded concurrency |
| `WS_COLLAB_STT_ALLOW_REMOTE` | `0` | Required before any audio leaves the device |
| `WS_COLLAB_VOSK_MODEL` | _(auto-discovered)_ | Vosk model directory |
| `WS_COLLAB_DISAMBIGUATOR` | `deterministic` | `deterministic` or `llm` |
| `WS_COLLAB_DISAMBIGUATOR_ALLOW_REMOTE` | `0` | Required for a remote LLM resolver |
| `WS_COLLAB_DISAMBIGUATOR_LLM_ENDPOINT` | — | Strict-schema resolver endpoint |
| `WS_COLLAB_TTS_BACKEND` | `auto` | `auto` (real, falling back), `sapi`, or `fake` |
| `WS_COLLAB_TTS_POLICY` | `unique_when_possible` | Voice assignment policy |
| `WS_COLLAB_TTS_OUTPUT_DESTINATION` | `local` | Safe default for legacy speech; set `companion` to require active Meet companion routing |
| `WS_COLLAB_COMPANION_AUDIO_QUEUE_MAX` | `8` | Pending speech/interject limit (1–100); overflow is rejected and counted |

Install the real audio stack with:

```bash
pip install sounddevice soundfile faster-whisper vosk pywin32   # pywin32: Windows only
```

Everything runs without them; each layer falls back to a hardware-free double and
reports why in `capabilities.warnings`.

## Deployment profiles

### Loopback development

```bash
export WS_COLLAB_ADMIN_TOKEN=dev-token
export WS_COLLAB_STATE_DIR=./collab_state
python -m ws_collab.server 127.0.0.1 8802
```

### Restricted LAN (still authenticated, explicitly insecure)

```bash
export WS_COLLAB_BIND_ADDRESSES=10.0.0.5
export WS_COLLAB_ALLOWLIST=10.0.0.0/24
export WS_COLLAB_TOKENS='ops-token=operator,worker-token=worker'
export WS_COLLAB_DEV_INSECURE=1      # acknowledged: no TLS on this segment
python -m ws_collab.server
```

### Secure remote (HTTPS + WSS + remote admin)

```bash
export WS_COLLAB_BIND_ADDRESSES=0.0.0.0
export WS_COLLAB_HTTPS_PORT=8803
export WS_COLLAB_TLS_CERT_FILE=/etc/ws_collab/fullchain.pem
export WS_COLLAB_TLS_KEY_FILE=/etc/ws_collab/privkey.pem
export WS_COLLAB_TRUSTED_ORIGINS=https://ops.example.com
export WS_COLLAB_ALLOWLIST=203.0.113.0/24
export WS_COLLAB_ADMIN_REMOTE=1
export WS_COLLAB_SESSION_SECRET="$(openssl rand -hex 32)"
export WS_COLLAB_TOKENS="$(cat /run/secrets/ws_collab_tokens)"
python -m ws_collab.server
```

Never commit tokens, keys, recordings, or transcripts. `collab_state/` is
git-ignored for exactly this reason.

## Running as a workbench plugin

`plugin.json` declares `entrypoint: plugin.py` and `routePrefix: /ws_collab`. The
host imports `create_router(manifest)`; configuration still comes from the same
`WS_COLLAB_*` environment variables, and the service starts lazily on the first
request.
