import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from esi02.avatar_assets import BUNDLED_MODEL, bundled_model_path, model_file
from esi02.desktop_preferences import read_preferences


def test_bundled_export_has_local_resources_and_blink_lip_sync_physics_groups(tmp_path):
    path = bundled_model_path()
    model = json.loads(path.read_text())
    assert model["Version"] == 3
    references = model["FileReferences"]
    for name in [references["Moc"], *references["Textures"], references["Physics"], references["DisplayInfo"]]:
        target = (path.parent / name).resolve(strict=True)
        assert target.is_relative_to(path.parent) and target.is_file()
    assert (path.parent / references["Moc"]).read_bytes().startswith(b"MOC3")
    groups = {group["Name"]: group["Ids"] for group in model["Groups"]}
    assert set(groups["EyeBlink"]) == {"ParamEyeLOpen", "ParamEyeROpen"}
    assert groups["LipSync"] == ["ParamMouthOpenY"]
    physics = json.loads((path.parent / references["Physics"]).read_text())
    assert physics["Meta"]["TotalOutputCount"] == 2
    assert {
        output["Destination"]["Id"] for setting in physics["PhysicsSettings"] for output in setting["Output"]
    } == {"ParamHairFront", "ParamHairSide"}
    preferences = read_preferences(tmp_path / "not-created.json")
    assert preferences.model_path == BUNDLED_MODEL
    assert model_file(preferences.model_path) == path
    assert not (tmp_path / "not-created.json").exists()


@pytest.mark.integration
def test_native_bundled_character_has_real_deformation_and_transparent_rendering(tmp_path):
    if not importlib.util.find_spec("live2d"):
        pytest.skip("live2d-py is required")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("An X11 rendering display is required")
    root = Path(__file__).resolve().parents[1]
    platform = "windows" if os.name == "nt" else "cocoa" if sys.platform == "darwin" else "xcb"
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(root / "scripts/validate-rinne-model.py"),
         "--output", str(tmp_path / "native")],
        env={**os.environ, "QT_QPA_PLATFORM": platform},
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "native/geometry.json").read_text())
    assert len(report["phases"]) == 11
    assert report["physical_audio_verified"] is False


@pytest.mark.integration
def test_fresh_desktop_loads_bundled_model_survives_minimizing_and_preserves_still_choice(tmp_path):
    if not importlib.util.find_spec("live2d"):
        pytest.skip("live2d-py is required")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("An X11 rendering display is required")
    script = textwrap.dedent("""
        import json, os, sys, traceback
        from pathlib import Path
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QApplication
        from esi.avatar import StillCanvas
        from esi02.avatar_assets import BUNDLED_MODEL
        from esi02.config import Settings
        from esi02.desktop_preferences import read_preferences
        from esi02.gui import Window
        from esi02.session import Session
        app = QApplication([])
        failures, completed = [], []
        def fail(kind, value, trace):
            traceback.print_exception(kind, value, trace)
            failures.append(str(value)); app.quit()
        sys.excepthook = fail
        window = Window(Session(Path(os.environ['ESI_FRESH_RENDER']) / 'save.sqlite3', Settings(playback=False)))
        window.on_start.setChecked(False)
        window.show()
        window.desktop_avatar.setChecked(True)
        def check():
            assert window.desktop.preferences.model_path == BUNDLED_MODEL
            canvas = window.desktop.avatar.canvas
            assert canvas.model and canvas.model.IsV3()
            assert not canvas.grabFramebuffer().isNull()
            window.showMinimized()
            QTimer.singleShot(150, minimized)
        def minimized():
            assert window.isMinimized() and window.desktop.avatar.isVisible()
            canvas = window.desktop.avatar.canvas
            assert canvas.model and not canvas.grabFramebuffer().isNull()
            window.showNormal()
            window.still_button.click()
            assert isinstance(window.desktop.avatar.canvas, StillCanvas)
            assert read_preferences(window.desktop.path).model_path is None
            window.bundled_model_button.click()
            assert read_preferences(window.desktop.path).model_path == BUNDLED_MODEL
            QTimer.singleShot(300, finish)
        def finish():
            assert window.desktop.avatar.canvas.model
            completed.append(True)
            window.close(); window.deleteLater()
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            app.quit()
        QTimer.singleShot(650, check)
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert not failures and completed, failures or 'Fresh desktop check did not finish'
    """)
    platform = "windows" if os.name == "nt" else "cocoa" if sys.platform == "darwin" else "xcb"
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        env={**os.environ, "QT_QPA_PLATFORM": platform, "ESI_FRESH_RENDER": str(tmp_path)},
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
