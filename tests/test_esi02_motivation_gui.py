import pytest

from esi02.config import Settings
from esi02.session import Session

_app = None


@pytest.fixture
def app(monkeypatch):
    global _app
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    return _app


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def window_with_request(tmp_path, monkeypatch):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    session = Session(tmp_path / "s.sqlite3", Settings(playback=False))
    session.world_operation("environment", {"temperature": 39.0})
    session.advance_world(30, autonomy=True, allow_requests=True)
    return gui.Window(session)


def test_optional_request_buttons_change_only_virtual_world_then_show_observed_learning(
    app, tmp_path, monkeypatch
):
    window = window_with_request(tmp_path, monkeypatch)
    try:
        window.show()
        app.processEvents()
        assert window.request_card.isVisible()
        assert "対応は任意" in window.request_text.text()
        window.request_accept.click()
        assert window.session.world.room.temperature == 23
        assert "身体の変化を観測中" in window.request_text.text()
        assert not window.request_accept.isEnabled()
        assert window.session.brain.state.estimates["ask_cool"].count == 0
        window.session.advance_world(15, autonomy=False)
        window.refresh()
        assert not window.request_card.isVisible()
        assert window.session.brain.state.estimates["ask_cool"].count == 1
        assert "学習しました" in window.motivation_panel.records.toPlainText()
        assert window.motivation_panel.needs["comfort"].value() < 20
    finally:
        dispose(window, app)


def test_later_and_autonomy_off_are_saved_and_displayed(app, tmp_path, monkeypatch):
    window = window_with_request(tmp_path, monkeypatch)
    path = window.session.store.path
    try:
        window.request_later.click()
        assert window.session.brain.active is None
        assert window.session.brain.state.cooldowns["ask_cool"] >= window.session.world.elapsed + 900
        assert "お願いを控えます" in window.motivation_panel.records.toPlainText()
        window.motivation_panel.enabled.setChecked(False)
        assert not window.session.brain.state.enabled
        assert "自発的な行動OFF" in window.body_action.text()
    finally:
        dispose(window, app)
    restored = Session(path)
    try:
        assert not restored.brain.state.enabled
        assert restored.brain.state.estimates["ask_cool"].count == 1
    finally:
        restored.close()


def test_stop_failure_preserves_session_and_reports_error_in_window(app, tmp_path, monkeypatch):
    window = window_with_request(tmp_path, monkeypatch)

    class Stuck:
        def stop(self):
            raise RuntimeError("native operation still running")

    running = Stuck()
    window.runtime = running
    try:
        window.toggle()
        assert window.runtime is running
        assert "native operation" in window.error.text()
        assert window.session.store.meta("agent_id")
    finally:
        window.runtime = None
        dispose(window, app)
