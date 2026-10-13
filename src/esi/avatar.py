"""Independent topmost controls and a genuine Cubism model renderer.

The bundled illustration is explicitly a still image. Only a model3.json and
its exported MOC3 assets activate Live2D; no PNG animation is called Live2D.
"""

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QImageReader, QPainter, QPixmap, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from esi.icons import RESOURCES, activity_symbol, icon

_sdk_initialized = False


def initialize_sdk():
    global _sdk_initialized
    import live2d

    if not _sdk_initialized:
        live2d.init()
        live2d.enableLog(False)
        _sdk_initialized = True
    return live2d


def validate_model(path):
    path = Path(path).resolve(strict=True)
    if not path.name.endswith(".model3.json") or path.stat().st_size > 2_000_000:
        raise ValueError("Live2Dの .model3.json を選んでください。")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("モデルのJSON形式が正しくありません。")
    files = value.get("FileReferences", {})
    if value.get("Version") != 3 or not isinstance(files, dict):
        raise ValueError("Cubism 3以降の書き出し済みモデルが必要です。")

    def local_file(name):
        if not isinstance(name, str) or not name or PureWindowsPath(name).is_absolute() or ":" in name:
            raise ValueError("モデルの参照先は同じフォルダー内のファイルにしてください。")
        target = (path.parent / name.replace("\\", "/")).resolve(strict=True)
        if not target.is_relative_to(path.parent) or not target.is_file():
            raise ValueError("モデルの参照先がフォルダーの外にあります。")
        if target.stat().st_size > 64_000_000:
            raise ValueError("モデルの参照ファイルが大きすぎます。")
        if target.suffix == ".json":
            if target.stat().st_size > 8_000_000:
                raise ValueError("モデルのJSONが大きすぎます。")
            json.loads(target.read_text(encoding="utf-8-sig"))
        return target

    moc = local_file(files.get("Moc"))
    with moc.open("rb") as stream:
        if moc.suffix != ".moc3" or stream.read(4) != b"MOC3" or moc.stat().st_size < 64:
            raise ValueError("書き出し済みの有効な .moc3 が必要です。")
    textures = files.get("Textures")
    if not isinstance(textures, list) or not textures:
        raise ValueError("モデルの画像（Textures）がありません。")
    for name in textures:
        texture = local_file(name)
        with texture.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("モデルのテクスチャはPNG形式にしてください。")
        reader = QImageReader(str(texture))
        size = reader.size()
        if (
            size.width() < 1
            or size.height() < 1
            or max(size.width(), size.height()) > 8192
            or reader.read().isNull()
        ):
            raise ValueError("モデルの画像を読めません（最大8192ピクセル）。")
    for key in ("Physics", "Pose", "DisplayInfo", "UserData"):
        if files.get(key):
            local_file(files[key])
    for expression in files.get("Expressions", []):
        local_file(expression["File"])
    for motions in files.get("Motions", {}).values():
        for motion in motions:
            local_file(motion["File"])
            if motion.get("Sound"):
                local_file(motion["Sound"])
    return path


def probe_model(path):
    """A corrupt native model must not terminate the ESI process."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            [str(executable), "-m", "esi.model_probe", str(path)], capture_output=True, timeout=20, **options
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Live2Dの確認が時間切れになりました。") from exc
    if result.returncode:
        raise ValueError("このLive2Dモデルを読み込めません。書き出し設定と表示ライブラリを確認してください。")


def visible_geometry(position, width, screens):
    """Keep the control strip reachable after monitor removal or size changes."""
    try:
        width = max(260, min(720, int(width)))
    except (TypeError, ValueError):
        width = 340
    height = round(width * 1.5) + 116
    available = screens or [QRect(0, 0, 1280, 900)]
    point = (
        QPoint(*position)
        if (
            isinstance(position, list)
            and len(position) == 2
            and all(isinstance(x, int) and abs(x) < 1_000_000 for x in position)
        )
        else None
    )
    screen = next((s for s in available if point and s.contains(point)), available[0])
    width = min(width, screen.width())
    height = min(height, screen.height())
    x = point.x() if point else screen.right() - width - 20
    y = point.y() if point else screen.bottom() - height - 20
    x = max(screen.left(), min(x, screen.right() - width + 1))
    y = max(screen.top(), min(y, screen.bottom() - height + 1))
    return QRect(x, y, width, height)


class StillCanvas(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.pixmap = QPixmap(str(RESOURCES / "rion-el.png"))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumHeight(120)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        size = self.pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        painter.drawPixmap(
            (self.width() - size.width()) // 2,
            self.height() - size.height(),
            self.pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation),
        )

    def mousePressEvent(self, event):
        self.window().begin_drag(event)

    def mouseMoveEvent(self, event):
        self.window().continue_drag(event)

    def mouseReleaseEvent(self, event):
        self.window().finish_drag()


class LiveCanvas(QOpenGLWidget):
    problem = Signal(str)
    ready = Signal()

    def __init__(self, path, parent):
        # Initialize the SDK before Qt makes an OpenGL context current.
        sdk = initialize_sdk()
        super().__init__(parent)
        surface = QSurfaceFormat()
        surface.setAlphaBufferSize(8)
        surface.setVersion(2, 1)
        self.setFormat(surface)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.path, self.model, self.live2d = path, None, sdk
        self.phase = "stopped"
        self.clock = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(33)

    def initializeGL(self):
        try:
            self.live2d.glInit()
            self.model = self.live2d.Model()
            # The native renderer needs its viewport before allocating render targets.
            # Calling methods on an unloaded native Model also dereferences a null pointer.
            self.model.LoadModelJson(str(self.path), create_renderer=False)
            self.model.Resize(self.width(), self.height())
            self.model.CreateRenderer()
            self.parameters = set(self.model.GetParamIds())
            self.model.SetAutoBlink(True)
            self.model.SetAutoBreath(True)
            self.context().aboutToBeDestroyed.connect(self.release)
            self.ready.emit()
        except Exception as exc:
            self.timer.stop()
            self.problem.emit(f"Live2Dを表示できません：{exc}")

    def resizeGL(self, width, height):
        if self.model:
            self.model.Resize(width, height)

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.Update()
        local = self.mapFromGlobal(QCursor.pos())
        self.model.Drag(local.x(), local.y())
        # This is a speaking indicator, not phoneme/amplitude synchronization.
        elapsed = time.monotonic() - self.clock
        if "ParamMouthOpenY" in self.parameters:
            opening = (0.2 + 0.5 * abs(math.sin(elapsed * 11))) if self.phase == "playing" else 0
            self.model.SetParamById("ParamMouthOpenY", opening)
        self.model.Draw()

    def release(self):
        self.timer.stop()
        if self.model and self.context() and self.context().isValid():
            self.makeCurrent()
            self.model.DestroyRenderer()
            self.model = None
            self.doneCurrent()

    def mousePressEvent(self, event):
        self.window().begin_drag(event)

    def mouseMoveEvent(self, event):
        self.window().continue_drag(event)

    def mouseReleaseEvent(self, event):
        self.window().finish_drag()


class AvatarWindow(QWidget):
    start_requested = Signal()
    microphone_requested = Signal()
    main_requested = Signal()
    hidden_requested = Signal()
    preferences_changed = Signal()

    def __init__(self, preferences):
        # A separate top-level window remains visible when the main window is minimized.
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("ESI-01 / 凛音エル")
        self.preferences = preferences
        self.drag_origin = None
        self.model_path = None
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(5, 0, 5, 5)
        self.layout.setSpacing(4)
        self.canvas = StillCanvas(self)
        self.layout.addWidget(self.canvas, 1)
        panel = QFrame()
        panel.setObjectName("avatarPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(8, 6, 8, 6)
        title_row = QHBoxLayout()
        self.symbol = QLabel()
        title_row.addWidget(self.symbol)
        self.state_label = QLabel("停止中")
        title_row.addWidget(self.state_label, 1)
        for label, delta in [("−", -40), ("＋", 40)]:
            button = QPushButton(label)
            button.setFixedWidth(28)
            button.setToolTip("立ち絵の大きさを変更")
            button.clicked.connect(lambda checked=False, d=delta: self.scale(d))
            title_row.addWidget(button)
        hide = QPushButton("×")
        hide.setFixedWidth(28)
        hide.setToolTip("アバターを隠す（本体の処理は続きます）")
        hide.clicked.connect(self.hide_avatar)
        title_row.addWidget(hide)
        panel_layout.addLayout(title_row)
        controls = QHBoxLayout()
        self.start_button = QPushButton("開始")
        self.start_button.setIcon(icon("start"))
        self.start_button.clicked.connect(self.start_requested)
        self.microphone_button = QPushButton("マイクOFF")
        self.microphone_button.setIcon(icon("microphone"))
        self.microphone_button.clicked.connect(self.microphone_requested)
        main = QPushButton("本体")
        main.setToolTip("ESIの画面を開く")
        main.clicked.connect(self.main_requested)
        for button in (self.start_button, self.microphone_button, main):
            controls.addWidget(button)
        panel_layout.addLayout(controls)
        self.mode_label = QLabel("立ち絵表示 · 絵をドラッグして移動")
        self.mode_label.setStyleSheet("color: #9bb4c9; font-size: 10px")
        panel_layout.addWidget(self.mode_label)
        self.layout.addWidget(panel)
        self.setStyleSheet(
            "QWidget {color: #edf5ff; background: transparent;}"
            "QFrame#avatarPanel {background: #182c40; border: 1px solid #41627d; border-radius: 14px;}"
            "QPushButton {background: #29465e; border: 0; border-radius: 6px; padding: 5px;}"
            "QPushButton:hover {background: #3a637f;} QPushButton:disabled {color: #8397a9;}"
        )
        self.setGeometry(
            visible_geometry(
                preferences.get("position"),
                preferences.get("width", 340),
                [s.availableGeometry() for s in QApplication.screens()],
            )
        )

    def begin_drag(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_origin = event.globalPosition().toPoint() - self.pos()

    def continue_drag(self, event):
        if event.buttons() & Qt.LeftButton and self.drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self.drag_origin)

    def mouseReleaseEvent(self, event):
        self.finish_drag()

    def finish_drag(self):
        self.drag_origin = None
        self.setGeometry(
            visible_geometry(
                [self.x(), self.y()], self.width(), [s.availableGeometry() for s in QApplication.screens()]
            )
        )
        self.remember()

    def remember(self):
        self.preferences.update(position=[self.x(), self.y()], width=self.width())
        self.preferences_changed.emit()

    def scale(self, delta):
        self.setGeometry(
            visible_geometry(
                [self.x(), self.y()],
                self.width() + delta,
                [s.availableGeometry() for s in QApplication.screens()],
            )
        )
        self.remember()

    def hide_avatar(self):
        self.hide()
        self.hidden_requested.emit()

    def set_model(self, path):
        path = validate_model(path) if path else None
        if path:
            probe_model(path)
        replacement = LiveCanvas(path, self) if path else StillCanvas(self)
        old = self.canvas
        if isinstance(old, LiveCanvas):
            old.release()
        self.canvas = replacement
        self.layout.replaceWidget(old, self.canvas)
        old.hide()
        old.deleteLater()
        self.model_path = path
        if isinstance(self.canvas, LiveCanvas):
            self.canvas.problem.connect(self.model_failed)
            self.canvas.ready.connect(lambda: self.mode_label.setText("Live2D · 絵をドラッグして移動"))
            self.mode_label.setText("Live2Dを読み込み中…")
        else:
            self.mode_label.setText("立ち絵表示 · 絵をドラッグして移動")
        self.canvas.show()

    def model_failed(self, message):
        self.mode_label.setText(message)
        self.mode_label.setWordWrap(True)
        self.mode_label.setToolTip(message)

    def set_status(self, view, current, running, microphone, enabled=True):
        self.state_label.setText(current["title"])
        self.state_label.setWordWrap(True)
        self.state_label.setToolTip(current["detail"])
        self.symbol.setPixmap(icon(activity_symbol(view, current["phase"])).pixmap(24, 24))
        self.start_button.setText("停止" if running else "開始")
        self.start_button.setIcon(icon("stop" if running else "start"))
        self.start_button.setEnabled(enabled)
        self.microphone_button.setText("マイクON" if microphone else "マイクOFF")
        self.microphone_button.setEnabled(running and enabled)
        if isinstance(self.canvas, LiveCanvas):
            self.canvas.phase = current["phase"]

    def shutdown(self):
        self.remember()
        if isinstance(self.canvas, LiveCanvas):
            self.canvas.release()
        self.close()
