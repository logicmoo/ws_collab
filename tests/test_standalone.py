from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from ws_collab import standalone


def status(boot="boot", verdict="ok"):
    return {
        "status": verdict, "boot_id": boot,
        "subsystems": {"event_store": {"state": "ok"}, "transports": {"state": "ok"}},
    }


@pytest.mark.parametrize("host,expected", [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("::1", "[::1]")])
def test_wildcard_and_ipv6_control_urls(host, expected):
    assert standalone._base_url(host, 8802) == f"http://{expected}:8802/ws_collab"


@pytest.mark.parametrize("host,port", [("example.com/path", 8802), ("user@host", 8802), ("localhost", 0), ("localhost", 65536)])
def test_invalid_target_is_rejected(host, port):
    with pytest.raises(ValueError):
        standalone.start_server(host, port)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeouts_cannot_spawn(timeout):
    with pytest.raises(ValueError):
        standalone.launch(timeout=timeout)


def test_status_distinguishes_refused_connection_from_unreachable_server(monkeypatch):
    read = Mock(side_effect=URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")))
    monkeypatch.setattr(standalone, "_read_json", read)
    assert standalone.get_status() is None
    read.side_effect = URLError(TimeoutError("timed out"))
    with pytest.raises(URLError):
        standalone.get_status()


def test_start_does_not_reuse_an_unrelated_listener(monkeypatch):
    monkeypatch.setattr(standalone, "is_listening", lambda *a, **kw: True)
    monkeypatch.setattr(standalone, "_read_json", lambda *a, **kw: {"status": "ok"})
    popen = Mock()
    monkeypatch.setattr(standalone.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="refusing to control or replace"):
        standalone.launch()
    popen.assert_not_called()


def test_start_reuses_ready_ws_collab_and_rejects_down_server(monkeypatch):
    monkeypatch.setattr(standalone, "is_listening", lambda *a, **kw: True)
    read = Mock(return_value=status())
    monkeypatch.setattr(standalone, "_read_json", read)
    assert standalone.start_server()["status"] == "already-running"
    read.return_value = status(verdict="down")
    with pytest.raises(RuntimeError, match="occupied but not ready"):
        standalone.launch()


def test_launch_uses_canonical_state_detaches_and_closes_parent_log(tmp_path, monkeypatch):
    monkeypatch.delenv("WS_COLLAB_STATE_DIR", raising=False)
    monkeypatch.setattr(standalone, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(standalone, "is_listening", lambda *a, **kw: False)
    statuses = Mock(side_effect=[None, status(verdict="down"), status()])
    monkeypatch.setattr(standalone, "get_status", statuses)
    monkeypatch.setattr(standalone.time, "sleep", lambda _: None)
    proc = Mock(pid=123, poll=Mock(return_value=None))
    popen = Mock(return_value=proc)
    monkeypatch.setattr(standalone.subprocess, "Popen", popen)
    assert standalone.launch(python_executable="chosen-python") is proc
    args, kwargs = popen.call_args
    assert args[0] == ["chosen-python", "-u", "-m", "ws_collab.standalone", "127.0.0.1", "8802"]
    assert kwargs["env"]["WS_COLLAB_STATE_DIR"] == str(tmp_path / "collab_state")
    assert kwargs["env"]["PYTHONPATH"].split(standalone.os.pathsep)[0] == str(standalone._IMPORT_ROOT)
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] is kwargs["stderr"] and kwargs["stdout"].closed
    assert kwargs.get("creationflags") or kwargs.get("start_new_session")
    assert statuses.call_count == 3


def test_launch_reports_child_failure_and_startup_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(standalone, "is_listening", lambda *a, **kw: False)
    proc = Mock(pid=123, poll=Mock(return_value=1), returncode=1)
    monkeypatch.setattr(standalone.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(RuntimeError, match="exited.*code 1"):
        standalone.launch(state_dir=tmp_path)
    proc.poll.return_value = None
    monkeypatch.setattr(standalone.time, "monotonic", Mock(side_effect=[0, 100]))
    with pytest.raises(TimeoutError, match="supervisor pid 123"):
        standalone.launch(state_dir=tmp_path)
    proc.terminate.assert_not_called()


@pytest.mark.parametrize("timeouts", [0, 1, 2])
@pytest.mark.parametrize("repeated_interrupt", [False, True])
def test_supervisor_ctrl_c_waits_then_escalates_only_owned_child(monkeypatch, timeouts, repeated_interrupt):
    child = Mock()
    child.wait.side_effect = [KeyboardInterrupt()] + [
        KeyboardInterrupt() if repeated_interrupt else subprocess.TimeoutExpired("server", 10)
        for _ in range(timeouts)
    ] + [0]
    monkeypatch.setattr(standalone.subprocess, "Popen", Mock(return_value=child))
    assert standalone.main(["127.0.0.1", "8802"]) == 130
    assert child.terminate.call_count == (1 if timeouts else 0)
    assert child.kill.call_count == (1 if timeouts == 2 else 0)


@pytest.mark.parametrize("bad_token", ["private-token\n", "private\rtoken", "private\x00token", "private\u0100token"])
def test_malformed_token_cannot_leak_in_cli_error(monkeypatch, capsys, bad_token):
    monkeypatch.setattr(standalone, "get_status", Mock(return_value=status()))
    monkeypatch.setattr(standalone.os, "environ", {**standalone.os.environ, "WS_COLLAB_TOKEN": bad_token})
    assert standalone.main(["restart"]) == 1
    error = capsys.readouterr().err
    assert "Operator token must be printable ASCII" in error
    assert "private" not in error


def test_control_uses_token_file_and_waits_for_changed_ready_boot(tmp_path, monkeypatch):
    (tmp_path / "generated_admin_token.txt").write_text("test-only-token\n", encoding="utf-8")
    monkeypatch.delenv("WS_COLLAB_TOKEN", raising=False)
    monkeypatch.delenv("WS_COLLAB_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(standalone, "get_status", Mock(side_effect=[
        status("old"), status("old"), None, status("new", "down"), status("new"),
    ]))
    read = Mock(return_value={"action": "restart", "scheduled": True, "boot_id": "old"})
    monkeypatch.setattr(standalone, "_read_json", read)
    monkeypatch.setattr(standalone.time, "sleep", lambda _: None)
    assert standalone.control_server("restart", state_dir=tmp_path)["boot_id"] == "new"
    assert read.call_args.args == ("http://127.0.0.1:8802/ws_collab/admin/restart",)
    assert read.call_args.kwargs["token"] == "test-only-token"
    assert read.call_args.kwargs["post"] is True


def test_shutdown_waits_for_socket_close_not_a_failed_http_request(monkeypatch):
    monkeypatch.setattr(standalone, "get_status", Mock(return_value=status()))
    monkeypatch.setattr(standalone, "_read_json", Mock(return_value={"action": "shutdown", "scheduled": True, "pid": 123}))
    listener = Mock(side_effect=[True, False])
    monkeypatch.setattr(standalone, "is_listening", listener)
    monkeypatch.setattr(standalone.time, "sleep", lambda _: None)
    assert standalone.control_server("shutdown", token="")["status"] == "stopped"
    assert listener.call_count == 2


def test_stopped_and_unacknowledged_controls_are_explicit(monkeypatch):
    monkeypatch.setattr(standalone, "get_status", Mock(return_value=None))
    assert standalone.control_server("shutdown")["status"] == "already-stopped"
    with pytest.raises(RuntimeError, match="use the start"):
        standalone.control_server("restart")
    monkeypatch.setattr(standalone, "get_status", Mock(return_value=status()))
    monkeypatch.setattr(standalone, "_read_json", Mock(return_value={"ok": True}))
    with pytest.raises(RuntimeError, match="did not acknowledge"):
        standalone.control_server("restart", token="")


def test_cli_routes_commands_and_keeps_secrets_out_of_errors(monkeypatch, capsys):
    start = Mock(return_value={"status": "started"})
    monkeypatch.setattr(standalone, "start_server", start)
    assert standalone.main(["start", "--port", "8888"]) == 0
    assert start.call_args.args == ("127.0.0.1", 8888)
    control = Mock(return_value={"status": "restarted"})
    monkeypatch.setattr(standalone, "control_server", control)
    assert standalone.main(["restart", "--port", "8888", "--timeout", "3"]) == 0
    assert control.call_args.args == ("restart", "127.0.0.1", 8888)
    control.side_effect = HTTPError("local", 403, "test-secret-do-not-log", {}, None)
    assert standalone.main(["shutdown"]) == 1
    output = capsys.readouterr()
    assert "403" in output.err and "test-secret" not in output.err
    monkeypatch.setattr(standalone, "get_status", Mock(return_value=None))
    assert standalone.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"


def test_plugin_start_api_calls_launcher_and_rejects_embedded(monkeypatch, tmp_path):
    import plugin

    monkeypatch.setenv("WS_COLLAB_PLUGIN_MODE", "standalone")
    start = Mock(return_value={"status": "started", "supervisor_pid": 123})
    monkeypatch.setattr(standalone, "start_server", start)
    assert plugin.start_server(port=8888, state_dir=tmp_path, python_executable="own-python")["status"] == "started"
    assert start.call_args.kwargs["port"] == 8888
    assert start.call_args.kwargs["state_dir"] == tmp_path
    assert start.call_args.kwargs["python_executable"] == "own-python"
    assert "start_server" in plugin.__all__
    monkeypatch.setenv("WS_COLLAB_PLUGIN_MODE", "embedded")
    with pytest.raises(RuntimeError, match="owned by its host"):
        plugin.start_server()


def test_real_cli_restart_shutdown_and_plugin_start_preserve_settings(tmp_path, monkeypatch):
    from ws_collab.captioner import CaptionerSettings
    import plugin

    launched = []
    launch = standalone.launch

    def tracked_launch(*args, **kwargs):
        proc = launch(*args, **kwargs)
        if proc:
            launched.append(proc)
        return proc

    monkeypatch.setattr(standalone, "launch", tracked_launch)
    CaptionerSettings(tmp_path).update({"paused": True})
    for name, value in {
        "WS_COLLAB_STATE_DIR": str(tmp_path), "WS_COLLAB_PLUGIN_MODE": "standalone",
        "WS_COLLAB_AUDIO_ENABLED": "0", "WS_COLLAB_AUDIO_BACKEND": "fake",
        "WS_COLLAB_TTS_BACKEND": "fake", "WS_COLLAB_STT_ENGINES": "fallback_alpha",
        "WS_COLLAB_AUTH_DISABLED": "0", "WS_COLLAB_REQUIRE_AUTH": "1",
        "WS_COLLAB_ADMIN_TOKEN": "launcher-test-only", "WS_COLLAB_TOKEN": "launcher-test-only",
        "WS_COLLAB_BIND_ADDRESSES": "", "WS_COLLAB_TLS_CERT_FILE": "", "WS_COLLAB_TLS_KEY_FILE": "",
    }.items():
        monkeypatch.setenv(name, value)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = standalone.launch(port=port, state_dir=tmp_path, timeout=40)
    assert proc is not None
    try:
        first = standalone.get_status(port=port)["boot_id"]
        assert plugin.start_server(port=port, state_dir=tmp_path)["status"] == "already-running"
        config_url = f"http://127.0.0.1:{port}/ws_collab/language-chat/config"
        config = standalone._read_json(config_url, timeout=5, token="launcher-test-only")
        command = [sys.executable, "-m", "ws_collab.standalone"]
        restart = subprocess.run(command + ["restart", "--port", str(port), "--timeout", "40"], capture_output=True, text=True, timeout=50)
        assert restart.returncode == 0, restart.stdout + restart.stderr
        assert json.loads(restart.stdout)["boot_id"] != first
        assert standalone._read_json(config_url, timeout=5, token="launcher-test-only") == config
        stopped = subprocess.run(command + ["shutdown", "--port", str(port), "--timeout", "40"], capture_output=True, text=True, timeout=50)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert proc.wait(timeout=20) == 0
        # Starting a shut-down service uses the local plugin API, not a dead HTTP route.
        result = plugin.start_server(port=port, state_dir=tmp_path, timeout=40)
        assert result["status"] == "started" and result["boot_id"] != first
    finally:
        standalone.control_server("shutdown", port=port, timeout=40)
        for child in launched:
            if child.poll() is None:
                child.wait(timeout=20)
