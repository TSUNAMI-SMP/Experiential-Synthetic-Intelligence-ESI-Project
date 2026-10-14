import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
def test_native_esi03_avatar_growth_and_character_settings_survive_restart(tmp_path):
    if not importlib.util.find_spec("live2d"):
        pytest.skip("live2d-py is required")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("An X11 rendering display is required")
    script = textwrap.dedent("""
        import os, sys, traceback
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QApplication
        from esi03.config import Settings
        from esi03.gui import Window
        from esi03.session import Session
        app = QApplication([])
        failures, completed = [], []
        root = Path(os.environ['ESI03_NATIVE_SAVE'])
        def fail(kind, value, trace):
            traceback.print_exception(kind, value, trace)
            failures.append(str(value)); app.quit()
        sys.excepthook = fail
        window = Window(Session(root / 'save.sqlite3', Settings(playback=False)))
        window.on_start.setChecked(False)
        window.show()
        window.desktop_avatar.setChecked(True)
        def check():
            canvas = window.desktop.avatar.canvas
            assert canvas.model and canvas.model.IsV3()
            assert not canvas.grabFramebuffer().isNull()
            assert len(window.growth_panel.needs) == 14
            panel = window.growth_panel
            panel.name.setText('成長する子')
            panel.axes['traits', 'curiosity'].setValue(93)
            panel.save_character()
            window.session.create_turn('星砂村は月の国です。')
            panel.refresh()
            window.showMinimized()
            QTimer.singleShot(150, finish)
        def finish():
            assert window.isMinimized() and window.desktop.avatar.isVisible()
            assert window.desktop.avatar.canvas.model
            window.close()
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            restored = Session(root / 'save.sqlite3')
            try:
                assert restored.experience.state.character.name == '成長する子'
                assert restored.experience.state.character.traits.curiosity == 0.93
                assert restored.knowledge.entries()[0]['value'] == '月の国'
            finally:
                restored.close()
            completed.append(True)
            app.quit()
        QTimer.singleShot(800, check)
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert not failures and completed, failures or 'Native ESI-03 check did not finish'
    """)
    platform = "windows" if os.name == "nt" else "cocoa" if sys.platform == "darwin" else "xcb"
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        env={**os.environ, "QT_QPA_PLATFORM": platform, "ESI03_NATIVE_SAVE": str(tmp_path)},
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
