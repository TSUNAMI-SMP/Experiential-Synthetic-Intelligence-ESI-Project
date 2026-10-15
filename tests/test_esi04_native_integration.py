"""Real Qt/OpenGL rendering, capture, summary bubble and revocable avatar movement."""

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
def test_native_esi04_avatar_room_screen_and_switching(tmp_path):
    if not importlib.util.find_spec("live2d"):
        pytest.skip("live2d-py is required")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("An X11 rendering display is required")
    script = textwrap.dedent("""
        import os, sys, time, traceback
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QApplication
        from esi04.config import Settings
        from esi04.gui import Window
        from esi04.session import Session
        from esi04.motion import AutonomousCanvas
        app = QApplication([])
        failures, completed = [], []
        root = Path(os.environ['ESI04_NATIVE_SAVE'])
        def fail(kind, value, trace):
            traceback.print_exception(kind, value, trace)
            failures.append(str(value)); app.quit()
        sys.excepthook = fail
        first = root / 'save.sqlite3'
        second = root / 'second.sqlite3'
        Session(second, Settings(playback=False, idle_deliberation=False)).close()
        window = Window(Session(first, Settings(playback=False, idle_deliberation=False)))
        window.on_start.setChecked(False)
        window.show()
        window.desktop_avatar.setChecked(True)
        window.session.cognition.intention = 'ask'
        window.session.cognition.emotion = 'pleased'
        window.session.cognition.emotion_strength = .9
        window.session.agency.summarize('部屋を確かめる', '机', '置いた物の位置を確認した', ['fixture:room'])
        window.refresh()
        def check():
            canvas = window.desktop.avatar.canvas
            assert isinstance(canvas, AutonomousCanvas)
            assert canvas.model and canvas.model.IsV3()
            assert 'ParamArmRZ' in canvas.parameters
            assert canvas.values['ParamArmRZ'] > 3
            assert not canvas.grabFramebuffer().isNull()
            assert 'rinne-el04' in str(canvas.path)
            assert window.desktop.bubble.isVisible()
            window.desktop.show_summaries()
            assert '部屋を確かめる' in window.desktop.summary_text.toPlainText()
            window.desktop.summary_dialog.hide()
            supervisor = window.computer_supervisor
            assert supervisor and not supervisor.hub.grant.observe
            screen = app.primaryScreen()
            supervisor.configure(screen=screen.name(), observe=True, control=True, move_avatar=True)
            foreground, elements = supervisor.metadata()
            supervisor.capture(foreground, elements, supervisor.hub.grant)
            frame = supervisor.hub.latest
            assert frame and Path(frame.path).is_file()
            point = frame.image_point(500, 450)
            supervisor.hub.attention = dict(generation=frame.generation, frame_id=frame.id,
                                            point=point, at=time.monotonic())
            window.desktop.choose_position(window.desktop.state())
            target = window.desktop.motion_target
            assert target is not None
            x, y = frame.logical_point(*point)
            avatar = window.desktop.avatar
            assert not (target.x()-24 <= x <= target.x()+avatar.width()+24
                        and target.y()-24 <= y <= target.y()+avatar.height()+24)
            supervisor.revoke()
            before = avatar.pos()
            window.desktop.advance_position()
            assert avatar.pos() == before and window.desktop.motion_target is None
            window.session.create_turn('星砂村は月の国です。')
            original_screen_root = supervisor.hub.memory.root
            window.switch_save(second)
            assert window.session.store.path == second
            assert not window.computer_supervisor.hub.grant.observe
            assert not window.computer_supervisor.hub.grant.control
            assert window.computer_supervisor.hub.memory.root != original_screen_root
            QTimer.singleShot(200, finish)
        def finish():
            window.close()
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            restored = Session(first)
            try:
                assert restored.knowledge.entries()[0]['value'] == '月の国'
                assert restored.cognition.summaries[-1].subject == '机'
            finally:
                restored.close()
            completed.append(True)
            app.quit()
        QTimer.singleShot(1300, check)
        QTimer.singleShot(20000, app.quit)
        app.exec()
        assert not failures and completed, failures or 'Native ESI-04 check did not finish'
    """)
    platform = "windows" if os.name == "nt" else "cocoa" if sys.platform == "darwin" else "xcb"
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script],
                            env={**os.environ, "QT_QPA_PLATFORM": platform, "ESI04_NATIVE_SAVE": str(tmp_path)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
