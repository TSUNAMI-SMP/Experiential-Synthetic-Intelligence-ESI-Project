"""A manipulable 3-D room projection: objects, heights, paths and observed actions."""

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
)

from esi02.world_gui import RoomCanvas as PreviousCanvas
from esi02.world_gui import WorldPanel as PreviousPanel
from esi04.world import LABELS, SIZES

COLORS = {"snack": "#e0b37c", "desk": "#ad825c", "chair": "#7b91af", "bed": "#6a9cb7",
          "computer": "#496374", "book": "#a5c49f", "ball": "#edab9d", "water": "#68b9df", "window": "#d0dcbf"}


class RoomCanvas(PreviousCanvas):
    def box(self, painter, position, size, color):
        x, y, z = position["x"], position["y"], position["z"]
        w, d, h = size
        c = QColor(color)
        faces = [([(x-w/2, y, z-d/2), (x+w/2, y, z-d/2), (x+w/2, y+h, z-d/2), (x-w/2, y+h, z-d/2)], c.darker(120)),
                 ([(x-w/2, y, z-d/2), (x-w/2, y, z+d/2), (x-w/2, y+h, z+d/2), (x-w/2, y+h, z-d/2)], c.darker(135)),
                 ([(x-w/2, y+h, z-d/2), (x+w/2, y+h, z-d/2), (x+w/2, y+h, z+d/2), (x-w/2, y+h, z+d/2)], c.lighter(115))]
        for points, shade in faces:
            self.polygon(painter, points, shade.name())

    def paintEvent(self, event):
        if not self.world:
            return
        self.fit()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#111f2e"))
        self.polygon(painter, [(0, 0, 0), (6, 0, 0), (6, 0, 5), (0, 0, 5)], "#263e50")
        bx, bz = (0 if math.sin(self.yaw) > 0 else 6), (0 if math.cos(self.yaw) > 0 else 5)
        self.polygon(painter, [(bx, 0, 0), (bx, 0, 5), (bx, 3, 5), (bx, 3, 0)], "#294355")
        self.polygon(painter, [(0, 0, bz), (6, 0, bz), (6, 3, bz), (0, 3, bz)], "#304c60")
        painter.setPen(QPen(QColor("#426078"), 1))
        for x in range(1, 6):
            painter.drawLine(self.project(x, 0, 0), self.project(x, 0, 5))
        for z in range(1, 5):
            painter.drawLine(self.project(0, 0, z), self.project(6, 0, z))
        objects = [o for o in self.world.get("objects", []) if o["quantity"] > 0]
        objects += [{"kind": "body", "position": self.world["position"], "label": "ESI"}]
        objects.sort(key=lambda o: math.sin(self.yaw)*o["position"]["x"]+math.cos(self.yaw)*o["position"]["z"]
                     + o["position"]["y"]*0.1)
        for obj in objects:
            p, kind = obj["position"], obj["kind"]
            if kind == "body":
                self.box(painter, p, (0.35, 0.28, 1.2), "#65c7bd")
                head = self.project(p["x"], 1.45, p["z"])
                painter.setBrush(QColor("#b0e7db"))
                painter.drawEllipse(head, self.scale*0.16, self.scale*0.16)
            else:
                self.box(painter, p, SIZES[kind], COLORS[kind])
                if kind == "computer":
                    self.polygon(painter, [(p["x"]-0.2, p["y"]+0.08, p["z"]-0.16),
                                           (p["x"]+0.2, p["y"]+0.08, p["z"]-0.16),
                                           (p["x"]+0.2, p["y"]+0.35, p["z"]-0.16),
                                           (p["x"]-0.2, p["y"]+0.35, p["z"]-0.16)], "#80d8e1")
            top = self.project(p["x"], p["y"]+(1.7 if kind == "body" else SIZES[kind][2]+0.15), p["z"])
            painter.setPen(QColor("#eff5ff"))
            label = obj["label"]+ (f" ×{obj['quantity']}" if obj.get("quantity", 1) > 1 else "")
            painter.drawText(top+QPointF(-25, -4), label)
        points = [self.world["position"]]+([self.world["destination"]] if self.world.get("destination") else [])+self.world.get("path", [])
        painter.setPen(QPen(QColor("#e8d59c"), 2, Qt.PenStyle.DashLine))
        for a, b in zip(points, points[1:]):
            painter.drawLine(self.project(a["x"], 0.02, a["z"]), self.project(b["x"], 0.02, b["z"]))
        painter.setPen(QColor("#abc3d4"))
        painter.drawText(12, 20, "床をクリックして移動 / 物体は下の一覧から配置・移動・撤去")
        painter.end()


class WorldPanel(PreviousPanel):
    def __init__(self, window):
        super().__init__(window)
        old = self.canvas
        self.canvas = RoomCanvas(self)
        self.layout().replaceWidget(old, self.canvas)
        old.deleteLater()
        self.objects = QTableWidget(0, 6)
        self.objects.setHorizontalHeaderLabels(["物体", "種類", "個数", "位置", "状態", "ID"])
        self.objects.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.objects.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.objects.setMaximumHeight(180)
        self.objects.itemSelectionChanged.connect(self.selected_object)
        self.layout().addWidget(self.objects)
        row = QHBoxLayout()
        self.kind = QComboBox()
        for key, label in LABELS.items():
            if key != "window":
                self.kind.addItem(label, key)
        row.addWidget(self.kind)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 99)
        row.addWidget(self.quantity)
        self.coordinates = {}
        for axis, maximum in (("x", 6), ("y", 3), ("z", 5)):
            row.addWidget(QLabel(axis))
            spin = QDoubleSpinBox()
            spin.setRange(0, maximum)
            spin.setSingleStep(0.1)
            spin.setDecimals(2)
            self.coordinates[axis] = spin
            row.addWidget(spin)
        self.coordinates["x"].setValue(3.7)
        self.coordinates["z"].setValue(2.5)
        self.surface = QComboBox()
        self.surface.addItem("床・指定した高さ", None)
        row.addWidget(self.surface)
        self.layout().addLayout(row)
        actions = QHBoxLayout()
        for label, operation in [("置く", "place"), ("移動", "move"), ("取り除く", "remove"),
                                 ("近くへ行く", "approach"), ("調べる", "inspect"), ("食べる", "eat"), ("使う", "use")]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, op=operation: self.scene(op))
            actions.addWidget(button)
        self.layout().addLayout(actions)
        self.object_signature = None
        self.selected_id = None

    def selected_object(self):
        row = self.objects.currentRow()
        if row >= 0 and self.objects.item(row, 5):
            self.selected_id = self.objects.item(row, 5).text()
            obj = next((o for o in self.window.session.world.objects if o.id == self.selected_id), None)
            if obj:
                for axis, spin in self.coordinates.items():
                    spin.setValue(getattr(obj.position, axis))

    def scene(self, operation):
        values = {"id": self.selected_id}
        if operation in {"place", "move"}:
            values["position"] = {axis: spin.value() for axis, spin in self.coordinates.items()}
            values["parent_id"] = self.surface.currentData()
        if operation == "place":
            values.pop("id")
            values.update(kind=self.kind.currentData(), quantity=self.quantity.value(), parent_id=self.surface.currentData())
        self.window.scene_action(operation, values)

    def refresh(self, world):
        super().refresh(world)
        if not hasattr(self, "objects"):
            return
        signature = (id(self.window.session), world.get("revision"), world.get("discoveries"))
        if signature == self.object_signature:
            return
        self.object_signature = signature
        self.objects.blockSignals(True)
        objects = world.get("objects", [])
        self.objects.setRowCount(len(objects))
        selected = self.selected_id
        for row, obj in enumerate(objects):
            p = obj["position"]
            cells = [obj["label"], LABELS[obj["kind"]], str(obj["quantity"]),
                     f"({p['x']:.1f}, {p['y']:.1f}, {p['z']:.1f})",
                     "調べた" if obj["inspected"] else "見つけた" if obj["discovered"] else "未発見", obj["id"]]
            for column, value in enumerate(cells):
                self.objects.setItem(row, column, QTableWidgetItem(value))
            if obj["id"] == selected:
                self.objects.selectRow(row)
        self.objects.blockSignals(False)
        self.objects.resizeColumnsToContents()
        previous = self.surface.currentData()
        self.surface.clear()
        self.surface.addItem("床・指定した高さ", None)
        for obj in objects:
            if obj["kind"] == "desk":
                self.surface.addItem(obj["label"]+"の上", obj["id"])
        self.surface.setCurrentIndex(max(0, self.surface.findData(previous)))
