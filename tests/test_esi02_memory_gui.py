import sqlite3

import pytest

from esi02.config import Settings
from esi02.session import Session


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_memory_editor_add_search_correct_delete_and_saved_reload(app, tmp_path, monkeypatch):
    from esi02 import gui
    from esi02.memory_gui import MemoryDialog

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    path = tmp_path / "session.sqlite3"
    session = Session(path, Settings(playback=False))
    window = gui.Window(session)
    dialog = MemoryDialog(window)
    try:
        dialog.label.setText("名前")
        dialog.value.setText("太郎")
        dialog.add()
        dialog.table.selectRow(0)
        dialog.value.setText("次郎")
        dialog.confirm.click()
        assert session.memory.entries()[0]["value"] == "次郎"
        assert "画面で確認" in dialog.table.item(0, 3).text()
        dialog.query.setText("次郎")
        assert dialog.table.rowCount() == 1
        dialog.delete_button.click()
        assert not session.memory.entries()
        assert dialog.table.rowCount() == 0
    finally:
        dialog.close()
        dialog.deleteLater()
        dispose(window, app)
    restored = Session(path)
    try:
        assert not restored.memory.entries()
    finally:
        restored.close()


def test_invalid_save_selection_preserves_running_session_valid_selection_restores_settings(
    app, tmp_path, monkeypatch
):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    current = Session(tmp_path / "current.sqlite3", Settings(playback=False))
    saved_path = tmp_path / "selected.sqlite3"
    saved = Session(saved_path, Settings(cpu_threads=3, playback=False))
    saved.memory_operation("add", {"kind": "preference", "label": "紅茶", "value": "好き"})
    saved.close()
    window = gui.Window(current)

    class Running:
        stopped = False

        def stop(self):
            self.stopped = True

    running = Running()
    window.runtime = running
    bad = tmp_path / "unrelated.sqlite3"
    db = sqlite3.connect(bad)
    db.execute("CREATE TABLE unrelated (id INTEGER)")
    db.close()
    before = bad.read_bytes()
    try:
        with pytest.raises(sqlite3.OperationalError):
            window.switch_save(bad)
        assert window.session is current and window.runtime is running and not running.stopped
        assert bad.read_bytes() == before
        window.switch_save(saved_path)
        assert running.stopped and window.runtime is None
        assert window.session.settings.cpu_threads == 3
        assert window.session.memory.entries()[0]["label"] == "紅茶"
        assert "selected.sqlite3" in window.statusBar().currentMessage()
    finally:
        dispose(window, app)


def test_corrupt_session_contents_rejected_before_any_write(app, tmp_path, monkeypatch):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    current = Session(tmp_path / "current.sqlite3")
    selected = tmp_path / "corrupt.sqlite3"
    other = Session(selected)
    other.close()
    db = sqlite3.connect(selected)
    db.execute("UPDATE snapshots SET payload=?", ('{"release":"ESI-02","elapsed":-1,"epoch":0,"turns":[]}',))
    db.commit()
    db.close()
    before = selected.read_bytes()
    window = gui.Window(current)
    try:
        with pytest.raises(ValueError):
            window.switch_save(selected)
        assert window.session is current and selected.read_bytes() == before
    finally:
        dispose(window, app)
