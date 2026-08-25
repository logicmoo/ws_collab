"""Security: authentication, authorization, CSRF, origins, limits, redaction."""

from __future__ import annotations

import pytest

from ws_collab.errors import (
    AuthenticationError,
    AuthorizationError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationError,
)
from ws_collab.security import Security, safe_join

from conftest import ADMIN_TOKEN, VIEWER_TOKEN, WORKER_TOKEN, make_config


@pytest.fixture
def security(config):
    return Security(config)


# ----------------------------------------------------------------- identity
def test_valid_bearer_token_is_accepted(security) -> None:
    principal = security.authenticate_token(f"Bearer {ADMIN_TOKEN}")
    assert principal is not None and principal.role == "admin"


def test_unknown_token_is_rejected(security) -> None:
    assert security.authenticate_token("Bearer nope") is None


def test_missing_credentials_are_rejected(security) -> None:
    assert security.authenticate_token(None) is None
    assert security.authenticate_token("") is None


def test_no_bypass_when_authentication_is_required(config) -> None:
    """With auth required, even zero configured tokens still demand one."""

    from ws_collab.config import Config

    generated = Config.from_env({
        "WS_COLLAB_STATE_DIR": str(config.state_dir),
        "WS_COLLAB_REQUIRE_AUTH": "1",
    })
    assert generated.tokens, "a token must always exist"
    assert generated.generated_admin_token, "the generated token must be reported to the operator"
    security = Security(generated)
    assert security.authenticate_token("Bearer ") is None
    assert security.authenticate_token(None) is None


def test_auth_can_be_disabled_for_loopback(tmp_path) -> None:
    """Opt-in bypass: with authentication disabled every caller is a local admin."""

    from ws_collab.config import Config

    open_cfg = Config.from_env({
        "WS_COLLAB_STATE_DIR": str(tmp_path / "state"),
        "WS_COLLAB_AUTH_DISABLED": "1",
    })
    assert open_cfg.auth_disabled is True
    security = Security(open_cfg)
    principal = security.authenticate_token(None)
    assert principal is not None and principal.role == "admin"


# ------------------------------------------------------------ authorization
def test_role_hierarchy_grants_higher_roles(security) -> None:
    admin = security.authenticate_token(f"Bearer {ADMIN_TOKEN}")
    Security.require_role(admin, "worker")
    Security.require_role(admin, "operator")


def test_lower_role_cannot_reach_higher_capability(security) -> None:
    viewer = security.authenticate_token(f"Bearer {VIEWER_TOKEN}")
    with pytest.raises(AuthorizationError):
        Security.require_role(viewer, "operator")


def test_anonymous_is_rejected_before_authorization(security) -> None:
    with pytest.raises(AuthenticationError):
        Security.require_role(None, "viewer")


# ----------------------------------------------------------- sessions / CSRF
def test_session_round_trip_and_tamper_detection(security) -> None:
    session = security.create_session("operator", "ops")
    cookie = security.cookie_value(session)
    assert security.authenticate_session(cookie) is not None
    assert security.authenticate_session(cookie[:-3] + "xxx") is None, "signature must be verified"


def test_destroyed_session_stops_working(security) -> None:
    session = security.create_session("operator", "ops")
    cookie = security.cookie_value(session)
    security.destroy_session(session.sid)
    assert security.authenticate_session(cookie) is None


def test_csrf_token_is_required_for_cookie_mutations(security) -> None:
    session = security.create_session("operator", "ops")
    security.verify_csrf(session, session.csrf)
    with pytest.raises(AuthorizationError):
        security.verify_csrf(session, None)
    with pytest.raises(AuthorizationError):
        security.verify_csrf(session, "wrong")


# ---------------------------------------------------------------- origins
def test_untrusted_origin_is_refused_when_a_list_is_configured(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_TRUSTED_ORIGINS="https://ops.example")
    security = Security(config)
    security.check_origin("https://ops.example", required=False)
    with pytest.raises(AuthorizationError):
        security.check_origin("https://evil.example", required=False)


# -------------------------------------------------------------- allowlist
def test_allowlist_blocks_addresses_outside_the_range(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_ALLOWLIST="10.0.0.0/8")
    security = Security(config)
    security.check_allowlist("10.1.2.3")
    with pytest.raises(AuthorizationError):
        security.check_allowlist("192.168.1.5")


def test_admin_defaults_to_loopback_only(security) -> None:
    assert security.is_admin_client_allowed("127.0.0.1") is True
    assert security.is_admin_client_allowed("10.1.1.1") is False


def test_admin_remote_can_be_enabled_deliberately(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_ADMIN_REMOTE="1", WS_COLLAB_DEV_INSECURE="1")
    assert Security(config).is_admin_client_allowed("10.1.1.1") is True


# ------------------------------------------------------------------ limits
def test_rate_limiting_eventually_refuses(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_RATE_LIMIT_RPS="3")
    security = Security(config)
    with pytest.raises(RateLimitError):
        for _ in range(200):
            security.rate_limit("client")


def test_oversized_payloads_are_refused(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_MAX_BODY_BYTES="100", WS_COLLAB_MAX_WS_MESSAGE_BYTES="100")
    security = Security(config)
    security.check_body_size(50)
    with pytest.raises(PayloadTooLargeError):
        security.check_body_size(101)
    with pytest.raises(PayloadTooLargeError):
        security.check_ws_message_size(101)


def test_connection_count_is_capped_and_released(tmp_path) -> None:
    config = make_config(tmp_path, WS_COLLAB_MAX_CONNECTIONS="2")
    security = Security(config)
    security.acquire_connection()
    security.acquire_connection()
    with pytest.raises(RateLimitError):
        security.acquire_connection()
    security.release_connection()
    security.acquire_connection()


# --------------------------------------------------------------- redaction
def test_secrets_are_redacted_from_audit_fields(security) -> None:
    assert security.redact_value("token", "super-secret") == "***redacted***"
    assert security.redact_value("worker_id", "w1") == "w1"


def test_secrets_are_redacted_from_free_text(security) -> None:
    redacted = Security.redact_text("Authorization: Bearer abc123 and api_key=zzz")
    assert "abc123" not in redacted and "zzz" not in redacted


# ------------------------------------------------------------ path safety
def test_path_traversal_is_blocked(tmp_path) -> None:
    (tmp_path / "ok.txt").write_text("fine", encoding="utf-8")
    assert safe_join(tmp_path, "ok.txt").name == "ok.txt"
    with pytest.raises(ValidationError):
        safe_join(tmp_path, "..", "escaped.txt")
