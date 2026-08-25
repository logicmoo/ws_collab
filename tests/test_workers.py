"""Worker registry, health thresholds, alert deduplication, and recovery."""

from __future__ import annotations

import time

import pytest

from ws_collab.events import ALERT_RAISED, ALERT_RECOVERED, streams_for_role
from ws_collab.workers import (
    CADENCE_CONTINUOUS,
    CADENCE_ON_ACTIVATION,
    STATE_OK,
    STATE_OVERDUE,
    STATE_UNRESPONSIVE,
    STATE_WARN,
    WorkerMonitor,
)

ALERTS = streams_for_role("alerts")[0]


@pytest.fixture
def monitor(config):
    published: list[dict] = []

    def publish(**kwargs):
        published.append(kwargs)
        return {"id": f"e{len(published)}", "seq": len(published)}

    instance = WorkerMonitor(config, publish)
    instance.published = published  # type: ignore[attr-defined]
    return instance


def _alerts(monitor, kind: str) -> list[dict]:
    return [p for p in monitor.published if p.get("type") == kind]


def _age(monitor, worker_id: str, seconds: float) -> None:
    """Simulate the passage of time without sleeping."""

    monitor._workers[worker_id].last_status_at = time.time() - seconds


# ----------------------------------------------------------------- registry
def test_registration_makes_a_worker_visible(monitor) -> None:
    monitor.register("w1", task="solve arc")
    listed = monitor.list_workers()
    assert [w["worker_id"] for w in listed] == ["w1"]
    assert listed[0]["task"] == "solve arc" and listed[0]["state"] == STATE_OK


def test_registry_rehydrates_from_status_events(monitor) -> None:
    from ws_collab.events import WORKER_REGISTERED, WORKER_STATUS

    monitor.rebuild_from_events([
        {"type": WORKER_REGISTERED, "ts": "2026-01-01T00:00:00Z",
         "data": {"worker_id": "w1", "task": "demo", "meta": {"host": "local"}}},
        {"type": WORKER_STATUS, "ts": "2026-01-01T00:00:05Z",
         "data": {"worker_id": "w1", "status": "active"}},
    ])
    workers = {w["worker_id"]: w for w in monitor.list_workers()}
    assert "w1" in workers, "restored worker must be visible/assignable after restart"
    assert workers["w1"]["task"] == "demo"
    assert workers["w1"]["last_status"] == "active"
    # The old timestamp must be preserved, not reset to 'freshly seen'.
    assert workers["w1"]["last_status_age_seconds"] > 0


def test_status_check_in_is_recorded(monitor) -> None:
    monitor.register("w1")
    monitor.record_status("w1", "working", {"phase": "step-3"})
    snapshot = monitor.snapshot("w1")
    assert snapshot["last_status"] == "working"
    assert snapshot["last_status_age_seconds"] < 5


def test_status_from_an_unregistered_worker_is_still_tracked(monitor) -> None:
    monitor.record_status("ghost", "alive")
    assert monitor.snapshot("ghost")["worker_id"] == "ghost"


# ------------------------------------------------------------- health states
def test_state_escalates_as_a_worker_goes_quiet(monitor, config) -> None:
    monitor.register("w1")
    for seconds, expected in [
        (config.worker_warn_seconds + 1, STATE_WARN),
        (config.worker_overdue_seconds + 1, STATE_OVERDUE),
        (config.worker_unresponsive_seconds + 1, STATE_UNRESPONSIVE),
    ]:
        _age(monitor, "w1", seconds)
        monitor.evaluate()
        assert monitor.snapshot("w1")["state"] == expected


def test_a_quiet_worker_is_never_assumed_terminated(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_unresponsive_seconds * 10)
    monitor.evaluate()
    assert monitor.snapshot("w1")["state"] == STATE_UNRESPONSIVE
    assert monitor.snapshot("w1")["terminated_confirmed"] is False


def test_termination_requires_independent_confirmation(monitor) -> None:
    monitor.register("w1")
    monitor.confirm_terminated("w1", operator="alice")
    assert monitor.snapshot("w1")["terminated_confirmed"] is True


# ------------------------------------------------------------------- alerts
def test_an_alert_is_raised_when_a_worker_becomes_overdue(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_overdue_seconds + 1)
    monitor.evaluate()
    assert _alerts(monitor, ALERT_RAISED), "an overdue worker must raise an alert"


def test_repeated_evaluation_does_not_duplicate_an_alert(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_overdue_seconds + 1)
    for _ in range(5):
        monitor.evaluate()
    per_worker = [a for a in _alerts(monitor, ALERT_RAISED) if a["data"].get("worker_id") == "w1"]
    assert len(per_worker) == 1, "alerts must be deduplicated per worker"


def test_worsening_state_escalates_with_a_new_alert(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_overdue_seconds + 1)
    monitor.evaluate()
    _age(monitor, "w1", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    severities = [a["data"]["severity"] for a in _alerts(monitor, ALERT_RAISED) if "severity" in a["data"]]
    assert "danger" in severities, "escalation must be visible"


def test_unresponsive_alert_requests_confirmation(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    raised = [a for a in _alerts(monitor, ALERT_RAISED) if a["data"].get("state") == STATE_UNRESPONSIVE]
    assert raised and raised[0]["data"]["confirmation_required"] is True


def test_check_in_recovers_the_worker_and_clears_the_alert(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_overdue_seconds + 1)
    monitor.evaluate()
    monitor.record_status("w1", "back online")
    assert monitor.snapshot("w1")["state"] == STATE_OK
    assert _alerts(monitor, ALERT_RECOVERED), "recovery must be announced"


def test_alerts_are_written_to_the_alert_stream(monitor, config) -> None:
    monitor.register("w1")
    _age(monitor, "w1", config.worker_overdue_seconds + 1)
    monitor.evaluate()
    assert all(a["stream"] == ALERTS for a in _alerts(monitor, ALERT_RAISED))


# -------------------------------------------------------- team-wide failure
def test_team_wide_failure_is_reported_once(monitor, config) -> None:
    for worker in ("w1", "w2", "w3"):
        monitor.register(worker)
        _age(monitor, worker, config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    monitor.evaluate()
    team = [a for a in _alerts(monitor, ALERT_RAISED) if a["data"].get("scope") == "team"]
    assert len(team) == 1, "team-wide failure must be raised exactly once"


def test_team_recovers_when_any_worker_reports(monitor, config) -> None:
    for worker in ("w1", "w2"):
        monitor.register(worker)
        _age(monitor, worker, config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    monitor.record_status("w1", "alive")
    monitor.evaluate()
    recovered = [a for a in _alerts(monitor, ALERT_RECOVERED) if a["data"].get("scope") == "team"]
    assert recovered, "one responsive worker must clear the team alert"


def test_a_single_reporting_worker_is_enough_to_observe_the_team(monitor, config) -> None:
    """The last responsive worker may be the only observer -- it must not be silenced."""

    monitor.register("reporter")
    monitor.register("other")
    _age(monitor, "other", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    assert monitor.snapshot("reporter")["state"] == STATE_OK
    assert _alerts(monitor, ALERT_RAISED), "the quiet peer must still be reported"


# -------------------------------------------------------------- via service
def test_service_exposes_worker_lifecycle(service) -> None:
    service.register_worker("w1", task="demo")
    service.worker_status("w1", "working")
    workers = service.list_workers()["workers"]
    assert [w["worker_id"] for w in workers] == ["w1"]
    assert service.run_monitor_cycle()["workers"], "a monitor cycle must be runnable on demand"


# ------------------------------------------------- activation-cadence workers
def test_activation_cadence_is_declared_at_registration_and_visible(monitor) -> None:
    monitor.register("cron-worker", task="bounded monitor", meta={"cadence": "on-activation"})
    monitor.register("live-worker", task="relay duty")
    listed = {w["worker_id"]: w for w in monitor.list_workers()}
    assert listed["cron-worker"]["cadence"] == CADENCE_ON_ACTIVATION
    assert listed["live-worker"]["cadence"] == CADENCE_CONTINUOUS


def test_activation_cadence_quiet_is_informational_not_a_failure(monitor, config) -> None:
    """A worker that only runs when its cron fires is quiet by design."""

    monitor.register("cron-worker", meta={"cadence": "on-activation"})
    _age(monitor, "cron-worker", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()

    raised = _alerts(monitor, ALERT_RAISED)
    assert len(raised) == 1
    alert = raised[0]["data"]
    # The state is still reported truthfully; only the alarm is downgraded.
    assert alert["state"] == STATE_UNRESPONSIVE
    assert alert["severity"] == "info"
    assert alert["confirmation_required"] is False
    assert alert["cadence"] == CADENCE_ON_ACTIVATION


def test_continuous_worker_quiet_still_raises_a_danger_alert(monitor, config) -> None:
    monitor.register("live-worker")
    _age(monitor, "live-worker", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    alert = _alerts(monitor, ALERT_RAISED)[0]["data"]
    assert alert["severity"] == "danger"
    assert alert["confirmation_required"] is True
    assert alert["cadence"] == CADENCE_CONTINUOUS


def test_quiet_activation_workers_do_not_trigger_a_team_wide_failure(monitor, config) -> None:
    """Cron workers going quiet is expected, so it is not evidence of collapse."""

    def team_alerts() -> list[dict]:
        return [
            p for p in _alerts(monitor, ALERT_RAISED)
            if p.get("data", {}).get("scope") == "team"
        ]

    monitor.register("cron-a", meta={"cadence": "on-activation"})
    monitor.register("cron-b", meta={"cadence": "on-activation"})
    monitor.register("live-worker")
    _age(monitor, "cron-a", config.worker_unresponsive_seconds + 1)
    _age(monitor, "cron-b", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()

    assert team_alerts() == []

    # The one worker that promised continuous reporting going quiet is decisive.
    _age(monitor, "live-worker", config.worker_unresponsive_seconds + 1)
    monitor.evaluate()
    assert len(team_alerts()) == 1
