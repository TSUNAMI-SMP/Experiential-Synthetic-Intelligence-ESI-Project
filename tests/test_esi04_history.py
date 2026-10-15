"""Regression coverage for ZIP relocation, full transcripts and non-destructive recovery."""

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from esi02.session import Session as Session02
from esi03.session import Session as Session03
from esi04.migration import migrate_esi03
from esi04.save_history import discover_saves, history_page, previous_source, save_summary, saved_path
from esi04.session import Session


def conversations(session, count):
    for index in range(count):
        turn = session.create_turn(f"会話番号 {index}")
        session.update_turn(turn["id"], reply=f"返答番号 {index}", stage="complete")


@pytest.mark.parametrize("previous", [Session02, Session03])
def test_copy_migration_preserves_older_conversations_and_new_save(tmp_path, previous):
    path, destination = tmp_path / "before.sqlite3", tmp_path / "after.sqlite3"
    old = previous(path)
    conversations(old, 30)
    agent_id = old.agent_id
    old.close()
    original = path.read_bytes()
    result = migrate_esi03(path, destination)
    assert path.read_bytes() == original
    with (sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True) as source_db,
          sqlite3.connect(result["archive"].resolve().as_uri()+"?mode=ro", uri=True) as archive_db):
        # SQLite backup changes its header counters but must preserve every row.
        assert list(source_db.iterdump()) == list(archive_db.iterdump())
    assert save_summary(destination)["turns"] == 30
    restored = Session(destination)
    try:
        assert restored.agent_id == agent_id
        assert len(restored.view()["turns"]) == 12  # The preview is intentionally bounded.
        conversations(restored, 1)
    finally:
        restored.close()
    first = history_page(destination, limit=7)
    second = history_page(destination, before=first[-1][0], limit=100)
    turns = [turn for _, turn in first+second]
    assert len(turns) == 31 and len({turn["id"] for turn in turns}) == 31
    assert any(turn["input"] == "会話番号 0" and turn["reply"] == "返答番号 0" for turn in turns)
    assert path.read_bytes() == original
    with pytest.raises(FileExistsError):
        migrate_esi03(path, destination)


def test_history_survives_retained_turn_pruning_and_restart(tmp_path):
    from esi04.config import Settings

    path = tmp_path / "save.sqlite3"
    session = Session(path, Settings(retained_turns=16))
    conversations(session, 40)
    session.close()
    restored = Session(path)
    try:
        assert len(restored.turns) == 16
        assert save_summary(path)["turns"] == 40
        assert len(history_page(path)) == 40
    finally:
        restored.close()


def test_pre_patch_event_history_is_archived_before_startup_compacts_the_log(tmp_path):
    from esi04.config import Settings

    path = tmp_path / "save.sqlite3"
    session = Session(path, Settings(retained_turns=16, max_records=1000))
    conversations(session, 40)
    session.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE esi04_conversations")  # The schema shipped in 0.4.0.
        db.executemany("INSERT INTO records(kind,payload) VALUES ('old_tick','{}')", [()]*1001)
    restored = Session(path)
    try:
        assert restored.store.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert len(restored.turns) == 16
        assert save_summary(path)["turns"] == 40 and len(history_page(path)) == 40
        conversations(restored, 1)
        restored.store.max_records = 1
    finally:
        restored.close()
    assert save_summary(path)["turns"] == 41 and len(history_page(path)) == 41


def test_moved_zip_rebases_old_paths_and_discovery_does_not_overwrite_a_new_character(tmp_path):
    current = tmp_path / "data/esi04/session.sqlite3"
    old = tmp_path / "data/esi03/characters/one/session.sqlite3"
    session = Session03(old)
    conversations(session, 3)
    session.close()
    Session(current).close()
    pref = tmp_path / "data/esi03/launcher.json"
    pref.write_text(json.dumps({"save_path": r"C:\Downloads\previous\data\esi03\characters\one\session.sqlite3"}))
    before = {p: p.read_bytes() for p in (current, old, pref)}
    assert previous_source(tmp_path, "esi03") == old
    assert saved_path(tmp_path, "data/esi04/session.sqlite3") == current
    entries = discover_saves(tmp_path, current)
    assert {entry["path"] for entry in entries} == {old, current}
    assert next(entry for entry in entries if entry["path"] == old)["turns"] == 3
    assert all(p.read_bytes() == value for p, value in before.items())


def test_missing_selected_character_does_not_fall_back_to_someone_else(tmp_path):
    default = tmp_path / "data/esi03/session.sqlite3"
    Session03(default).close()
    (default.parent / "launcher.json").write_text(json.dumps({"save_path": "missing-character.sqlite3"}))
    with pytest.raises(FileNotFoundError, match="以前に選んだセーブ"):
        previous_source(tmp_path, "esi03")
    assert discover_saves(tmp_path)[0]["path"] == default


def test_launcher_uses_portable_preferences_and_migrates_the_selected_save(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi04.launcher import Launcher

    app = QApplication.instance() or QApplication([])

    class Services:
        def __init__(self, root, preferences, notify):
            self.root, self.preferences, self.cancel = root, preferences, threading.Event()

        def close(self):
            pass

    (tmp_path / "config").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1] / "config/esi04-windows.toml", tmp_path / "config")
    source = tmp_path / "data/esi03/characters/one/session.sqlite3"
    old = Session03(source)
    conversations(old, 2)
    old.close()
    (tmp_path / "data/esi03/launcher.json").write_text(json.dumps({"save_path": str(source)}))
    launcher = Launcher(tmp_path, services_factory=Services)
    try:
        settings = launcher.migrate_if_needed(launcher.settings())
        assert settings.release == "ESI-04" and save_summary(launcher.save_path)["turns"] == 2
        launcher.preferences.save_path = str(launcher.save_path)
        launcher.save_preferences()
        assert json.loads(launcher.preferences_path.read_text())["save_path"] == str(Path("data/esi04/session.sqlite3"))
    finally:
        launcher.shutdown()
        launcher.close()
        launcher.deleteLater()
        app.processEvents()


def test_recovery_screen_opens_a_copy_and_keeps_the_current_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QTimer
    from PySide6.QtWidgets import QApplication, QListWidget

    from esi02 import gui as base_gui
    from esi02.desktop_preferences import DesktopPreferences, write_preferences
    from esi04 import gui
    from esi04.history_gui import choose_recovery

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(base_gui, "audio_devices", lambda: [])
    current, source, destination = (tmp_path / "data/esi04/session.sqlite3",
                                    tmp_path / "data/esi03/session.sqlite3", tmp_path / "restored.sqlite3")
    old = Session03(source)
    conversations(old, 15)
    old.close()
    original = source.read_bytes()
    write_preferences(current.parent / "esi04-desktop.json", DesktopPreferences(model_path=None))
    write_preferences(destination.parent / "esi04-desktop.json", DesktopPreferences(model_path=None))
    session = Session(current)
    session.create_turn("今のセーブの会話も残してください。")
    window = gui.Window(session)
    try:
        def select_old():
            dialog = app.activeModalWidget()
            choices = dialog.findChild(QListWidget)
            index = next(i for i in range(choices.count()) if "ESI-03" in choices.item(i).text())
            choices.setCurrentRow(index)
            dialog.accept()
        QTimer.singleShot(0, select_old)
        assert choose_recovery(window, tmp_path, current) == source
        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *args: (str(destination), ""))
        window.open_selected_save(source)
        assert window.session.store.path == destination and not window.ui_error
        window.history_panel.reload()
        assert "会話番号 0" in window.history_panel.text.toPlainText()
        assert "15件" in window.history_panel.identity.text()
        assert source.read_bytes() == original
        assert history_page(current)[0][1]["input"] == "今のセーブの会話も残してください。"
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
