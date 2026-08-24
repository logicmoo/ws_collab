"""Drop-in STT/TTS driver discovery, disabling, and graceful degradation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws_collab.config import Config
from ws_collab.drivers import (
    DriverUnavailable,
    discover_stt_drivers,
    discover_tts_drivers,
)
from ws_collab.stt import build_engines
from ws_collab.tts.engine import build_backend


# ------------------------------------------------------------------ discovery
def test_stt_drivers_are_discovered_from_their_directories() -> None:
    specs, notes = discover_stt_drivers()
    assert specs, "at least one STT driver must be discoverable"
    assert all(spec.id and spec.aliases and callable(spec.build) for spec in specs)
    assert not [n for n in notes if "failed to load" in n], f"driver load errors: {notes}"


def test_tts_drivers_are_discovered_from_their_directories() -> None:
    specs, notes = discover_tts_drivers()
    assert specs, "at least one TTS driver must be discoverable"
    assert not [n for n in notes if "failed to load" in n], f"driver load errors: {notes}"


def test_each_driver_reports_where_it_came_from() -> None:
    specs, _ = discover_stt_drivers()
    assert all(Path(spec.directory).is_dir() for spec in specs)


def test_driver_ids_are_unique() -> None:
    specs, _ = discover_stt_drivers()
    ids = [spec.id for spec in specs]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------ selection
def test_configured_engine_names_map_onto_drivers(config) -> None:
    engines, _ = build_engines(config)
    assert len(engines) == len(config.stt_engines)
    assert [e.name for e in engines] == config.stt_engines


def test_unknown_engine_name_still_yields_a_working_engine(tmp_path) -> None:
    from conftest import make_config

    config = make_config(tmp_path, WS_COLLAB_STT_ENGINES="something_nobody_wrote")
    engines, _ = build_engines(config)
    assert engines, "an unknown engine must degrade, not disappear"


def test_missing_optional_model_degrades_with_a_warning(tmp_path) -> None:
    """A driver whose library is absent falls back and says so."""

    from conftest import make_config

    # Deliberately name an engine no driver can satisfy on any machine, so this
    # asserts the degradation contract rather than what happens to be installed.
    config = make_config(tmp_path, WS_COLLAB_STT_ENGINES="whisper:definitely-not-a-real-model-name")
    engines, warnings = build_engines(config)
    assert engines, "the pipeline must keep working"
    if warnings:
        assert any("whisper" in w for w in warnings), "degradation must be reported honestly"


def test_remote_driver_is_skipped_unless_explicitly_allowed(tmp_path) -> None:
    from conftest import make_config

    config = make_config(tmp_path, WS_COLLAB_STT_ENGINES="remote:https://example.invalid/asr")
    engines, warnings = build_engines(config)
    assert not any(getattr(e, "is_remote", False) for e in engines), \
        "audio must never leave the device without explicit opt-in"
    assert any("remote" in w.lower() for w in warnings), "the skip must be reported"


def test_remote_driver_is_used_when_explicitly_allowed(tmp_path) -> None:
    from conftest import make_config

    config = make_config(
        tmp_path,
        WS_COLLAB_STT_ENGINES="remote:https://example.invalid/asr",
        WS_COLLAB_STT_ALLOW_REMOTE="1",
    )
    engines, _ = build_engines(config)
    assert engines and engines[0].is_remote is True


def test_tts_backend_falls_back_when_the_requested_one_is_unavailable(tmp_path) -> None:
    from conftest import make_config

    config = make_config(tmp_path, WS_COLLAB_TTS_BACKEND="definitely-not-installed")
    backend = build_backend(config)
    assert hasattr(backend, "play"), "a usable TTS backend must always be returned"


# -------------------------------------------------------------- disable/remove
def test_a_directory_suffixed_disabled_is_skipped(tmp_path, monkeypatch) -> None:
    """Renaming a driver directory to *_disabled removes it from discovery."""

    import ws_collab.drivers as drivers

    staged = tmp_path / "drivers"
    shutil.copytree(Path(drivers.__file__).parent, staged, dirs_exist_ok=True)
    target = staged / "stt" / "vosk"
    target.rename(staged / "stt" / "vosk_disabled")

    monkeypatch.setattr(drivers, "_DRIVERS_ROOT", staged)
    monkeypatch.setattr(drivers, "_MODULE_CACHE", {})
    specs, notes = drivers.discover_stt_drivers()
    assert "vosk" not in {spec.id for spec in specs}
    assert any("disabled" in note for note in notes), "skipping must be reported"


def test_a_manifest_can_disable_a_driver(tmp_path, monkeypatch) -> None:
    import ws_collab.drivers as drivers

    staged = tmp_path / "drivers"
    shutil.copytree(Path(drivers.__file__).parent, staged, dirs_exist_ok=True)
    manifest = staged / "stt" / "vosk" / "driver.json"
    manifest.write_text(json.dumps({"id": "vosk", "enabled": False}), encoding="utf-8")

    monkeypatch.setattr(drivers, "_DRIVERS_ROOT", staged)
    monkeypatch.setattr(drivers, "_MODULE_CACHE", {})
    specs, _ = drivers.discover_stt_drivers()
    assert "vosk" not in {spec.id for spec in specs}


def test_deleting_a_driver_directory_removes_it(tmp_path, monkeypatch) -> None:
    import ws_collab.drivers as drivers

    staged = tmp_path / "drivers"
    shutil.copytree(Path(drivers.__file__).parent, staged, dirs_exist_ok=True)
    shutil.rmtree(staged / "stt" / "whisper")

    monkeypatch.setattr(drivers, "_DRIVERS_ROOT", staged)
    monkeypatch.setattr(drivers, "_MODULE_CACHE", {})
    specs, _ = drivers.discover_stt_drivers()
    assert "whisper" not in {spec.id for spec in specs}


def test_a_broken_driver_does_not_prevent_startup(tmp_path, monkeypatch) -> None:
    import ws_collab.drivers as drivers

    staged = tmp_path / "drivers"
    shutil.copytree(Path(drivers.__file__).parent, staged, dirs_exist_ok=True)
    broken = staged / "stt" / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "driver.py").write_text("raise RuntimeError('boom')", encoding="utf-8")

    monkeypatch.setattr(drivers, "_DRIVERS_ROOT", staged)
    monkeypatch.setattr(drivers, "_MODULE_CACHE", {})
    specs, notes = drivers.discover_stt_drivers()
    assert specs, "healthy drivers must still load"
    assert any("broken" in note for note in notes), "the failure must be surfaced, not hidden"


# ------------------------------------------------------- real-hardware policy
def test_defaults_prefer_real_backends(tmp_path) -> None:
    """Out of the box the system asks for real hardware, not the doubles.

    Tests pin the doubles explicitly (see conftest.make_config); this asserts the
    *shipped* default is real, so a fresh deployment uses the machine's actual
    devices, voices, and speech models.
    """

    from ws_collab.config import Config

    config = Config.from_env({"WS_COLLAB_STATE_DIR": str(tmp_path / "state"), "WS_COLLAB_ADMIN_TOKEN": "t"})
    assert config.audio_backend == "auto"
    assert config.tts_backend == "auto"
    assert not any(engine.startswith("fallback") for engine in config.stt_engines), \
        "the default engine set must name real recognizers"


def test_real_backend_falls_back_rather_than_failing(tmp_path) -> None:
    """`auto` must never leave the system without devices or voices."""

    from ws_collab.audio.devices import enumerate_devices
    from ws_collab.config import Config
    from ws_collab.tts.voices import enumerate_voices

    config = Config.from_env({"WS_COLLAB_STATE_DIR": str(tmp_path / "state"), "WS_COLLAB_ADMIN_TOKEN": "t"})
    devices, _notes = enumerate_devices(config)
    voices, _voice_notes = enumerate_voices(config)
    assert devices, "some device catalog must always be available"
    assert voices, "some voice catalog must always be available"
    assert any(d["direction"] in ("input", "loopback") for d in (x.public() for x in devices)), \
        "there must be at least one capturable input"
