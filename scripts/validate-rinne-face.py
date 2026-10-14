"""Render an open/closed/reopened face comparison using the native app renderer."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from scipy.ndimage import find_objects, label

from esi02.avatar_assets import bundled_model_path
from esi02.playback import PlaybackMeter

spec = importlib.util.spec_from_file_location("rig_validation", Path(__file__).with_name("validate-rinne-model.py"))
rig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rig)


class FacePreview(rig.FixedCanvas):
    def paintGL(self):
        if self.model:
            self.model.SetScale(3)
            self.model.SetOffset(0, -2)
        super().paintGL()


def compare(model_path, output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.resize(800, 800)
    canvas = FacePreview(model_path, window, PlaybackMeter())
    QVBoxLayout(window).addWidget(canvas)
    frames, geometry, failures = [], [], []
    window.show()

    def sample():
        try:
            assert canvas.model, "Native model failed to initialize"
            frame = canvas.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888)
            array = np.frombuffer(frame.constBits(), dtype=np.uint8).reshape(frame.height(), frame.width(), 4).copy()
            vertices = {
                name: np.array(canvas.model.GetDrawableVertices(index)).reshape(-1, 2)
                for index, name in enumerate(canvas.model.GetDrawableIds())
            }
            frames.append(array)
            geometry.append(vertices)
            name = ("open", "closed", "reopened")[len(frames) - 1]
            assert frame.save(str(output / f"{name}.png"))
            if len(frames) == 3:
                assert np.array_equal(frames[0], frames[2]), "Reopened face differs from its starting frame"
                assert all(np.array_equal(geometry[0][key], geometry[1][key]) for key in vertices), "Blink moved a layer"
                changed = np.max(abs(frames[0].astype(int) - frames[1].astype(int)), axis=2) > 25
                matrix = np.array(canvas.model.GetMvp()).reshape(4, 4, order="F")
                widths = {}
                for side in ("left", "right"):
                    points = np.concatenate([vertices[f"Eye_viewer_{side}_{state}"] for state in ("open", "closed")])
                    clip = np.column_stack([points, np.zeros(len(points)), np.ones(len(points))]) @ matrix.T
                    pixel = np.column_stack([(clip[:, 0] + 1) * frame.width() / 2,
                                             (1 - clip[:, 1]) * frame.height() / 2])
                    x0, y0 = np.floor(pixel.min(axis=0)).astype(int)
                    x1, y1 = np.ceil(pixel.max(axis=0)).astype(int)
                    widths[side] = {}
                    for index, state in enumerate(("open", "closed")):
                        region = frames[index][y0:y1 + 1, x0:x1 + 1]
                        # Significant frame differences exclude unchanged hair and
                        # faint source noise. The largest dark connected component
                        # isolates the lash from the iris and antialiasing speckles.
                        dark = (region[:, :, :3].max(axis=2) < 100) & (region[:, :, 3] > 240)
                        components, count = label(dark & changed[y0:y1 + 1, x0:x1 + 1])
                        assert count, f"No visible {side} {state} lash pixels"
                        sizes = np.bincount(components.ravel())
                        largest = sizes[1:].argmax()
                        bounds = find_objects(components)[largest]
                        widths[side][state] = bounds[1].stop - bounds[1].start
                    assert widths[side]["closed"] <= widths[side]["open"], f"Visible {side} closed eye is too wide"
                assert abs(widths["left"]["closed"] - widths["right"]["closed"]) <= 2, "Closed eyes look different in width"
                report = {
                    "renderer": "native live2d-py; same camera and fixed pose for all three frames",
                    "open_reopened_pixels_identical": True,
                    "all_meshes_stationary_during_blink": True,
                    "visible_lash_width_pixels": widths,
                    "frame_sha256": [hashlib.sha256(a.tobytes()).hexdigest() for a in frames],
                }
                (output / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                app.quit()
            else:
                value = 0 if len(frames) == 1 else 1
                canvas.pose = {**rig.DEFAULTS, "ParamEyeLOpen": value, "ParamEyeROpen": value}
                canvas.update()
                QTimer.singleShot(250, sample)
        except Exception as exc:
            failures.append(str(exc))
            app.quit()

    QTimer.singleShot(800, sample)
    QTimer.singleShot(10000, app.quit)
    app.exec()
    canvas.release()
    window.close()
    if failures or len(frames) != 3:
        raise RuntimeError(failures or "Comparison did not complete")
    print("Face comparison passed: closed lash width and identical reopening.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=bundled_model_path())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compare(args.model.resolve(strict=True), args.output)
