import hashlib

import pytest

from esi03.session import Session as PreviousSession
from esi04.migration import migrate_esi03, validate_save
from esi04.session import Session


def test_copy_import_includes_wal_and_preserves_original_character(tmp_path):
    source = tmp_path/"old.sqlite3"
    old = PreviousSession(source)
    try:
        old.create_turn("星砂村は月の国です。")
        before = source.read_bytes()
        destination = tmp_path/"new.sqlite3"
        result = migrate_esi03(source, destination)
        assert source.read_bytes() == before
        assert validate_save(source)["release"] == "ESI-03"
        assert validate_save(destination)["release"] == "ESI-04"
        restored = Session(destination)
        try:
            assert restored.agent_id == old.agent_id
            assert restored.knowledge.entries()[0]["value"] == "月の国"
            assert restored.experience.state.preferences.min_question_interval == 0
            assert restored.store.meta("branch_id") != old.store.meta("branch_id")
        finally:
            restored.close()
        assert hashlib.sha256(result["archive"].read_bytes()).hexdigest() == result["archive_sha256"]
        with pytest.raises(FileExistsError):
            migrate_esi03(source, destination)
    finally:
        old.close()


def test_old_avatar_position_inside_new_solid_furniture_is_adjusted_and_recorded(tmp_path):
    source = tmp_path/"old.sqlite3"
    old = PreviousSession(source)
    old.world.position.x, old.world.position.z = 4.5, 1.0
    old.close()
    result = migrate_esi03(source, tmp_path/"new.sqlite3")
    assert result["position_adjusted_from"] == {"x": 4.5, "y": 0.0, "z": 1.0}
    new = Session(tmp_path/"new.sqlite3")
    try:
        assert new.world.free(new.world.position.x, new.world.position.z)
        assert new.experience.state.traces[-1].kind == "migration"
    finally:
        new.close()
