"""Check the authored character's native geometry and render transparent previews."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication

from esi02.avatar_assets import bundled_model_path
from esi02.desktop import AvatarWindow
from esi02.live2d import AudioLiveCanvas

DEFAULTS = {
    "ParamEyeLOpen": 1, "ParamEyeROpen": 1, "ParamMouthOpenY": 0,
    "ParamAngleX": 0, "ParamAngleY": 0, "ParamAngleZ": 0,
    "ParamBodyAngleX": 0, "ParamBodyAngleZ": 0, "ParamBreath": 0,
    "ParamHairFront": 0, "ParamHairSide": 0,
}
BACK_HAIR = {f"Hair_back_{side}_{band}" for side in ("left", "right") for band in ("upper", "middle", "lower")}
FRONT_HAIR = {f"Hair_front_{name}" for name in ("crown", "bang_left", "bang_center", "side_left", "side_right")}
DRAWABLES = {
    "Body_base", "Halo", "Eye_viewer_left_open", "Eye_viewer_right_open",
    "Eye_viewer_left_closed", "Eye_viewer_right_closed", "Mouth_open",
    "Face_skin_separate", "Neck_base_original", "Ear_viewer_left", "Ear_viewer_right", "Collar_front",
    "Mouth_clear_patch",
    "Brow_viewer_left", "Brow_viewer_right",
} | BACK_HAIR | FRONT_HAIR
PHASES = [
    ("default", None, 0), ("mouth", "ParamMouthOpenY", 1),
    ("left_blink", "ParamEyeLOpen", 0), ("right_blink", "ParamEyeROpen", 0),
    ("left_mid_blink", "ParamEyeLOpen", 0.5), ("right_mid_blink", "ParamEyeROpen", 0.5),
    ("both_closed", "ParamEyeLOpen", 0), ("reopened", None, 0),
    ("horizontal", "ParamAngleX", 30), ("vertical", "ParamAngleY", 30),
    ("tilt", "ParamAngleZ", 30), ("breath", "ParamBreath", 1),
    ("body", "ParamBodyAngleX", 10), ("hair", "ParamHairFront", 1),
    ("halo", "ParamHairSide", 1),
]


class FixedCanvas(AudioLiveCanvas):
    """Isolate each authored parameter from cursor and automatic animation."""

    pose = dict(DEFAULTS)

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.SetAutoBlink(False)
        self.model.SetAutoBreath(False)
        self.model.Update()
        for name, value in self.pose.items():
            self.model.SetParamById(name, value)
        self.apply_blink_visibility()
        self.model.Draw()


def verify(model_path, output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    window = AvatarWindow()
    window.set_model(model_path)
    old = window.canvas
    old.release()
    canvas = FixedCanvas(window.model_path, window, window.meter)
    window.layout().replaceWidget(old, canvas)
    old.hide()
    old.deleteLater()
    window.canvas = canvas
    window.set_width(450)
    window.show()
    results, failures, baseline = [], [], {}

    def sample():
        try:
            phase, parameter, _ = PHASES[len(results)]
            model = canvas.model
            assert model and model.IsV3(), "Native Cubism model did not initialize"
            frame = canvas.grabFramebuffer()
            assert not frame.isNull(), "Framebuffer is empty"
            identifiers = model.GetDrawableIds()
            vertices = {
                name: np.array(model.GetDrawableVertices(index), dtype=float).reshape(-1, 2)
                for index, name in enumerate(identifiers)
            }
            assert set(identifiers) == DRAWABLES, "An authored layer is missing or a reference layer was exported"
            assert all(len(points) >= 3 for points in vertices.values()), "An ArtMesh is empty"
            assert all(np.isfinite(points).all() for points in vertices.values()), "Geometry is not finite"
            digest = hashlib.sha256(bytes(frame.constBits())).hexdigest()
            if phase == "default":
                baseline.update(vertices=vertices, digest=digest)
                left, right = vertices["Eye_viewer_left_open"], vertices["Eye_viewer_right_open"]
                # Keep the reference's naturally different left/right drawings.
                # Registration and visible lash widths are checked separately;
                # mirroring the original would change the character's expression.
                assert abs(np.ptp(left[:, 0]) - np.ptp(right[:, 0])) < 0.01, "Normal eye crops have different widths"
                assert abs(left[:, 1].mean() - right[:, 1].mean()) < 0.01, "Normal eye crops have different heights"
                for side in ("left", "right"):
                    opened = vertices[f"Eye_viewer_{side}_open"]
                    closed = vertices[f"Eye_viewer_{side}_closed"]
                    assert np.ptp(closed[:, 0]) <= np.ptp(opened[:, 0]), "Closed eye is wider than the open eye"
                    assert abs(closed[:, 0].mean() - opened[:, 0].mean()) < 0.01, "Blink is off its eye centre"
                    assert opened[:, 1].min() <= closed[:, 1].mean() <= opened[:, 1].max(), "Blink is outside its eye height"
                brow_left, brow_right = vertices["Brow_viewer_left"], vertices["Brow_viewer_right"]
                assert abs(brow_left[:, 1].mean() - brow_right[:, 1].mean()) < 0.01, "Brows are misregistered vertically"
                assert frame.pixelColor(0, 0).alpha() == 0, "Background is opaque"
                assert any(frame.pixelColor(x, y).alpha() for x in range(0, frame.width(), 10)
                           for y in range(0, frame.height(), 10)), "Character did not render"
            changed = {
                name for name in identifiers
                if not np.allclose(vertices[name], baseline["vertices"][name], atol=1e-7, rtol=0)
            }
            if phase not in {"default", "reopened"}:
                assert digest != baseline["digest"], f"{phase} did not change the rendered image"
            if phase == "reopened":
                assert not changed and digest == baseline["digest"], "Reopening did not restore the original face"
            if phase in {"mouth", "hair", "halo"}:
                expected = BACK_HAIR if phase == "hair" else {"Mouth_open" if phase == "mouth" else "Halo"}
                assert changed == expected, f"{phase} changed the wrong layers: {changed}"
            if phase in {"horizontal", "vertical", "tilt"}:
                assert changed == DRAWABLES - {"Body_base", "Collar_front"}, f"{phase} changed the wrong layers"
            if phase in {"breath", "body"}:
                assert changed == DRAWABLES, f"{phase} has no deformation or an unparented layer"
            if phase in {"left_blink", "right_blink", "left_mid_blink", "right_mid_blink", "both_closed"}:
                assert not changed, "Opacity-based blink moved the meshes"
                if "mid" in phase:
                    assert digest == baseline[phase.replace("mid_", "")], "Mid-blink mixed the two eye drawings"
                else:
                    baseline[phase] = digest
            results.append({"phase": phase, "changed": sorted(changed), "frame_changed": digest != baseline["digest"]})
            frame.save(str(output / f"{phase}.png"))
            if len(results) < len(PHASES):
                _, next_parameter, value = PHASES[len(results)]
                canvas.pose = dict(DEFAULTS)
                if next_parameter:
                    canvas.pose[next_parameter] = value
                if PHASES[len(results)][0] == "both_closed":
                    canvas.pose["ParamEyeROpen"] = 0
                canvas.update()
                QTimer.singleShot(180, sample)
            else:
                app.quit()
        except Exception as exc:
            failures.append(str(exc))
            app.quit()

    QTimer.singleShot(700, sample)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(app.quit)
    deadline.start(20000)
    app.exec()
    deadline.stop()
    window.release()
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    if failures or len(results) != len(PHASES):
        raise RuntimeError(failures or "Native validation did not complete")
    report = {
        "model": str(model_path), "physical_audio_verified": False,
        "renderer": "live2d-py", "phases": results,
    }
    (output / "geometry.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Native Rinne El geometry, opacity and transparent rendering checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=bundled_model_path())
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    verify(arguments.model.resolve(strict=True), arguments.output)
