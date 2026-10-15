"""Native Cubism checks for ESI-04 expressions, arms, depth and blink restoration."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from esi02.desktop import AvatarWindow
from esi02.live2d import AudioLiveCanvas

PHASES = [
    ("neutral", {}), ("left_arm", {"ParamArmLZ": 10}), ("right_arm", {"ParamArmRZ": 10}),
    ("both_arms", {"ParamArmLZ": 10, "ParamArmRZ": 10}),
    ("left_arm_out", {"ParamArmLZ": -10}), ("right_arm_out", {"ParamArmRZ": -10}),
    ("both_arms_out", {"ParamArmLZ": -10, "ParamArmRZ": -10}),
    ("left_brow_lift", {"ParamBrowLY": 1}), ("right_brow_lift", {"ParamBrowRY": 1}),
    ("left_brow_angle", {"ParamBrowLAngle": 1}), ("right_brow_angle", {"ParamBrowRAngle": 1}),
    ("smile", {"ParamMouthForm": 1}), ("frown", {"ParamMouthForm": -1}),
    ("speaking", {"ParamMouthOpenY": 1}),
    ("left_blink", {"ParamEyeLOpen": 0}), ("right_blink", {"ParamEyeROpen": 0}),
    ("both_closed", {"ParamEyeLOpen": 0, "ParamEyeROpen": 0}),
    ("mid_blink", {"ParamEyeLOpen": 0.5, "ParamEyeROpen": 0.5}),
    ("reopened", {}), ("head", {"ParamAngleX": 30}), ("body", {"ParamBodyAngleX": 10}),
    ("hair", {"ParamHairFront": 1}),
]
ONLY = {"left_arm": {"Arm_viewer_left"}, "right_arm": {"Arm_viewer_right"},
        "both_arms": {"Arm_viewer_left", "Arm_viewer_right"},
        "left_arm_out": {"Arm_viewer_left"}, "right_arm_out": {"Arm_viewer_right"},
        "both_arms_out": {"Arm_viewer_left", "Arm_viewer_right"},
        "left_brow_lift": {"Brow_viewer_left"}, "right_brow_lift": {"Brow_viewer_right"},
        "left_brow_angle": {"Brow_viewer_left"}, "right_brow_angle": {"Brow_viewer_right"},
        "smile": {"Mouth_closed"}, "frown": {"Mouth_closed"}}


def visible_mouth_pixels(frame):
    w, h = frame.width(), frame.height()
    mouth = frame.copy(round(w*.47), round(h*.16), round(w*.06), round(h*.03))
    mouth = mouth.convertToFormat(QImage.Format_RGBA8888)
    values = np.array(mouth.constBits()).reshape(mouth.height(), mouth.bytesPerLine())
    values = values[:, :mouth.width()*4].reshape(mouth.height(), mouth.width(), 4)
    return int(((values[:, :, :3].mean(axis=2)<170) & (values[:, :, 3]>180)).sum())


class FixedCanvas(AudioLiveCanvas):
    pose = {}

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.SetAutoBlink(False)
        self.model.SetAutoBreath(False)
        self.model.Update()
        for identifier in self.parameters:
            self.model.SetParamById(identifier, 1 if identifier in {"ParamEyeLOpen", "ParamEyeROpen"} else 0)
        for identifier, value in self.pose.items():
            self.model.SetParamById(identifier, value)
        self.apply_blink_visibility()
        self.model.Draw()


def verify(path, output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    window = AvatarWindow("ESI-04")
    window.set_model(path)
    old = window.canvas
    old.release()
    canvas = FixedCanvas(window.model_path, window, window.meter)
    window.layout().replaceWidget(old, canvas)
    old.hide()
    old.deleteLater()
    window.canvas = canvas
    window.set_width(450)
    window.show()
    baseline, results, failures = {}, [], []

    def sample():
        try:
            phase, pose = PHASES[len(results)]
            model = canvas.model
            assert model and model.IsV3(), "Native Cubism did not initialize"
            assert {"ParamArmLZ", "ParamArmRZ", "ParamMouthForm", "ParamBrowLY", "ParamBrowRY"} <= canvas.parameters
            assert len(canvas.parameters) == 29
            frame = canvas.grabFramebuffer()
            assert not frame.isNull()
            frame.save(str(output/(phase+".png")))
            names = model.GetDrawableIds()
            assert len(names) == 29 and len(set(names)) == 29, names
            vertices = {name: np.array(model.GetDrawableVertices(i), dtype=float).reshape(-1,2)
                        for i,name in enumerate(names)}
            assert all(np.isfinite(value).all() and len(value)>=3 for value in vertices.values())
            digest = hashlib.sha256(bytes(frame.constBits())).hexdigest()
            if phase == "neutral":
                baseline.update(vertices=vertices, digest=digest, mouth_pixels=visible_mouth_pixels(frame))
                assert frame.pixelColor(0, 0).alpha() == 0
                for side in ("left", "right"):
                    opened = vertices[f"Eye_viewer_{side}_open"]
                    closed = vertices[f"Eye_viewer_{side}_closed"]
                    assert np.ptp(closed[:,0]) <= np.ptp(opened[:,0])
                    assert abs(closed[:,0].mean()-opened[:,0].mean()) < 0.01
            changed = {name for name in names if not np.allclose(vertices[name],baseline["vertices"][name],atol=1e-7,rtol=0)}
            if phase in ONLY:
                assert changed == ONLY[phase], f"{phase}: expected {ONLY[phase]}, got {changed}"
                assert digest != baseline["digest"], phase+" changed no pixels"
            if phase == "speaking":
                assert visible_mouth_pixels(frame) > baseline["mouth_pixels"]+2, "The mouth mesh moved but was hidden behind another drawing"
            if phase in {"left_arm", "right_arm", "both_arms", "left_arm_out", "right_arm_out", "both_arms_out"}:
                assert all(frame.pixelColor(x, y).alpha() == 0
                           for x in (0, frame.width()-1) for y in range(frame.height())), "Hand clipped at canvas edge"
            if phase in {"left_blink", "right_blink", "both_closed", "mid_blink"}:
                assert not changed, phase+" moved a blink mesh"
                assert digest != baseline["digest"]
                if phase == "both_closed":
                    baseline["closed_digest"] = digest
                if phase == "mid_blink":
                    assert digest == baseline["closed_digest"], "Mid-blink mixed the drawings"
            if phase == "reopened":
                assert not changed and digest == baseline["digest"], "Blink did not restore neutral pixels"
            if phase in {"head", "body", "hair"}:
                assert changed and digest != baseline["digest"], phase+" did not deform"
            results.append({"phase": phase, "changed": sorted(changed), "digest": digest})
            frame.save(str(output/(phase+".png")))
            if len(results) == len(PHASES):
                app.quit()
            else:
                canvas.pose = PHASES[len(results)][1]
                canvas.update()
                QTimer.singleShot(220, sample)
        except Exception as exc:
            failures.append(str(exc))
            results.append({"phase": PHASES[len(results)][0], "failed": str(exc)})
            if len(results) < len(PHASES):
                canvas.pose = PHASES[len(results)][1]
                canvas.update()
                QTimer.singleShot(220, sample)
            else:
                app.quit()
    QTimer.singleShot(850,sample)
    QTimer.singleShot(25000, app.quit)
    app.exec()
    window.release()
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    report={"renderer":"live2d-py","model":str(path),"phases":results,"failures":failures,
            "physical_windows_checked":False,"physical_audio_checked":False}
    (output/"geometry.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if failures or len(results)!=len(PHASES):
        raise RuntimeError(failures or "Native validation timed out")
    print("ESI-04 native expressions, poses and blink checks passed.")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    arguments=parser.parse_args()
    verify(arguments.model.resolve(strict=True),arguments.output)
