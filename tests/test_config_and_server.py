"""Configuration validation, the writable-state contract, and startup reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from ws_collab.config import Config, role_at_least
from ws_collab.errors import ConfigurationError
from ws_collab.server import build_startup_report
from ws_collab.service import WsCollabService
from conftest import make_config


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    base = {"WS_COLLAB_STATE_DIR": str(tmp_path / "state"), "WS_COLLAB_ADMIN_TOKEN": "tok"}
    base.update(overrides)
    return base


# ------------------------------------------------------------------ defaults
def test_defaults_are_loopback_and_auth_optional(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    assert config.is_loopback_only is True
    assert config.auth_disabled is True, "authentication is off by default for loopback"
    assert config.admin_remote is False, "administration defaults to loopback"


def test_a_token_always_exists_when_auth_required(tmp_path) -> None:
    config = Config.from_env({
        "WS_COLLAB_STATE_DIR": str(tmp_path / "state"),
        "WS_COLLAB_REQUIRE_AUTH": "1",
    })
    assert config.tokens and config.generated_admin_token
    assert any("generated" in w for w in config.warnings), "the operator must be told"


def test_generated_token_is_written_to_the_state_directory_not_logged(tmp_path) -> None:
    config = Config.from_env({
        "WS_COLLAB_STATE_DIR": str(tmp_path / "state"),
        "WS_COLLAB_REQUIRE_AUTH": "1",
    })
    config.prepare_state_dir()
    written = config.generated_token_path.read_text(encoding="utf-8").strip()
    assert written == config.generated_admin_token
    assert config.generated_admin_token not in "\n".join(config.warnings), "never print the secret"


# ------------------------------------------------------- writable state root
def test_all_writable_paths_live_under_one_directory(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    root = config.state_dir.resolve()
    for path in (config.jsonl_dir, config.cursors_dir, config.sessions_dir,
                 config.prompt_path.parent, config.generated_token_path.parent):
        resolved = Path(path).resolve()
        assert resolved == root or root in resolved.parents, f"{path} escapes the writable root"


def test_prepare_creates_the_writable_tree(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    config.prepare_state_dir()
    assert config.state_dir.is_dir() and config.cursors_dir.is_dir() and config.sessions_dir.is_dir()


def test_field_cache_separates_mailbox_definitions_from_chat_bubbles(service: WsCollabService) -> None:
    directory = service.list_mailboxes()["mailboxes"]
    mailbox_id = next(item["id"] for item in directory if item["id"] == "conversation")
    service._remember_field_values(
        mailbox_id,
        [{"from": "operator", "type": "CONVERSATION_MESSAGE", "raw": {"source_kind": "agent"}}],
        observation="chat_bubble",
    )

    definitions = service.field_values(mailbox_id, observation="mailbox_definition")
    bubbles = service.field_values(mailbox_id, observation="chat_bubble")

    assert definitions["observation"] == "mailbox_definition"
    assert {"kind", "source", "origin", "writable"} <= definitions["fields"].keys()
    assert "from" not in definitions["fields"]
    assert bubbles["observation"] == "chat_bubble"
    assert {"from", "type", "source_kind"} <= bubbles["fields"].keys()
    assert "purpose" not in bubbles["fields"]

    data = service._cache_data_doc()
    assert data["schema_version"] == 2
    assert set(data["observations"]) == {"chat_bubble", "mailbox_definition"}
    assert set(service._cache_config_doc()["observations"]) == {"chat_bubble", "mailbox_definition"}


def test_mailbox_directory_uses_durable_personal_cursor(service: WsCollabService) -> None:
    service.mailbox_send(to="conversation", text="first", sender="operator")
    service.mailbox_send(to="conversation", text="second", sender="operator")

    before = next(item for item in service.list_mailboxes("reader")["mailboxes"] if item["id"] == "conversation")
    assert before["unread"] == 2
    assert before["cursorOffset"] == 0

    moved = service.mailbox_cursor_move("conversation", "reader", "now")
    assert moved["initialized"] is True
    assert moved["behind"] == 0
    assert moved["last_read_id"]

    after = next(item for item in service.list_mailboxes("reader")["mailboxes"] if item["id"] == "conversation")
    assert after["unread"] == 0
    assert after["lastReadMessageId"] == moved["last_read_id"]

    cleared = service.mailbox_cursor_clear("conversation", "reader")
    assert cleared["initialized"] is False
    assert cleared["behind"] == 2


def test_state_directory_is_relocatable(tmp_path) -> None:
    elsewhere = tmp_path / "somewhere-else"
    config = Config.from_env(_env(tmp_path, WS_COLLAB_STATE_DIR=str(elsewhere)))
    config.prepare_state_dir()
    assert elsewhere.is_dir() and config.jsonl_dir == elsewhere


# ---------------------------------------------------------------- validation
def test_https_without_certificates_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(tmp_path, WS_COLLAB_HTTPS_PORT="8803"))


def test_missing_certificate_file_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(
            tmp_path,
            WS_COLLAB_HTTPS_PORT="8803",
            WS_COLLAB_TLS_CERT_FILE=str(tmp_path / "absent.pem"),
            WS_COLLAB_TLS_KEY_FILE=str(tmp_path / "absent.key"),
        ))


def test_non_loopback_without_tls_is_refused_by_default(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(tmp_path, WS_COLLAB_HOST="0.0.0.0"))


def test_development_exception_must_be_explicit_and_warns_loudly(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path, WS_COLLAB_HOST="0.0.0.0", WS_COLLAB_DEV_INSECURE="1"))
    joined = " ".join(config.warnings).lower()
    assert "insecure" in joined and "all interfaces" in joined


def test_remote_admin_without_tls_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(tmp_path, WS_COLLAB_ADMIN_REMOTE="1"))


def test_invalid_echo_policy_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(tmp_path, WS_COLLAB_ECHO_POLICY="just-guess"))


def test_invalid_role_in_token_list_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        Config.from_env(_env(tmp_path, WS_COLLAB_TOKENS="abc=wizard"))


def test_fewer_than_three_engines_is_warned(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path, WS_COLLAB_STT_ENGINES="only_one"))
    assert any("three" in w for w in config.warnings)


# --------------------------------------------------------------------- roles
def test_role_hierarchy_ordering() -> None:
    assert role_at_least("admin", "viewer") and role_at_least("operator", "worker")
    assert not role_at_least("viewer", "worker")
    assert not role_at_least("nonsense", "viewer")


# -------------------------------------------------------------------- agents
def test_agents_are_collected_in_declared_order(tmp_path) -> None:
    config = Config.from_env(_env(
        tmp_path,
        WS_COLLAB_AGENT_2="WS_COLLAB_AGENT_second",
        WS_COLLAB_AGENT_1="WS_COLLAB_AGENT_first",
    ))
    assert config.agents == ["WS_COLLAB_AGENT_first", "WS_COLLAB_AGENT_second"]


# ----------------------------------------------------------- startup report
def test_report_lists_every_transport_url(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    report = build_startup_report(config, [{"host": "127.0.0.1", "port": 8802, "scheme": "http"}], [])
    assert "http://127.0.0.1:8802/ws_collab/v1" in report
    assert "ws://127.0.0.1:8802/ws_collab/ws" in report
    assert "/ws_collab/admin" in report


def test_report_states_tls_and_authentication_status(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    report = build_startup_report(config, [{"host": "127.0.0.1", "port": 8802, "scheme": "http"}], [])
    assert "TLS:" in report and "authentication" in report


def test_report_lists_failed_bindings(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    report = build_startup_report(
        config,
        [{"host": "127.0.0.1", "port": 8802, "scheme": "http"}],
        [{"host": "10.0.0.5", "port": 8802, "error": "address unavailable"}],
    )
    assert "FAILED binding 10.0.0.5:8802" in report


def test_report_warns_prominently_when_exposed(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path, WS_COLLAB_HOST="0.0.0.0", WS_COLLAB_DEV_INSECURE="1"))
    report = build_startup_report(config, [{"host": "0.0.0.0", "port": 8802, "scheme": "http"}], [])
    assert "WARNING" in report and "ALL interfaces" in report


def test_report_never_prints_a_secret(tmp_path) -> None:
    config = Config.from_env({
        "WS_COLLAB_STATE_DIR": str(tmp_path / "state"),
        "WS_COLLAB_REQUIRE_AUTH": "1",
    })
    report = build_startup_report(config, [{"host": "127.0.0.1", "port": 8802, "scheme": "http"}], [])
    assert config.generated_admin_token not in report
    assert str(config.generated_token_path) in report, "the operator is told where to find it"


def test_report_is_only_built_for_sockets_that_bound(tmp_path) -> None:
    config = Config.from_env(_env(tmp_path))
    report = build_startup_report(config, [], [{"host": "127.0.0.1", "port": 8802, "error": "in use"}])
    assert "bound   http" not in report, "never claim a binding that did not happen"


# ------------------------------------------------------------ service config
def test_public_config_excludes_secrets(service) -> None:
    public = service.get_config_public()
    serialised = str(public)
    assert "test-admin-token" not in serialised and "session_secret" not in public


def test_capabilities_describe_the_running_system(service) -> None:
    caps = service.capabilities()
    assert caps["rest_base"] == "/ws_collab" and caps["versioned_base"] == "/ws_collab/v1"
    assert caps["features"]["three_stt_engines"] >= 3
    assert caps["streams"] and caps["stream_roles"]


def test_boot_id_identifies_the_process_and_changes_on_restart(config, event_store) -> None:
    # The admin UI records this id at load and reloads itself the first time a
    # (re)connect or poll reports a different one, so pages left open through a
    # restart swap in the freshly hosted frame. Contract the client relies on:
    # one id per process, reported identically on every surface it polls, and a
    # different id after a restart (a fresh service instance).
    service = WsCollabService(config, event_store)
    boot_id = service.capabilities()["boot_id"]
    assert boot_id
    assert service.status()["boot_id"] == boot_id
    assert service.diagnostics()["boot_id"] == boot_id

    restarted = WsCollabService(config, event_store)
    assert restarted.capabilities()["boot_id"] != boot_id


def test_diagnostics_report_stream_and_transport_health(service) -> None:
    diagnostics = service.diagnostics()
    assert diagnostics["streams"] and "broker" in diagnostics
    assert "capture" in diagnostics and "tts" in diagnostics
