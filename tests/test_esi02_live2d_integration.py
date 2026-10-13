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
        import hashlib, json, os, threading, time
        import numpy as np
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QApplication
        from esi02.desktop import AvatarWindow
        app = QApplication([])
        window = AvatarWindow()
        window.set_model(os.environ['ESI_LIVE2D_MODEL'])
        window.show()
        frames, mouths, eyes, breaths = [], [], [], []
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
            if len(frames) == 1:
                assert mouths[-1] == 0
                window.meter.publish(token, time.monotonic(), 3, np.full((512, 1), 0.2))
                QTimer.singleShot(400, sample)
            elif len(frames) == 2:
                assert mouths[-1] > 0.9
                stop.set()
                QTimer.singleShot(200, sample)
            elif len(frames) == 3:
                assert mouths[-1] == 0
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
                    'model': 'test model only, not completed Rinne El',
                }))
                window.release()
                window.close()
                window.deleteLater()
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                app.quit()
        QTimer.singleShot(500, sample)
        app.exec()
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
