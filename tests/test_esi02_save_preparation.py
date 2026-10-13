import os
import threading
import time

import pytest

from esi02.config import Settings
from esi02.session import Session


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class Services:
    def __init__(self):
        self.cancel = threading.Event()
        self.notify = lambda message: None
        self.backend = {"device": "cpu", "verified": True}
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.fail_candidate = False
        self.fail_restore = False

    def prepare(self, settings):
        self.calls.append(settings.cpu_threads)
        if settings.cpu_threads == 3:
            self.started.set()
            assert self.release.wait(3), "test did not release preparation"
            if self.fail_candidate or self.cancel.is_set():
                raise RuntimeError("test: candidate connection failed")
        elif self.fail_restore:
            raise RuntimeError("test: previous connection failed")


def until(app, predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("preparation did not reach expected state")


def make_window(tmp_path, monkeypatch):
    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    old = Session(tmp_path / "old.sqlite3", Settings(cpu_threads=2, playback=False))
    selected = tmp_path / "new.sqlite3"
    Session(selected, Settings(cpu_threads=3, playback=False)).close()
    services = Services()
    window = gui.Window(old, services=services)
    window.on_start.setChecked(False)
    return window, old, selected, services


def dispose(window, services, app):
    from PySide6.QtCore import QEvent

    services.release.set()
    if window.preparation:
        until(app, lambda: window.preparation is None)
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_switch_keeps_event_loop_responsive_and_commits_only_after_ready(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QSignalSpy

    window, old, selected, services = make_window(tmp_path, monkeypatch)
    changed = QSignalSpy(window.save_changed)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    try:
        started = time.monotonic()
        window.switch_save(selected)
        assert time.monotonic() - started < 0.3
        assert services.started.wait(1)
        until(app, lambda: len(ticks) > 3)
        assert window.session is old and changed.count() == 0
        assert not window.start_button.isEnabled() and not window.open_save_button.isEnabled()
        window.desktop_microphone()
        assert window.runtime is None
        assert not window.world_action("move", [1, 1, 0])
        services.release.set()
        until(app, lambda: window.preparation is None)
        assert window.session.store.path == selected and changed.count() == 1
        assert window.connection_ready and window.start_button.isEnabled()
    finally:
        timer.stop()
        dispose(window, services, app)


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_switch_preserves_old_save_and_restores_services(
    app, tmp_path, monkeypatch, cancel
):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    services.fail_candidate = not cancel
    try:
        window.switch_save(selected)
        assert services.started.wait(1)
        if cancel:
            window.cancel_preparation()
        services.release.set()
        until(app, lambda: window.preparation is None)
        assert window.session is old and old.store.meta("agent_id")
        assert services.calls == [3, 2] and not services.cancel.is_set()
        assert window.connection_ready and window.start_button.isEnabled()
        assert window.ui_error
    finally:
        dispose(window, services, app)


def test_failed_rollback_disables_start_and_retry_recovers_same_session(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    services.fail_candidate = services.fail_restore = True
    try:
        window.switch_save(selected)
        services.release.set()
        until(app, lambda: window.preparation is None)
        assert window.session is old and not window.connection_ready
        assert not window.start_button.isEnabled() and not window.desktop.microphone.start_action.isEnabled()
        window.toggle()
        assert window.runtime is None
        services.fail_restore = False
        window.retry_connection()
        until(app, lambda: window.preparation is None)
        assert window.session is old and window.connection_ready and not window.ui_error
    finally:
        dispose(window, services, app)


def test_late_cancel_after_worker_completion_restores_old_settings(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    try:
        window.switch_save(selected)
        services.release.set()
        window.preparation["future"].result(timeout=3)
        window.cancel_preparation()  # No GUI poll between completion and cancellation.
        until(app, lambda: window.preparation is None)
        assert window.session is old and services.calls == [3, 2]
        assert window.connection_ready and "中止" in window.ui_error
    finally:
        dispose(window, services, app)


def test_final_checkpoint_error_preserves_old_session_and_rolls_back(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    checkpoint = old.checkpoint
    try:
        window.switch_save(selected)
        services.release.set()
        window.preparation["future"].result(timeout=3)

        def fail():
            raise OSError("test: disk full")

        old.checkpoint = fail
        until(app, lambda: window.preparation is None)
        assert window.session is old and old.store.meta("agent_id")
        assert services.calls == [3, 2] and "disk full" in window.ui_error
        assert window.connection_ready and not window.desktop.closed
    finally:
        old.checkpoint = checkpoint
        dispose(window, services, app)


def test_close_during_preparation_waits_before_closing_save(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    try:
        window.show()
        window.switch_save(selected)
        assert services.started.wait(1)
        assert not window.close()
        assert old.store.meta("agent_id") and window.closing
        services.release.set()
        until(app, lambda: window.preparation is None)
        until(app, lambda: not window.isVisible())
        assert window.desktop.closed and window.session is old
    finally:
        from PySide6.QtCore import QEvent

        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_stop_failure_keeps_runtime_and_does_not_prepare_other_services(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)

    class Stuck:
        def stop(self):
            raise RuntimeError("test: still running")

    running = Stuck()
    window.runtime = running
    try:
        window.switch_save(selected)
        until(app, lambda: window.preparation is None)
        assert window.session is old and window.runtime is running and not services.calls
        assert "停止できない" in window.ui_error
    finally:
        window.runtime = None
        dispose(window, services, app)


def test_close_checkpoint_error_keeps_save_and_views_available(app, tmp_path, monkeypatch):
    window, old, selected, services = make_window(tmp_path, monkeypatch)
    checkpoint = old.checkpoint

    def fail():
        raise OSError("test: disk full on close")

    try:
        window.show()
        window.set_desktop_visible("microphone", True)
        old.checkpoint = fail
        assert not window.close()
        assert window.timer.isActive() and not window.desktop.closed
        assert old.store.meta("agent_id") and window.desktop.microphone.isVisible()
        assert "disk full on close" in window.error.text()
        old.checkpoint = checkpoint
        assert window.close() and window.desktop.closed
    finally:
        from PySide6.QtCore import QEvent

        old.checkpoint = checkpoint
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
