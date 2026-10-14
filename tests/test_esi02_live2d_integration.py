import json
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("ESI_LIVE2D_MODEL"), reason="Exported Cubism model required")
def test_native_render_tracks_scheduled_output_blinks_breathes_reloads_and_releases(tmp_path):
    script = textwrap.dedent("""
        import hashlib, json, os, sys, threading, time, traceback
        import numpy as np
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QApplication
        from esi02.desktop import AvatarWindow
        app = QApplication([])
        failures = []
        def callback_failed(kind, value, trace):
            traceback.print_exception(kind, value, trace)
            failures.append(str(value))
            app.quit()
        sys.excepthook = callback_failed
        window = AvatarWindow()
        window.set_model(os.environ['ESI_LIVE2D_MODEL'])
        window.show()
        frames, mouths, eyes, breaths, heights, hair = [], [], [], [], [], []
        stop = threading.Event()
        token = window.meter.begin(stop)
        def sample():
            canvas = window.canvas
            model = canvas.model
            assert model and model.IsV3()
            image = canvas.grabFramebuffer()
            assert not image.isNull()
            assert any(image.pixelColor(x, y).alpha() for x in range(0,image.width(),10)
                       for y in range(0,image.height(),10))
            frames.append(hashlib.sha256(bytes(image.constBits())).hexdigest())
            mouths.append(model.GetParamValueById('ParamMouthOpenY'))
            eyes.append(model.GetParamValueById('ParamEyeLOpen'))
            breaths.append(model.GetParamValueById('ParamBreath'))
            ids = model.GetDrawableIds()
            if 'Mouth_open' in ids:
                mouth_vertices = np.array(model.GetDrawableVertices(ids.index('Mouth_open'))).reshape(-1, 2)
                heights.append(float(np.ptp(mouth_vertices[:, 1])))
            if 'ParamHairFront' in model.GetParamIds():
                hair.append(model.GetParamValueById('ParamHairFront'))
            if len(frames) == 1:
                assert mouths[-1] == 0
                window.meter.publish(token, time.monotonic(), 3, np.full((512, 1), 0.2))
                QTimer.singleShot(400, sample)
            elif len(frames) == 2:
                assert mouths[-1] > 0.9
                if heights:
                    assert heights[-1] > heights[0] * 3, 'Audio level did not open the actual mouth mesh'
                stop.set()
                QTimer.singleShot(200, sample)
            elif len(frames) == 3:
                assert mouths[-1] == 0
                if heights:
                    assert heights[-1] < heights[1] * 0.6, 'Cancellation did not close the actual mouth mesh'
                window.set_model(None)
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                window.set_model(os.environ['ESI_LIVE2D_MODEL'])
                QTimer.singleShot(500, sample)
            elif len(frames) < 30:
                QTimer.singleShot(110, sample)
            else:
                assert len(set(frames)) > 1 and max(eyes) - min(eyes) > 0.05
                assert max(breaths) - min(breaths) > 0.05
                Path(os.environ['ESI_RENDER_RESULT']).write_text(json.dumps({
                    'mouths': mouths, 'blink_range': [min(eyes), max(eyes)],
                    'breath_range': [min(breaths), max(breaths)], 'reload': True,
                    'mouth_geometry_heights': heights,
                    'hair_range': [min(hair), max(hair)] if hair else None,
                    'model': Path(os.environ['ESI_LIVE2D_MODEL']).name,
                    'physical_audio_verified': False,
                }))
                window.release()
                window.close()
                window.deleteLater()
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                app.quit()
        QTimer.singleShot(500, sample)
        app.exec()
        assert not failures and len(frames) == 30, failures or 'Render callbacks did not finish'
    """)
    report = tmp_path / "render.json"
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        env={**os.environ, "QT_QPA_PLATFORM": "xcb", "ESI_RENDER_RESULT": str(report)},
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(report.read_text())["reload"]
