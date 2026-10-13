import math

import pytest

from esi02.config import Settings
from esi02.session import Session


def test_3d_floor_click_moves_without_changing_dialogue_and_camera_can_rotate(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    app = QApplication.instance() or QApplication([])
    session = Session(tmp_path / "room.sqlite3", Settings(playback=False))
    window = gui.Window(session)
    try:
        window.tabs.setCurrentIndex(1)
        window.resize(1200, 860)
        window.show()
        app.processEvents()
        canvas = window.world_panel.canvas
        target = canvas.project(1.2, 0, 3.9)
        QTest.mouseClick(canvas, Qt.LeftButton, pos=target.toPoint())
        assert session.world.destination.x == pytest.approx(1.2, abs=0.05)
        assert session.world.destination.z == pytest.approx(3.9, abs=0.05)
        assert not session.turns and session.epoch == 0
        window.world_panel.rotate()
        app.processEvents()
        assert canvas.yaw == pytest.approx(math.pi / 4 + math.pi / 6)
        assert not window.grab().isNull()
        window.world_panel.temperature.setValue(35)
        assert window.world_action("environment", {"temperature": 35, "noise_db": 60, "lamp": False})
        assert "温度の不快度" in window.world_panel.stimulus.text()
        assert session.world.room.temperature == 35 and not session.world.room.lamp
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_projected_room_preserves_world_positions_under_camera_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    QApplication.instance() or QApplication([])
    session = Session(tmp_path / "room.sqlite3", Settings(playback=False))
    window = gui.Window(session)
    try:
        canvas = window.world_panel.canvas
        for angle in (0, math.pi / 4, math.pi, -math.pi / 2):
            canvas.yaw = angle
            canvas.fit()
            for x, z in ((0.1, 0.1), (3, 2.5), (5.9, 4.9)):
                recovered = canvas.floor_at(canvas.project(x, 0, z))
                assert recovered["x"] == pytest.approx(x) and recovered["z"] == pytest.approx(z)
    finally:
        window.close()
        window.deleteLater()
