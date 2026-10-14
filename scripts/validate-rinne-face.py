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
                left = vertices["Eye_viewer_left_open"]
                right = vertices["Eye_viewer_right_open"]
                # Original-pixel registration, inferred from the two eye crops.
                # These checks catch atlas contamination and missing crown
                # triangles that geometry-only and blink-width checks miss.
                sx = (right[:, 0].mean() - left[:, 0].mean()) / (559 - 454.5)
                sy = -np.ptp(left[:, 1]) / 44
                origin = left.mean(axis=0) - np.array([454.5, 212]) * [sx, sy]

                def source_pixels(points):
                    world = origin + np.array(points) * [sx, sy]
                    clip = np.column_stack([world, np.zeros(len(world)), np.ones(len(world))]) @ matrix.T
                    return np.column_stack([(clip[:, 0] + 1) * frame.width() / 2,
                                            (1 - clip[:, 1]) * frame.height() / 2])

                landmarks = [(506, 72), (511, 76), (502, 80), (520, 82), (530, 70), (535, 75), (520, 62)]
                for x, y in source_pixels(landmarks).round().astype(int):
                    assert frames[0][y, x, 3] > 240, "Original crown has a transparent hole"
                # Fixed dark pixels on the original outer hair line. Coverage
                # alone misses a mesh that cuts off the stroke but retains fill.
                outline = [
                    (437, 82), (425, 94), (415, 106), (407, 118), (400, 130),
                    (395, 142), (387, 166), (384, 178), (380, 190),
                    (570, 82), (582, 94), (591, 106), (598, 118), (605, 130),
                    (610, 142), (618, 166), (621, 178), (627, 190),
                ]
                for index, (x, y) in enumerate(source_pixels(outline).round().astype(int)):
                    region = frames[0][y - 2:y + 3, x - 2:x + 3]
                    dark = (region[:, :, :3].max(axis=2) < 120) & (region[:, :, 3] > 180)
                    assert np.any(dark), f"Original hair outline is clipped near {outline[index]}"
                face_bounds = source_pixels([(450, 242), (559, 290)])
                x0, y0 = np.floor(face_bounds.min(axis=0)).astype(int)
                x1, y1 = np.ceil(face_bounds.max(axis=0)).astype(int)
                face = frames[0][y0:y1 + 1, x0:x1 + 1]
                yellow = (face[:, :, 0] > 180) & (face[:, :, 1] > 150) & (face[:, :, 2] < 130) & (face[:, :, 3] > 100)
                assert np.count_nonzero(yellow) < 5, "Halo/atlas pixels leaked into the neutral face"
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
                    "neutral_face_atlas_contamination_checked": True,
                    "original_crown_coverage_checked": True,
                    "original_hair_outline_landmarks_checked": len(outline),
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
