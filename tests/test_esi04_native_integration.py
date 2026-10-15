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


@pytest.mark.integration
def test_native_audio_meter_opens_visible_mouth_and_closes_in_silence(tmp_path):
    if not importlib.util.find_spec("live2d"):
        pytest.skip("live2d-py is required")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("An X11 rendering display is required")
    script = textwrap.dedent("""
        import os, sys, time, threading, traceback
        import numpy as np
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication
        from esi04.config import Settings
        from esi04.gui import Window
        from esi04.runtime import Runtime
        from esi04.session import Session
        app = QApplication([])
        failures, counts, images = [], [], []
        def fail(kind, value, trace):
            traceback.print_exception(kind, value, trace)
            failures.append(str(value)); app.quit()
        sys.excepthook = fail
        root = Path(os.environ['ESI04_NATIVE_SAVE'])
        session = Session(root/'lips.sqlite3', Settings(playback=False, idle_deliberation=False))
        session.experience.state.preferences.autonomous_motion = False
        session.experience.state.preferences.proactive = False
        session.experience.state.preferences.idle_activity = False
        window = Window(session)
        window.on_start.setChecked(False)
        window.desktop.avatar.set_width(600)
        window.show()
        window.desktop_avatar.setChecked(True)
        runtime = Runtime(session)
        runtime.start()
        window.runtime = runtime
        window.refresh()
        stop = threading.Event()
        levels = iter([0, .6, 1, 0])
        def step():
            canvas = window.desktop.avatar.canvas
            assert canvas.model, window.desktop.avatar.problem
            assert canvas.meter is runtime.playback_meter, 'Renderer disconnected from the active output meter'
            canvas.model.SetAutoBlink(False)
            canvas.model.SetAutoBreath(False)
            try:
                opening = next(levels)
            except StopIteration:
                assert max(counts[1:3]) > counts[0]+6, counts
                assert np.array_equal(images[0], images[-1]), 'Mouth did not return to its closed drawing'
                stop.set()
                assert runtime.playback_meter.level() == 0
                window.close(); window.deleteLater()
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                app.quit(); return
            token = runtime.playback_meter.begin(stop)
            runtime.playback_meter.publish(token, time.monotonic(), 2,
                np.full((512, 1), opening/7+.003 if opening else 0))
            def sample():
                frame = canvas.grabFramebuffer()
                w, h = frame.width(), frame.height()
                mouth = frame.copy(round(w*.47), round(h*.16), round(w*.06), round(h*.03))
                mouth = mouth.convertToFormat(QImage.Format_RGBA8888)
                pixels = np.array(mouth.constBits()).reshape(mouth.height(), mouth.bytesPerLine())
                pixels = pixels[:, :mouth.width()*4].reshape(mouth.height(), mouth.width(), 4).copy()
                images.append(pixels)
                dark = (pixels[:,:,:3].mean(axis=2)<170) & (pixels[:,:,3]>180)
                counts.append(int(dark.sum()))
                frame.save(str(root/f'actual-meter-mouth-{len(counts)}.png'))
                step()
            QTimer.singleShot(180, sample)
        QTimer.singleShot(1200, step)
        QTimer.singleShot(12000, app.quit)
        app.exec()
        assert not failures and len(counts)==4, failures or counts
        print('Actual desktop meter, visible mouth pixels and silence:', counts)
    """)
    platform = "windows" if os.name == "nt" else "cocoa" if sys.platform == "darwin" else "xcb"
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script],
                            env={**os.environ, "QT_QPA_PLATFORM": platform, "ESI04_NATIVE_SAVE": str(tmp_path)},
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout+result.stderr
