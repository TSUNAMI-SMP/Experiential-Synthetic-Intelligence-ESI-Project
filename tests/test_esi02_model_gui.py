import json

import pytest

from esi02.config import Settings
from esi02.desktop_preferences import read_preferences
from esi02.session import Session


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def model(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QWidget

    from esi02 import desktop

    folder = tmp_path / "exported"
    folder.mkdir()
    path = folder / "fixture.model3.json"
    (folder / "fixture.moc3").write_bytes(b"MOC3" + bytes(100))
    texture = QImage(32, 32, QImage.Format_RGBA8888)
    texture.fill(Qt.white)
    texture.save(str(folder / "texture.png"))
    path.write_text(
        json.dumps({"Version": 3, "FileReferences": {"Moc": "fixture.moc3", "Textures": ["texture.png"]}})
    )

    class Canvas(QWidget):
        problem = Signal(str)

        def __init__(self, path, parent, meter):
            super().__init__(parent)
            self.path, self.meter, self.released = path, meter, False

        def release(self):
            self.released = True

    monkeypatch.setattr(desktop, "probe_model", lambda path: None)
    monkeypatch.setattr(desktop, "AudioLiveCanvas", Canvas)
    return path, Canvas


def window_for(tmp_path, monkeypatch):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    return gui.Window(Session(tmp_path / "session.sqlite3", Settings(playback=False)))


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_selected_model_persists_reloads_releases_and_preserves_free_position(
    app, model, tmp_path, monkeypatch
):
    path, Canvas = model
    window = window_for(tmp_path, monkeypatch)
    try:
        window.desktop.avatar.move(-2300, 3200)
        window.set_model(path)
        canvas = window.desktop.avatar.canvas
        assert isinstance(canvas, Canvas) and not window.ui_error
        assert read_preferences(window.desktop.path).model_path == str(path)
        window.reload_model()
        assert canvas.released and window.desktop.avatar.canvas is not canvas
        assert [window.desktop.avatar.x(), window.desktop.avatar.y()] == [-2300, 3200]
    finally:
        dispose(window, app)
    restored = window_for(tmp_path, monkeypatch)
    try:
        assert isinstance(restored.desktop.avatar.canvas, Canvas)
        assert restored.desktop.avatar.model_path == path
        current = restored.desktop.avatar.canvas
        restored.set_model(None)
        assert current.released and restored.desktop.avatar.model_path is None
        assert read_preferences(restored.desktop.path).model_path is None
    finally:
        dispose(restored, app)


def test_invalid_model_keeps_previous_canvas_and_saved_path(app, model, tmp_path, monkeypatch):
    path, _ = model
    window = window_for(tmp_path, monkeypatch)
    try:
        window.set_model(path)
        previous = window.desktop.avatar.canvas
        bad = path.with_name("bad.model3.json")
        bad.write_text("{}")
        window.set_model(bad)
        assert window.desktop.avatar.canvas is previous and not previous.released
        assert read_preferences(window.desktop.path).model_path == str(path)
        assert "モデルを読み込めません" in window.error.text()
    finally:
        dispose(window, app)


def test_missing_saved_model_keeps_settings_and_shows_still_with_error(app, model, tmp_path, monkeypatch):
    path, Canvas = model
    window = window_for(tmp_path, monkeypatch)
    window.set_model(path)
    dispose(window, app)
    path.unlink()
    restored = window_for(tmp_path, monkeypatch)
    try:
        assert not isinstance(restored.desktop.avatar.canvas, Canvas)
        assert read_preferences(restored.desktop.path).model_path == str(path)
        assert "モデルを読み込めません" in restored.error.text()
    finally:
        dispose(restored, app)
