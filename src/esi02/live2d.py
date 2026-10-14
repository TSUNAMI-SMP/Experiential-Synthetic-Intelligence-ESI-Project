"""ESI-02 Cubism rendering driven only by real output telemetry."""

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from esi.avatar import LiveCanvas


class AudioLiveCanvas(LiveCanvas):
    def __init__(self, path, parent, meter):
        if QApplication.platformName() in {"offscreen", "minimal"}:
            raise RuntimeError("この表示環境ではLive2Dを描画できません。")
        self.meter = meter
        super().__init__(path, parent)

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.Update()
        local = self.mapFromGlobal(QCursor.pos())
        self.model.Drag(local.x(), local.y())
        if "ParamMouthOpenY" in self.parameters:
            self.model.SetParamById("ParamMouthOpenY", self.meter.level())
        self.model.Draw()
