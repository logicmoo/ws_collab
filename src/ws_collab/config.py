"""Configuration for WS_COLLAB, sourced from ``WS_COLLAB_*`` environment vars.

The configuration is deliberately explicit and self-validating. It never invents
an authentication bypass: when no tokens are configured a random administrator
token is generated so the service is still protected, and the token is written to
a restricted file (its path is printed, the secret itself is not).
"""

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .ids import new_token

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}

ROLE_ORDER = ["viewer", "worker", "operator", "admin"]

# How captured audio is reconciled against the system's own TTS output.
ECHO_POLICIES = {
    "mute_input_during_tts",
    "listen_and_filter_tts",
    "listen_and_measure_tts_accuracy",
    "full_duplex_with_echo_cancellation",
}


def _plugin_env_variables() -> dict[str, str]:
    """The ``"env:variables"`` map declared in the sibling ``plugin.json``.

    Lets the plugin manifest set environment variables (e.g.
    ``WS_COLLAB_AUDIO_ENABLED``) for the server without touching the OS
    environment. Best-effort: a missing or malformed manifest yields ``{}``.
    """

    try:
        manifest_path = Path(__file__).resolve().parent.parent / "plugin.json"
        data = json.loads(manifest_path.read_text("utf-8"))
    except Exception:
        return {}
    raw = data.get("env:variables") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ConfigurationError(f"invalid integer value: {value!r}") from error


def _parse_virtual_mailboxes(raw: str | None, default: list[dict[str, str]]) -> list[dict[str, str]]:
    """Parse WS_COLLAB_VIRTUAL_MAILBOXES: a JSON list of {source, mailbox, purpose?}."""
    if not raw or not raw.strip():
        return default
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ConfigurationError(f"invalid VIRTUAL_MAILBOXES JSON: {error}") from error
    if not isinstance(data, list):
        raise ConfigurationError("VIRTUAL_MAILBOXES must be a JSON list")
    result: list[dict[str, str]] = []
    for item in data:
        if isinstance(item, dict) and item.get("mailbox") and item.get("source"):
            result.append({
                "source": str(item["source"]),
                "mailbox": str(item["mailbox"]),
                "purpose": str(item.get("purpose", "")),
            })
    return result or default


def _as_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_tokens(raw: str | None) -> dict[str, dict[str, str]]:
    """Parse ``token=role`` pairs into a token -> descriptor mapping."""

    tokens: dict[str, dict[str, str]] = {}
    for index, entry in enumerate(_as_list(raw)):
        if "=" in entry:
            token, role = entry.split("=", 1)
        else:
            token, role = entry, "operator"
        token = token.strip()
        role = role.strip().lower()
        if role not in ROLE_ORDER:
            raise ConfigurationError(
                f"invalid role {role!r}; expected one of {ROLE_ORDER}"
            )
        tokens[token] = {"role": role, "label": f"token-{index + 1}"}
    return tokens


@dataclass
class Config:
    """Validated runtime configuration."""

    # Networking / binding
    host: str = "127.0.0.1"
    http_port: int = 8802
    https_port: int = 0
    tls_cert_file: str = ""
    tls_key_file: str = ""
    bind_addresses: list[str] = field(default_factory=list)
    admin_host: str = "127.0.0.1"
    admin_remote: bool = False

    # Storage
    # Storage -- ``state_dir`` is the single directory that requires write access.
    # Everything writable (JSONL streams, cursors, prompt file + history, audit,
    # generated secrets, sessions) lives beneath it so the rest of the deployment
    # can be mounted read-only.
    state_dir: Path = field(default_factory=lambda: Path("collab_state"))
    jsonl_dir: Path = field(default_factory=lambda: Path("collab_state"))
    rotate_max_bytes: int = 64 * 1024 * 1024
    retention_max_files: int = 20
    prompt_file: str = "long_running_prompt.txt"

    # Security
    tokens: dict[str, dict[str, str]] = field(default_factory=dict)
    session_secret: str = ""
    trusted_origins: list[str] = field(default_factory=list)
    network_allowlist: list[str] = field(default_factory=list)
    require_tls: bool = True
    dev_insecure: bool = False
    # Authentication is optional. It defaults to OFF so local (loopback) use needs
    # no token; validate() auto-enables it when the server is exposed beyond
    # loopback (unless DEV_INSECURE opts out). Override with WS_COLLAB_AUTH_DISABLED
    # or WS_COLLAB_REQUIRE_AUTH.
    auth_disabled: bool = True
    rate_limit_rps: int = 50
    max_body_bytes: int = 1 * 1024 * 1024
    max_ws_message_bytes: int = 1 * 1024 * 1024
    max_connections: int = 256

    # Workers
    worker_warn_seconds: int = 60
    worker_overdue_seconds: int = 120
    worker_unresponsive_seconds: int = 300

    # Audio
    audio_enabled: bool = False
    audio_backend: str = "auto"
    audio_input_device: str = ""
    vad_threshold: float = 0.02
    vad_silence_ms: int = 600
    echo_policy: str = "listen_and_filter_tts"

    # STT. Real engines are preferred when their libraries/models are installed;
    # each falls back to a deterministic double (with a reported warning) so the
    # three-hypothesis pipeline always runs.
    stt_engines: list[str] = field(
        default_factory=lambda: ["whisper:tiny.en", "whisper:base.en", "vosk"]
    )
    stt_timeout_ms: int = 120000
    stt_concurrency: int = 3
    stt_allow_remote: bool = False

    # Disambiguator
    disambiguator: str = "deterministic"
    disambiguator_llm_endpoint: str = ""
    disambiguator_allow_remote: bool = False

    # TTS
    tts_backend: str = "auto"
    tts_policy: str = "unique_when_possible"

    # Agents
    agents: list[str] = field(default_factory=list)

    # Federation: a globally-unique prefix prepended to this server's local
    # mailbox names to form their global names (e.g. "ws_collab/conversation").
    global_name: str = "ws_collab"

    # Virtual (emulated) read-only mailboxes: each projects a source (a disk JSON
    # file, or a `self:`/http endpoint) as a mailbox. Configure via
    # WS_COLLAB_VIRTUAL_MAILBOXES (a JSON list of {source, mailbox, purpose?}).
    virtual_mailboxes: list[dict[str, str]] = field(default_factory=lambda: [
        {
            "source": "self:mailbox/agents",
            "mailbox": "server-agents",
            "purpose": "Agents/users directory, emulated as a read-only stream.",
        }
    ])

    # Derived / diagnostic
    warnings: list[str] = field(default_factory=list)
    generated_admin_token: str = ""

    # ------------------------------------------------------------------ helpers
    @property
    def https_enabled(self) -> bool:
        return bool(self.https_port and self.tls_cert_file and self.tls_key_file)

    @property
    def is_loopback_only(self) -> bool:
        loopback = {"127.0.0.1", "::1", "localhost"}
        addresses = set(self.bind_addresses or [self.host])
        return addresses.issubset(loopback)

    @property
    def exposed_all_interfaces(self) -> bool:
        return any(addr in {"0.0.0.0", "::"} for addr in (self.bind_addresses or [self.host]))

    def role_for_token(self, token: str) -> str | None:
        descriptor = self.tokens.get(token)
        return descriptor["role"] if descriptor else None

    # -------------------------------------------------------------- writable root
    @property
    def prompt_path(self) -> Path:
        candidate = Path(self.prompt_file)
        return candidate if candidate.is_absolute() else self.state_dir / candidate

    @property
    def cursors_dir(self) -> Path:
        return self.state_dir / "cursors"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def generated_token_path(self) -> Path:
        return self.state_dir / "generated_admin_token.txt"

    def prepare_state_dir(self) -> Path:
        """Create the single writable directory tree and persist generated secrets.

        This is the only location WS_COLLAB needs write access to. The generated
        administrator token (if any) is written here with restricted permissions
        instead of being printed, so operators can retrieve it without exposing it
        in logs.
        """

        for directory in (self.state_dir, self.jsonl_dir, self.cursors_dir, self.sessions_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.generated_admin_token:
            path = self.generated_token_path
            path.write_text(self.generated_admin_token + "\n", encoding="utf-8")
            try:  # best-effort hardening; POSIX honours it, Windows ignores it
                os.chmod(path, 0o600)
            except OSError:
                pass
        return self.state_dir

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        use_manifest = env is None
        env = dict(os.environ if env is None else env)
        # Apply plugin.json "env:variables" as defaults so the manifest can enable
        # features (e.g. audio) without OS env edits. Real env still wins; skipped
        # when an explicit env dict is passed (keeps tests hermetic).
        if use_manifest:
            for _name, _value in _plugin_env_variables().items():
                env.setdefault(_name, _value)

        def get(name: str) -> str | None:
            return env.get(f"WS_COLLAB_{name}")

        cfg = cls()
        cfg.host = get("HOST") or cfg.host
        cfg.http_port = _as_int(get("HTTP_PORT"), cfg.http_port)
        cfg.https_port = _as_int(get("HTTPS_PORT"), cfg.https_port)
        cfg.tls_cert_file = get("TLS_CERT_FILE") or ""
        cfg.tls_key_file = get("TLS_KEY_FILE") or ""
        cfg.bind_addresses = _as_list(get("BIND_ADDRESSES"))
        cfg.admin_host = get("ADMIN_HOST") or cfg.admin_host
        cfg.admin_remote = _as_bool(get("ADMIN_REMOTE"), cfg.admin_remote)

        jsonl_dir = get("JSONL_DIR")
        state_dir = get("STATE_DIR")
        if state_dir:
            cfg.state_dir = Path(state_dir)
        # The JSONL streams default to living directly inside the single writable
        # state directory; an explicit JSONL_DIR override is still honoured.
        cfg.jsonl_dir = Path(jsonl_dir) if jsonl_dir else cfg.state_dir
        cfg.rotate_max_bytes = _as_int(get("ROTATE_MAX_BYTES"), cfg.rotate_max_bytes)
        cfg.retention_max_files = _as_int(get("RETENTION_MAX_FILES"), cfg.retention_max_files)
        cfg.prompt_file = get("PROMPT_FILE") or cfg.prompt_file

        cfg.tokens = _parse_tokens(get("TOKENS"))
        admin_token = get("ADMIN_TOKEN")
        if admin_token:
            cfg.tokens[admin_token] = {"role": "admin", "label": "admin"}
        cfg.session_secret = get("SESSION_SECRET") or ""
        cfg.trusted_origins = _as_list(get("TRUSTED_ORIGINS"))
        cfg.network_allowlist = _as_list(get("ALLOWLIST"))
        cfg.require_tls = _as_bool(get("REQUIRE_TLS"), cfg.require_tls)
        cfg.dev_insecure = _as_bool(get("DEV_INSECURE"), cfg.dev_insecure)
        cfg.auth_disabled = _as_bool(get("AUTH_DISABLED"), cfg.auth_disabled)
        require_auth = get("REQUIRE_AUTH")
        if require_auth is not None:
            cfg.auth_disabled = not _as_bool(require_auth, True)
        cfg.rate_limit_rps = _as_int(get("RATE_LIMIT_RPS"), cfg.rate_limit_rps)
        cfg.max_body_bytes = _as_int(get("MAX_BODY_BYTES"), cfg.max_body_bytes)
        cfg.max_ws_message_bytes = _as_int(get("MAX_WS_MESSAGE_BYTES"), cfg.max_ws_message_bytes)
        cfg.max_connections = _as_int(get("MAX_CONNECTIONS"), cfg.max_connections)

        cfg.worker_warn_seconds = _as_int(get("WORKER_WARN_SECONDS"), cfg.worker_warn_seconds)
        cfg.worker_overdue_seconds = _as_int(get("WORKER_OVERDUE_SECONDS"), cfg.worker_overdue_seconds)
        cfg.worker_unresponsive_seconds = _as_int(
            get("WORKER_UNRESPONSIVE_SECONDS"), cfg.worker_unresponsive_seconds
        )

        cfg.audio_enabled = _as_bool(get("AUDIO_ENABLED"), cfg.audio_enabled)
        cfg.audio_backend = get("AUDIO_BACKEND") or cfg.audio_backend
        cfg.audio_input_device = get("AUDIO_INPUT_DEVICE") or ""
        cfg.echo_policy = get("ECHO_POLICY") or cfg.echo_policy

        stt_engines = _as_list(get("STT_ENGINES"))
        if stt_engines:
            cfg.stt_engines = stt_engines
        cfg.stt_timeout_ms = _as_int(get("STT_TIMEOUT_MS"), cfg.stt_timeout_ms)
        cfg.stt_concurrency = _as_int(get("STT_CONCURRENCY"), cfg.stt_concurrency)
        cfg.stt_allow_remote = _as_bool(get("STT_ALLOW_REMOTE"), cfg.stt_allow_remote)

        cfg.disambiguator = get("DISAMBIGUATOR") or cfg.disambiguator
        cfg.disambiguator_llm_endpoint = get("DISAMBIGUATOR_LLM_ENDPOINT") or ""
        cfg.disambiguator_allow_remote = _as_bool(
            get("DISAMBIGUATOR_ALLOW_REMOTE"), cfg.disambiguator_allow_remote
        )

        cfg.tts_backend = get("TTS_BACKEND") or cfg.tts_backend
        cfg.tts_policy = get("TTS_POLICY") or cfg.tts_policy

        cfg.agents = _collect_agents(env)
        cfg.global_name = get("GLOBAL_NAME") if get("GLOBAL_NAME") is not None else cfg.global_name
        cfg.virtual_mailboxes = _parse_virtual_mailboxes(get("VIRTUAL_MAILBOXES"), cfg.virtual_mailboxes)

        cfg.validate()
        return cfg

    # --------------------------------------------------------------- validation
    def validate(self) -> "Config":
        self.warnings = []

        if self.https_port and not (self.tls_cert_file and self.tls_key_file):
            raise ConfigurationError(
                "HTTPS port configured without WS_COLLAB_TLS_CERT_FILE/WS_COLLAB_TLS_KEY_FILE"
            )
        for label, path in (("cert", self.tls_cert_file), ("key", self.tls_key_file)):
            if path and not Path(path).is_file():
                raise ConfigurationError(f"TLS {label} file not found: {path}")

        if self.echo_policy not in ECHO_POLICIES:
            raise ConfigurationError(f"invalid echo policy: {self.echo_policy!r}")

        if self.disambiguator not in {"deterministic", "llm"}:
            raise ConfigurationError(f"invalid disambiguator: {self.disambiguator!r}")

        if len(self.stt_engines) < 3:
            self.warnings.append(
                "fewer than three STT engines configured; parity of three independent "
                "hypotheses is not guaranteed"
            )

        # Authentication policy. Disabled by default so local (loopback) use needs
        # no credentials; auto-enabled when the server is exposed beyond loopback
        # unless DEV_INSECURE explicitly keeps it off.
        if self.auth_disabled and not self.is_loopback_only and not self.dev_insecure:
            self.auth_disabled = False
            self.warnings.append(
                "authentication auto-enabled: binding is not loopback-only "
                "(set WS_COLLAB_DEV_INSECURE=1 to keep authentication off on an exposed bind)"
            )
        if self.auth_disabled:
            self.warnings.append(
                "AUTHENTICATION DISABLED: every request is treated as a local admin. "
                "Set WS_COLLAB_REQUIRE_AUTH=1 (or WS_COLLAB_AUTH_DISABLED=0) to require tokens."
            )

        if not self.tokens and not self.auth_disabled:
            token = new_token()
            self.tokens[token] = {"role": "admin", "label": "generated-admin"}
            self.generated_admin_token = token
            self.warnings.append(
                "no tokens configured; generated a random administrator token "
                "(written to collab_state/generated_admin_token.txt, not printed)"
            )

        if not self.session_secret:
            self.session_secret = new_token(32)
            self.warnings.append(
                "no WS_COLLAB_SESSION_SECRET configured; using an ephemeral secret "
                "(cookie sessions will not survive a restart)"
            )

        non_loopback = not self.is_loopback_only
        if non_loopback and not self.https_enabled:
            if not self.dev_insecure:
                raise ConfigurationError(
                    "non-loopback binding requires TLS; set WS_COLLAB_TLS_* or explicitly "
                    "set WS_COLLAB_DEV_INSECURE=1 for a development exception"
                )
            self.warnings.append(
                "DEVELOPMENT INSECURE MODE: serving non-loopback traffic without TLS"
            )

        if self.exposed_all_interfaces:
            self.warnings.append(
                "server is bound to ALL interfaces; ensure firewalling and authentication"
            )

        if self.admin_remote and not self.https_enabled and not self.dev_insecure:
            raise ConfigurationError(
                "remote administration requires TLS; enable WS_COLLAB_TLS_* or WS_COLLAB_DEV_INSECURE"
            )

        return self


def _collect_agents(env: dict[str, str]) -> list[str]:
    pattern = re.compile(r"^WS_COLLAB_AGENT_(\d+)$")
    found: list[tuple[int, str]] = []
    for key, value in env.items():
        match = pattern.match(key)
        if match and value.strip():
            found.append((int(match.group(1)), value.strip()))
    found.sort(key=lambda item: item[0])
    return [value for _, value in found]


def role_at_least(role: str, required: str) -> bool:
    """Return True when ``role`` is at least as privileged as ``required``."""

    try:
        return ROLE_ORDER.index(role) >= ROLE_ORDER.index(required)
    except ValueError:
        return False
