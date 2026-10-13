import json
import os
import threading
import time

import pytest

from esi02.config import Settings
from esi02.desktop_preferences import DesktopPreferences, read_preferences, write_preferences
from esi02.session import Session

_app = None


@pytest.fixture
def app(monkeypatch):
    global _app
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    return _app


def make_window(tmp_path, monkeypatch):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    session.motivation_operation("enabled", False)
    window = gui.Window(session)
    window.on_start.setChecked(False)
    return window


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def until(app, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("GUI/runtime did not reach expected state")


def test_round_control_starts_one_shared_runtime_and_operates_while_main_minimized(
    app, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from esi02 import gui
    from esi02.runtime import Runtime

    captures, runtimes = [], []

    class Capture:
        def __init__(self, *args):
            self.stop_event = threading.Event()
            self.capture_id = str(len(captures))
            captures.append(self)

        def start(self):
            pass

        def close(self):
            self.stop_event.set()

    monkeypatch.setattr("esi02.runtime.Microphone", Capture)

    def runtime_factory(*args):
        runtime = Runtime(*args)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(gui, "Runtime", runtime_factory)
    window = make_window(tmp_path, monkeypatch)
    try:
        window.show()
        window.desktop_mic.setChecked(True)
        window.desktop_avatar.setChecked(True)
        app.processEvents()
        microphone, avatar = window.desktop.microphone, window.desktop.avatar
        assert microphone.parent() is None and avatar.parent() is None
        assert microphone.windowFlags() & Qt.WindowStaysOnTopHint
        assert microphone.mask().contains(microphone.rect().center())
        assert not microphone.mask().contains(microphone.rect().topLeft())
        window.showMinimized()
        app.processEvents()
        assert window.isMinimized() and microphone.isVisible() and avatar.isVisible()
        QTest.mouseClick(microphone, Qt.LeftButton, pos=microphone.rect().center())
        until(app, lambda: window.runtime and window.runtime.microphone)
        window.refresh()
        assert len(runtimes) == 1 and microphone.microphone and microphone.running
        assert window.on_start.isChecked() is False
        assert window.start_button.text() == "停止"
        elapsed = window.session.elapsed
        until(app, lambda: window.session.elapsed > elapsed + 0.1)
        microphone.mic_action.trigger()
        until(app, lambda: window.runtime.microphone is None)
        window.refresh()
        assert not microphone.microphone and window.runtime is runtimes[0]
        microphone.mic_action.trigger()
        until(app, lambda: len(captures) == 2 and window.runtime.microphone is captures[1])
        microphone.main_action.trigger()
        assert not window.isMinimized() and window.isVisible()
        microphone.start_action.trigger()
        assert window.runtime is None and not runtimes[0].thread.is_alive()
        assert not microphone.running and not microphone.microphone
        assert captures[1].stop_event.is_set()
        assert microphone.isVisible() and avatar.isVisible()
    finally:
        dispose(window, app)


def test_avatar_can_drag_offscreen_scale_and_reload_without_position_correction(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window = make_window(tmp_path, monkeypatch)
    path = window.session.store.path
    try:
        window.show()
        window.desktop_avatar.setChecked(True)
        avatar = window.desktop.avatar
        app.processEvents()
        origin = avatar.pos()
        pointer = QPointF(origin.x() + 20, origin.y() + 20)
        press = QMouseEvent(
            QEvent.MouseButtonPress, QPointF(20, 20), pointer, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
        )
        QApplication = type(app)
        QApplication.sendEvent(avatar.canvas, press)
        destination = QPointF(-2500, 3600)
        move = QMouseEvent(
            QEvent.MouseMove, QPointF(20, 20), destination, Qt.NoButton, Qt.LeftButton, Qt.NoModifier
        )
        QApplication.sendEvent(avatar.canvas, move)
        release = QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(20, 20), destination, Qt.LeftButton, Qt.NoButton, Qt.NoModifier
        )
        QApplication.sendEvent(avatar.canvas, release)
        offscreen = [avatar.x(), avatar.y()]
        assert offscreen == [-2520, 3580]
        for _ in range(4):
            avatar.plus.click()
        width = avatar.width()
        assert width == 420
        assert [avatar.x(), avatar.y()] == offscreen
        window.session.status["phase"] = "generating"
        window.refresh()
        app.processEvents()
        assert avatar.width() == width  # Long activity labels must not resize the character.
        avatar.hide_button.click()
        assert not avatar.isVisible() and not window.desktop_avatar.isChecked()
        window.desktop_avatar.setChecked(True)
        window.desktop.microphone.move(5000, -2400)
        window.desktop.flush()
        assert read_preferences(window.desktop.path).avatar_position == offscreen
    finally:
        dispose(window, app)
    restored = gui_window = None
    try:
        from esi02.gui import Window

        restored = Session(path)
        gui_window = Window(restored)
        gui_window.show()
        app.processEvents()
        avatar = gui_window.desktop.avatar
        assert [avatar.x(), avatar.y()] == offscreen and avatar.width() == width
        assert [gui_window.desktop.microphone.x(), gui_window.desktop.microphone.y()] == [5000, -2400]
        assert gui_window.desktop_avatar.isChecked()
        gui_window.desktop.reset_position("avatar")
        assert [avatar.x(), avatar.y()] != offscreen
        assert avatar.width() == width  # Manual return changes position only.
    finally:
        if gui_window:
            dispose(gui_window, app)
        elif restored:
            restored.close()


def test_microphone_drag_is_not_a_click_and_visibility_is_independent(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QSignalSpy

    window = make_window(tmp_path, monkeypatch)
    try:
        window.desktop_mic.setChecked(True)
        microphone = window.desktop.microphone
        pointer = QPointF(microphone.x() + 36, microphone.y() + 36)
        clicked = QSignalSpy(microphone.clicked)
        app.sendEvent(
            microphone,
            QMouseEvent(
                QEvent.MouseButtonPress, QPointF(36, 36), pointer, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
            ),
        )
        app.sendEvent(
            microphone,
            QMouseEvent(
                QEvent.MouseMove,
                QPointF(36, 36),
                pointer + QPointF(300, 200),
                Qt.NoButton,
                Qt.LeftButton,
                Qt.NoModifier,
            ),
        )
        app.sendEvent(
            microphone,
            QMouseEvent(
                QEvent.MouseButtonRelease,
                QPointF(36, 36),
                pointer + QPointF(300, 200),
                Qt.LeftButton,
                Qt.NoButton,
                Qt.NoModifier,
            ),
        )
        assert clicked.count() == 0 and window.runtime is None
        assert not window.desktop.avatar.isVisible()
        microphone.hide_action.trigger()
        assert not microphone.isVisible() and not window.desktop_mic.isChecked()
    finally:
        dispose(window, app)


def test_fatal_runtime_is_reflected_and_can_restart_from_floating_menu(app, tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    original = window.session.advance_world

    def broken(*args, **kwargs):
        raise ValueError("test: virtual world error")

    window.session.advance_world = broken
    try:
        window.toggle()
        runtime = window.runtime
        until(app, lambda: not runtime.thread.is_alive())
        window.refresh()
        assert window.runtime is None and window.start_button.text() == "開始"
        assert window.reload.isEnabled() and not window.mic_button.isEnabled()
        assert "virtual world error" in window.error.text()
        assert "virtual world error" in window.desktop.microphone.toolTip()
        window.session.advance_world = original
        window.desktop.microphone.start_action.trigger()
        assert window.runtime is not runtime and window.start_button.text() == "停止"
        assert not window.error.text() and not window.desktop.microphone.problem
    finally:
        dispose(window, app)


def test_save_switch_flushes_old_views_and_loads_new_independent_layout(app, tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    other = tmp_path / "other" / "selected.sqlite3"
    other.parent.mkdir()
    Session(other, Settings(playback=False)).close()
    write_preferences(
        other.parent / "esi02-desktop.json",
        DesktopPreferences(microphone_visible=True, microphone_position=[-4000, 20], avatar_width=150),
    )
    try:
        old = window.desktop
        window.desktop_avatar.setChecked(True)
        old.avatar.move(-1000, -2000)
        window.switch_save(other)
        app.processEvents()
        assert old.closed and not old.timer.isActive()
        assert read_preferences(tmp_path / "esi02-desktop.json").avatar_position == [-1000, -2000]
        assert window.desktop is not old and window.desktop_mic.isChecked()
        assert window.desktop.microphone.x() == -4000 and window.desktop.avatar.width() == 150
        assert not window.desktop_avatar.isChecked()
    finally:
        dispose(window, app)


def test_stop_failure_does_not_switch_save_or_close_views(app, tmp_path, monkeypatch):
    window = make_window(tmp_path, monkeypatch)
    original = window.session
    other = tmp_path / "selected.sqlite3"
    Session(other, Settings(playback=False)).close()

    class Stuck:
        def stop(self):
            raise RuntimeError("still running")

    window.runtime = Stuck()
    try:
        window.desktop_mic.setChecked(True)
        with pytest.raises(RuntimeError, match="停止できない"):
            window.switch_save(other)
        assert window.session is original and not window.desktop.closed
        assert window.desktop.microphone.isVisible()
        assert original.store.meta("agent_id")
    finally:
        window.runtime = None
        dispose(window, app)


def test_bad_display_preferences_preserved_and_write_error_visible(app, tmp_path, monkeypatch):
    from esi02 import desktop

    path = tmp_path / "esi02-desktop.json"
    original = b'{"avatar_position": [true, 100]}'
    path.write_bytes(original)
    window = make_window(tmp_path, monkeypatch)
    try:
        window.desktop_avatar.setChecked(True)
        window.desktop.flush()
        window.refresh()
        assert path.read_bytes() == original
        assert "元ファイルは保持" in window.error.text()
    finally:
        dispose(window, app)
    path.unlink()
    window = make_window(tmp_path, monkeypatch)

    def write_error(*args):
        raise OSError("disk full")

    monkeypatch.setattr(desktop, "write_preferences", write_error)
    try:
        window.desktop.flush()
        window.refresh()
        assert "disk full" in window.error.text()
        assert "disk full" in window.desktop.microphone.toolTip()
    finally:
        dispose(window, app)


@pytest.mark.parametrize(
    "bad",
    [
        {"avatar_width": float("nan")},
        {"microphone_position": [True, 20]},
        {"avatar_position": [10]},
        {"avatar_visible": "true"},
        {"version": 2},
    ],
)
def test_display_preferences_reject_invalid_data(tmp_path, bad):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        read_preferences(path)


def test_activity_icons_distinguish_listening_thinking_voice_and_body_states():
    from esi02.presentation import BODY_SYMBOLS, activity

    assert activity({"phase": "waiting", "audio_input": {"asr_busy": True}})[0] == "ear"
    assert activity({"phase": "generating"})[0] == "think"
    assert activity({"phase": "playing"})[0] == "speak"
    assert BODY_SYMBOLS["drink"] == "water" and BODY_SYMBOLS["rest"] == "rest"


def test_same_explicit_configuration_can_reopen_save_after_json_number_roundtrip(tmp_path):
    path = tmp_path / "same-settings.sqlite3"
    settings = Settings(playback=False, tick_seconds=0.01)
    original = Session(path, settings)
    original.close()
    reopened = Session(path, settings)
    try:
        assert reopened.settings.fingerprint == Settings.model_validate(settings.model_dump()).fingerprint
    finally:
        reopened.close()


def test_speaker_test_uses_selected_device_without_starting_ai_or_creating_experience(
    app, tmp_path, monkeypatch
):
    from esi02 import gui

    window = make_window(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    calls = []
    device = {"index": 9, "name": "Speaker", "hostapi": "WASAPI", "inputs": 0, "outputs": 2}
    monkeypatch.setattr(gui, "audio_devices", lambda: [device])

    def tone(selected, stop):
        calls.append(selected)
        started.set()
        assert release.wait(2)
        return not stop.is_set()

    monkeypatch.setattr(gui, "test_tone", tone)
    try:
        window.refresh_devices()
        window.output_devices.setCurrentIndex(1)
        window.sound_test.click()
        assert started.wait(1) and calls == [9]
        window.desktop_microphone()  # The floating button also respects the routing-test lock.
        assert window.runtime is None and "音のテストが終わってから" in window.error.text()
        assert not window.output_devices.isEnabled()
        release.set()
        until(app, lambda: window.audio_test is None)
        assert window.output_devices.isEnabled()
        assert "テスト音を出力" in window.routing_hint.text()
        assert not window.session.turns and not window.session.memory.entries()
        assert sum(window.session.brain.view(window.session.world)["counts"].values()) == 0
        assert not window.error.text()
    finally:
        release.set()
        dispose(window, app)


def test_disappeared_speaker_is_reported_and_close_cancels_pending_test(app, tmp_path, monkeypatch):
    from esi02 import gui

    window = make_window(tmp_path, monkeypatch)
    started = threading.Event()
    stops = []
    device = {"index": 9, "name": "Speaker", "hostapi": "WASAPI", "inputs": 0, "outputs": 2}
    window.output_devices.addItem("Speaker", device)
    window.output_devices.setCurrentIndex(1)
    window.test_output()
    assert window.audio_test is None and "音声機器が見つからない" in window.error.text()
    monkeypatch.setattr(gui, "audio_devices", lambda: [device])

    def tone(selected, stop):
        stops.append(stop)
        started.set()
        assert stop.wait(2)
        return False

    monkeypatch.setattr(gui, "test_tone", tone)
    window.test_output()
    assert started.wait(1)
    future = window.audio_test
    dispose(window, app)
    assert stops[0].is_set() and future.result(timeout=2) is False
