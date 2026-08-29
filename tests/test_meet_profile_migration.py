from __future__ import annotations

from pathlib import Path


def test_meet_profile_migration_copies_old_default(tmp_path, monkeypatch) -> None:
    from ws_collab.meet_bridge import cdp

    old = tmp_path / "old_profile"
    new = tmp_path / "collab_state" / "meet_bridge_profile"
    old.mkdir(parents=True)
    (old / "Preferences").write_text("{}", encoding="utf-8")
    old_companion = Path(str(old) + "_companion")
    old_companion.mkdir()
    (old_companion / "Preferences").write_text("{}", encoding="utf-8")

    monkeypatch.delenv("WS_COLLAB_MEET_PROFILE_DIR", raising=False)
    monkeypatch.setattr(cdp, "_OLD_DEFAULT_PROFILE", old)
    monkeypatch.setattr(cdp, "DEFAULT_PROFILE", new)

    resolved = cdp.ensure_default_profile_migrated()
    assert resolved == new
    assert (new / "Preferences").is_file()
    assert (Path(str(new) + "_companion") / "Preferences").is_file()
