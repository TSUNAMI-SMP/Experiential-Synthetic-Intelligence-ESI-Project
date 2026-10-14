"""ESI-02 Cubism rendering driven only by real output telemetry."""

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from esi.avatar import LiveCanvas


class AudioLiveCanvas(LiveCanvas):
    def __init__(self, path, parent, meter):
        if QApplication.platformName() in {"offscreen", "minimal"}:
            raise RuntimeError("この表示環境ではLive2Dを描画できません。")
        self.meter = meter
        self.discrete_blink = False
        super().__init__(path, parent)

    def initializeGL(self):
        super().initializeGL()
        if self.model:
            # This authored model uses separate open/closed drawings. Mixing
            # them makes two eyelids and irises appear during a blink.
            self.discrete_blink = {
                "Eye_viewer_left_open", "Eye_viewer_right_open",
                "Eye_viewer_left_closed", "Eye_viewer_right_closed",
            }.issubset(self.model.GetDrawableIds())

    def apply_blink_visibility(self):
        if self.discrete_blink:
            for name in ("ParamEyeLOpen", "ParamEyeROpen"):
                if name in self.parameters:
                    value = self.model.GetParamValueById(name)
                    self.model.SetParamById(name, 1.0 if value > 0.5 else 0.0)

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.Update()
        local = self.mapFromGlobal(QCursor.pos())
        self.model.Drag(local.x(), local.y())
        self.apply_blink_visibility()
        if "ParamMouthOpenY" in self.parameters:
            self.model.SetParamById("ParamMouthOpenY", self.meter.level())
        self.model.Draw()
