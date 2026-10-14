import shutil
import threading
import time
from pathlib import Path

import pytest

from esi02.session import Session as PreviousSession


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    return QApplication.instance() or QApplication([])


class ServicesStub:
    def __init__(self, root, *args):
        self.root = root
        self.cancel = threading.Event()
        self.backend = {"device": "unknown", "verified": False}

    def close(self):
        self.cancel.set()


def setup_root(root):
    (root / "config").mkdir()
    source = Path(__file__).resolve().parents[1] / "config/esi03-windows.toml"
    shutil.copy2(source, root / "config/esi03-windows.toml")


def test_offline_start_migrates_in_worker_and_keeps_old_character(app, tmp_path):
    from esi03.launcher import Launcher

    setup_root(tmp_path)
    old_path = tmp_path / "data/esi02/session.sqlite3"
    old = PreviousSession(old_path)
    old.create_turn("星砂村は月の国です。")
    old.close()
    old_bytes = old_path.read_bytes()
    launcher = Launcher(tmp_path, services_factory=ServicesStub)
    try:
        assert launcher.settings().release == "ESI-03"
        assert not launcher.save_path.exists()  # Readiness inspection does not migrate or create.
        launcher.open_offline()
        deadline = time.monotonic() + 5
        while launcher.window is None and time.monotonic() < deadline:
            app.processEvents()
            threading.Event().wait(0.01)
        assert launcher.window is not None
        assert launcher.window.session.knowledge.entries()[0]["value"] == "月の国"
        assert not launcher.window.connection_ready
        assert old_path.read_bytes() == old_bytes
        assert (launcher.data / "session.esi02-original.sqlite3").is_file()
    finally:
        if launcher.window:
            launcher.window.close()
            launcher.window.deleteLater()
        launcher.shutdown()
        launcher.close()
        launcher.deleteLater()
        app.processEvents()


def test_bootstrap_captures_errors_and_names_the_correct_version(tmp_path):
    from esi03 import bootstrap

    def broken(root):
        raise RuntimeError("injected startup failure")

    assert bootstrap.main(tmp_path, broken) == 1
    text = (tmp_path / "data/esi03/logs/bootstrap.log").read_text()
    assert "ESI-03 startup" in text
    assert "injected startup failure" in text
