import json
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ESI_LIVE2D_MODEL"), reason="Set ESI_LIVE2D_MODEL to a real exported Cubism model"
)
def test_real_cubism_render_animation_reload_and_clean_shutdown(tmp_path):
    # A subprocess turns native crashes into test failures instead of killing pytest.
    script = textwrap.dedent("""
        import hashlib, json, os
        from pathlib import Path
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer, QEvent
        from esi.avatar import AvatarWindow
        app = QApplication([])
        window = AvatarWindow({})
        window.set_model(os.environ['ESI_LIVE2D_MODEL'])
        window.show()
        frames = []
        parameter_values = []
        def sample():
            model = window.canvas.model
            assert model and model.IsV3() and model.GetParamCount() > 0
            image = window.canvas.grabFramebuffer()
            assert not image.isNull()
            assert any(image.pixelColor(x, y).alpha() for x in range(0,image.width(),10)
                       for y in range(0,image.height(),10))
            frames.append(hashlib.sha256(bytes(image.constBits())).hexdigest())
            parameter_values.append(model.GetParamValueById('ParamMouthOpenY'))
            if len(frames) == 1:
                window.canvas.phase = 'playing'
                QTimer.singleShot(700, sample)
            elif len(frames) == 2:
                window.set_model(None)
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                window.set_model(os.environ['ESI_LIVE2D_MODEL'])
                QTimer.singleShot(700, sample)
            else:
                assert len(set(frames)) > 1 and parameter_values[1] > 0
                Path(os.environ['ESI_LIVE2D_RESULT']).write_text(json.dumps({
                    'model_parameters': model.GetParamCount(), 'frame_hashes': frames,
                    'mouth_values': parameter_values, 'reload': True
                }))
                window.shutdown()
                app.quit()
        QTimer.singleShot(700, sample)
        app.exec()
    """)
    result_path = tmp_path / "render.json"
    environment = {**os.environ, "ESI_LIVE2D_RESULT": str(result_path)}
    # Linux needs a real GL-capable display, such as the test Xvfb server.
    environment["QT_QPA_PLATFORM"] = "xcb" if sys.platform.startswith("linux") else "windows"
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result_path.read_text())
    assert report["reload"] and report["model_parameters"] > 0
