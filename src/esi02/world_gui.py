"""Interactive projection of real 3-D room coordinates, with no extra graphics dependency."""

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from esi02.world import PLACES

ACTIVITIES = {
    "idle": "待機",
    "moving": "歩いています",
    "resting": "休んでいます",
    "talking": "対話",
    "exploring": "周りを調べています",
}


class RoomCanvas(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel, self.yaw = panel, math.pi / 4
        self.setMinimumSize(300, 240)
        self.world = None
        self.scale, self.offset = 1, QPointF()
        self.setToolTip(
            "床をクリックするとその場所へ移動します。仮想空間の位置です。デスクトップの立ち絵とは別です。"
        )

    def raw(self, x, y, z):
        x, z = x - 3, z - 2.5
        return QPointF(
            math.cos(self.yaw) * x - math.sin(self.yaw) * z,
            0.5 * (math.sin(self.yaw) * x + math.cos(self.yaw) * z) - 0.866 * y,
        )

    def project(self, x, y, z):
        point = self.raw(x, y, z)
        return QPointF(point.x() * self.scale, point.y() * self.scale) + self.offset

    def fit(self):
        points = [self.raw(x, y, z) for x in (0, 6) for y in (0, 3) for z in (0, 5)]
        xmin, xmax = min(p.x() for p in points), max(p.x() for p in points)
        ymin, ymax = min(p.y() for p in points), max(p.y() for p in points)
        self.scale = max(1, min((self.width() - 80) / (xmax - xmin), (self.height() - 55) / (ymax - ymin)))
        self.offset = QPointF(
            self.width() / 2 - (xmin + xmax) * self.scale / 2,
            self.height() / 2 - (ymin + ymax) * self.scale / 2,
        )

    def floor_at(self, point):
        sx, sy = (point.x() - self.offset.x()) / self.scale, (point.y() - self.offset.y()) / self.scale
        depth = sy / 0.5
        return {
            "x": math.cos(self.yaw) * sx + math.sin(self.yaw) * depth + 3,
            "y": 0,
            "z": -math.sin(self.yaw) * sx + math.cos(self.yaw) * depth + 2.5,
        }

    def polygon(self, painter, points, fill):
        painter.setPen(QPen(QColor("#506e88"), 1))
        painter.setBrush(QColor(fill))
        painter.drawPolygon(QPolygonF([self.project(*p) for p in points]))

    def paintEvent(self, event):
        if not self.world:
            return
        self.fit()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#111f2e"))
        self.polygon(painter, [(0, 0, 0), (6, 0, 0), (6, 0, 5), (0, 0, 5)], "#243e51")
        bx = 0 if math.sin(self.yaw) > 0 else 6
        bz = 0 if math.cos(self.yaw) > 0 else 5
        self.polygon(painter, [(bx, 0, 0), (bx, 0, 5), (bx, 3, 5), (bx, 3, 0)], "#294355")
        self.polygon(painter, [(0, 0, bz), (6, 0, bz), (6, 3, bz), (0, 3, bz)], "#304c60")
        painter.setPen(QPen(QColor("#3d5c70"), 1))
        for x in range(1, 6):
            painter.drawLine(self.project(x, 0, 0), self.project(x, 0, 5))
        for z in range(1, 5):
            painter.drawLine(self.project(0, 0, z), self.project(6, 0, z))
        objects = [
            (place["position"]["x"], place["position"]["z"], key, place) for key, place in PLACES.items()
        ]
        objects.append((self.world["position"]["x"], self.world["position"]["z"], "body", {}))
        objects.sort(key=lambda o: math.sin(self.yaw) * o[0] + math.cos(self.yaw) * o[1])
        for x, z, kind, place in objects:
            point = self.project(x, 0, z)
            if kind == "body":
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#082a36"))
                painter.drawEllipse(point, self.scale * 0.35, self.scale * 0.15)
                top = self.project(x, 1.5, z)
                painter.setPen(QPen(QColor("#68dfcf"), max(6, self.scale * 0.22), Qt.SolidLine, Qt.RoundCap))
                painter.drawLine(self.project(x, 0.3, z), self.project(x, 1.05, z))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#91eee3"))
                painter.drawEllipse(top, self.scale * 0.18, self.scale * 0.18)
                painter.setPen(QColor("#efffff"))
                painter.drawText(top + QPointF(-18, -16), "ESI")
            else:
                color = {"rest": "#497d9b", "desk": "#bc9570", "window": "#d9c875", "water": "#3c98b8"}[kind]
                self.polygon(
                    painter,
                    [
                        (x - 0.4, 0.25, z - 0.3),
                        (x + 0.4, 0.25, z - 0.3),
                        (x + 0.4, 0.25, z + 0.3),
                        (x - 0.4, 0.25, z + 0.3),
                    ],
                    color,
                )
                painter.setPen(QColor("#e5f2ff"))
                painter.drawText(point + QPointF(-30, 20), place["label"])
        destination = self.world.get("destination")
        if destination:
            painter.setPen(QPen(QColor("#e8d59c"), 2, Qt.DashLine))
            p = self.world["position"]
            painter.drawLine(
                self.project(p["x"], 0, p["z"]), self.project(destination["x"], 0, destination["z"])
            )
        painter.setPen(QColor("#9ebace"))
        painter.drawText(12, 20, "仮想の部屋 6m × 5m × 高さ3m / 床をクリックして移動")
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.world:
            point = self.floor_at(event.position())
            if 0 <= point["x"] <= 6 and 0 <= point["z"] <= 5:
                self.panel.window.world_action("move", point)


class WorldPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        note = QLabel(
            "ESIの仮想身体です。表示する温度・体調は計算上の値です。開始中だけ時間が進み、停止・終了中は進みません。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.canvas = RoomCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.body = QLabel()
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        self.stimulus = QLabel()
        self.stimulus.setWordWrap(True)
        layout.addWidget(self.stimulus)
        environment = QHBoxLayout()
        environment.addWidget(QLabel("部屋の基準温度"))
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(-10, 45)
        self.temperature.setSuffix(" ℃")
        environment.addWidget(self.temperature)
        environment.addWidget(QLabel("周囲の音"))
        self.noise = QDoubleSpinBox()
        self.noise.setRange(15, 95)
        self.noise.setSuffix(" dB（仮想）")
        environment.addWidget(self.noise)
        self.lamp = QCheckBox("照明")
        environment.addWidget(self.lamp)
        apply = QPushButton("環境を変更")
        apply.clicked.connect(
            lambda: self.window.world_action(
                "environment",
                {
                    "temperature": self.temperature.value(),
                    "noise_db": self.noise.value(),
                    "lamp": self.lamp.isChecked(),
                },
            )
        )
        environment.addWidget(apply)
        layout.addLayout(environment)
        actions = QHBoxLayout()
        self.places = QComboBox()
        for key, place in PLACES.items():
            self.places.addItem(place["label"], key)
        actions.addWidget(self.places)
        go = QPushButton("移動する")
        go.clicked.connect(lambda: self.window.world_action("place", self.places.currentData()))
        rest = QPushButton("休む")
        rest.clicked.connect(lambda: self.window.world_action("activity", "resting"))
        idle = QPushButton("休憩を終える")
        idle.clicked.connect(lambda: self.window.world_action("activity", "idle"))
        drink = QPushButton("水を飲む")
        drink.clicked.connect(lambda: self.window.world_action("drink"))
        rotate = QPushButton("視点を回す")
        rotate.clicked.connect(self.rotate)
        for widget in (go, rest, idle, drink, rotate):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.session_id = None

    def rotate(self):
        self.canvas.yaw += math.pi / 6
        self.canvas.update()

    def refresh(self, world):
        if self.session_id != id(self.window.session):
            room = self.window.session.world.room
            self.temperature.setValue(room.temperature)
            self.noise.setValue(room.noise_db)
            self.lamp.setChecked(room.lamp)
            self.session_id = id(self.window.session)
        self.canvas.world = world
        self.canvas.update()
        b, s, p = world["body"], world["stimulus"], world["position"]
        self.body.setText(
            f"{ACTIVITIES[b['activity']]}"
            + ("・対話中" if world.get("conversation_active") else "")
            + f" / 位置 ({p['x']:.1f}, {p['y']:.1f}, {p['z']:.1f})m\n活動エネルギー {b['energy']:.0%} / 疲労 {b['fatigue']:.0%} / 水分 {b['hydration']:.0%} / 体調 {b['condition']:.0%} / 仮想体温 {b['temperature']:.1f}℃"
        )
        self.stimulus.setText(
            f"その場所の温度 {s['temperature']:.1f}℃ / 明るさ {s['light_lux']:.0f}lux / 音 {s['noise_db']:.1f}dB\n温度の不快度 {s['thermal_discomfort']:.0%} / 音の不快度 {s['noise_discomfort']:.0%} / 身体の不快度 {s['body_discomfort']:.0%} / 仮想時間 {world['elapsed_seconds']:.1f}秒"
        )
