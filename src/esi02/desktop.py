"""Independent desktop controls. No automatic screen-position correction."""

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from esi.avatar import StillCanvas, probe_model, validate_model
from esi.icons import icon
from esi02.desktop_preferences import read_preferences, write_preferences
from esi02.live2d import AudioLiveCanvas
from esi02.playback import PlaybackMeter


def primary_area():
    screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1280, 900)


class FloatingWindow(QWidget):
    geometry_changed = Signal()
    hidden_requested = Signal()

    def __init__(self, title):
        # No QObject/window parent: minimizing the main window must not hide this one.
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self.drag_origin = None
        self.dragged = False

    def begin_drag(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_origin = (event.globalPosition().toPoint(), self.pos())
            self.dragged = False
            event.accept()

    def continue_drag(self, event):
        if self.drag_origin and event.buttons() & Qt.LeftButton:
            pointer, position = self.drag_origin
            offset = event.globalPosition().toPoint() - pointer
            if offset.manhattanLength() >= QApplication.startDragDistance():
                self.dragged = True
            if self.dragged:
                self.move(position + offset)
            event.accept()

    def finish_drag(self):
        moved = self.dragged
        self.drag_origin = None
        self.dragged = False
        return moved

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometry_changed.emit()

    def closeEvent(self, event):
        # Closing a floating view never shuts down the shared runtime.
        self.hidden_requested.emit()
        event.accept()


class MicrophoneButton(FloatingWindow):
    clicked = Signal()
    start_requested = Signal()
    main_requested = Signal()
    reset_requested = Signal()

    def __init__(self):
        super().__init__("ESI-02 マイク")
        self.setFixedSize(72, 72)
        self.setMask(QRegion(self.rect(), QRegion.Ellipse))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("ESI マイク切替")
        self.running, self.microphone, self.problem = False, False, ""
        self.menu = QMenu(self)
        self.start_action = self.menu.addAction(icon("start"), "開始")
        self.start_action.triggered.connect(self.start_requested)
        self.mic_action = self.menu.addAction(icon("microphone"), "マイクをONにする")
        self.mic_action.triggered.connect(self.clicked)
        self.menu.addSeparator()
        self.main_action = self.menu.addAction(icon("folder"), "本体を表示")
        self.main_action.triggered.connect(self.main_requested)
        self.reset_action = self.menu.addAction("マイクボタンの位置を戻す")
        self.reset_action.triggered.connect(self.reset_requested)
        self.hide_action = self.menu.addAction("マイクボタンを隠す")
        self.hide_action.triggered.connect(self.hidden_requested)
        self.set_state(False, False)

    def set_state(self, running, microphone, problem=""):
        self.running, self.microphone, self.problem = running, microphone, problem
        self.start_action.setText("停止" if running else "開始")
        self.start_action.setIcon(icon("stop" if running else "start"))
        self.mic_action.setText("マイクをOFFにする" if microphone else "マイクをONにする")
        self.setToolTip(
            ("マイクON" if microphone else "マイクOFF")
            + (" / 動作中" if running else " / 停止中")
            + "\nクリック：マイク切替（停止中はマイクを使って開始）"
            + "\nドラッグ：移動 / 右クリック：開始・停止・本体を表示"
            + ("\n" + problem if problem else "")
        )
        self.setAccessibleDescription(self.toolTip())
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(30, 95, 90, 210) if self.microphone else QColor(26, 38, 54, 190)
        painter.setBrush(color)
        painter.setPen(QPen(QColor("#f9b4bf" if self.problem else "#70dccc"), 2))
        painter.drawEllipse(self.rect().adjusted(3, 3, -3, -3))
        icon("microphone").paint(painter, QRect(23, 14, 26, 30))
        if not self.microphone:
            painter.setPen(QPen(QColor("#edf5ff"), 2))
            painter.drawLine(22, 40, 49, 15)
        painter.setPen(QColor("#edf5ff"))
        painter.drawText(QRect(4, 46, 64, 19), Qt.AlignCenter, "ON" if self.microphone else "OFF")
        painter.setBrush(QColor("#70dccc" if self.running else "#8397a9"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(56, 48, 7, 7)

    def mousePressEvent(self, event):
        self.begin_drag(event)

    def mouseMoveEvent(self, event):
        self.continue_drag(event)

    def mouseReleaseEvent(self, event):
        pressed = self.drag_origin is not None
        moved = self.finish_drag()
        if (
            pressed
            and event.button() == Qt.LeftButton
            and not moved
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        self.menu.popup(event.globalPos())


class AvatarWindow(FloatingWindow):
    """Free-positioned still draft or a user-selected exported Cubism model."""

    def __init__(self):
        super().__init__("ESI-02 凛音エル — 立ち絵・制作中")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.canvas = StillCanvas(self)
        self.meter = PlaybackMeter()
        self.model_path = None
        self.problem = ""
        layout.addWidget(self.canvas, 1)
        controls = QWidget()
        controls.setStyleSheet(
            "QWidget {background: rgba(21,35,50,175); color: #edf5ff; font-size: 11px;}"
            "QPushButton {background: #29465e; border: 0; border-radius: 4px;}"
        )
        row = QHBoxLayout(controls)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(3)
        self.symbol = QLabel()
        self.symbol.setFixedSize(18, 18)
        row.addWidget(self.symbol)
        self.state_label = QLabel("停止中")
        self.state_label.setMinimumWidth(0)
        self.state_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.state_label, 1)
        self.minus = QPushButton("−")
        self.plus = QPushButton("＋")
        self.hide_button = QPushButton("×")
        for button, tooltip in (
            (self.minus, "立ち絵を小さくする"),
            (self.plus, "立ち絵を大きくする"),
            (self.hide_button, "立ち絵を隠す（ESIの処理は続きます）"),
        ):
            button.setFixedSize(22, 22)
            button.setToolTip(tooltip)
            row.addWidget(button)
        self.minus.clicked.connect(lambda: self.scale(-30))
        self.plus.clicked.connect(lambda: self.scale(30))
        self.hide_button.clicked.connect(self.hidden_requested)
        layout.addWidget(controls)
        self.setToolTip(
            "ドラッグで自由に移動できます。現在は静止画です。Live2Dモデルは制作工程が残っています。"
        )
        self.set_width(300)

    def set_model(self, path):
        path = validate_model(path) if path else None
        if path:
            probe_model(path)
        replacement = AudioLiveCanvas(path, self, self.meter) if path else StillCanvas(self)
        old = self.canvas
        if isinstance(old, AudioLiveCanvas):
            old.release()
        self.layout().replaceWidget(old, replacement)
        old.hide()
        old.deleteLater()
        self.canvas, self.model_path, self.problem = replacement, path, ""
        if path:
            replacement.problem.connect(self.model_failed)
        replacement.show()
        self.setWindowTitle("ESI-02 アバター — " + ("Live2D" if path else "立ち絵・制作中"))
        self.setToolTip(
            "ドラッグで自由に移動できます。"
            + ("書き出し済みLive2Dモデルを表示します。" if path else "現在は静止画の下書きです。")
        )

    def model_failed(self, message):
        self.problem = message

    def release(self):
        if isinstance(self.canvas, AudioLiveCanvas):
            self.canvas.release()

    def set_width(self, width):
        width = max(120, min(900, width))
        self.resize(width, round(width * 1.5) + 28)
        self.minus.setEnabled(width > 120)
        self.plus.setEnabled(width < 900)
        self.geometry_changed.emit()

    def scale(self, delta):
        # Size limits protect resource use. Position is never clamped to a screen.
        self.set_width(self.width() + delta)

    def set_state(self, symbol, text):
        self.symbol.setPixmap(icon(symbol).pixmap(18, 18))
        self.state_label.setText(text)
        self.state_label.setToolTip(text)

    def mousePressEvent(self, event):
        self.begin_drag(event)

    def mouseMoveEvent(self, event):
        self.continue_drag(event)

    def mouseReleaseEvent(self, event):
        self.finish_drag()


class DesktopViews:
    """GUI-owned views, routed to the one runtime owned by the main window."""

    def __init__(self, main, path):
        self.main, self.path = main, path
        self.problem = ""
        self.writable = True
        try:
            self.preferences = read_preferences(path)
        except (OSError, ValueError) as exc:
            from esi02.desktop_preferences import DesktopPreferences

            self.preferences = DesktopPreferences()
            self.writable = False  # Keep the damaged/original file for diagnosis.
            self.problem = f"表示設定を読み込めません。元ファイルは保持しました：{exc}"
        self.microphone = MicrophoneButton()
        self.avatar = AvatarWindow()
        if self.preferences.model_path:
            try:
                self.avatar.set_model(self.preferences.model_path)
            except Exception as exc:
                self.avatar.problem = f"モデルを読み込めません。立ち絵を表示します：{exc}"
        self.closed = False
        self.timer = QTimer(main)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.flush)
        area = primary_area()
        self.microphone.move(
            QPoint(*self.preferences.microphone_position)
            if self.preferences.microphone_position is not None
            else QPoint(area.right() - 90, area.center().y() - 36)
        )
        self.avatar.set_width(self.preferences.avatar_width)
        self.avatar.move(
            QPoint(*self.preferences.avatar_position)
            if self.preferences.avatar_position is not None
            else QPoint(area.right() - self.avatar.width() - 100, area.top() + 60)
        )
        self.microphone.clicked.connect(main.desktop_microphone)
        self.microphone.start_requested.connect(main.toggle)
        self.microphone.main_requested.connect(main.show_main)
        self.microphone.reset_requested.connect(lambda: self.reset_position("microphone"))
        for name, widget in (("microphone", self.microphone), ("avatar", self.avatar)):
            widget.geometry_changed.connect(self.changed)
            widget.hidden_requested.connect(lambda n=name: main.set_desktop_visible(n, False))
            widget.setVisible(getattr(self.preferences, name + "_visible"))

    def changed(self):
        if not self.closed:
            self.timer.start(200)

    def set_visible(self, name, enabled):
        setattr(self.preferences, name + "_visible", enabled)
        getattr(self, name).setVisible(enabled)
        self.changed()

    def reset_position(self, name):
        area = primary_area()
        widget = getattr(self, name)
        widget.move(
            QPoint(area.right() - 90, area.center().y() - 36)
            if name == "microphone"
            else QPoint(area.right() - widget.width() - 100, area.top() + 60)
        )
        self.changed()

    def flush(self):
        self.timer.stop()
        for name in ("microphone", "avatar"):
            point = getattr(self, name).pos()
            setattr(self.preferences, name + "_position", [point.x(), point.y()])
        self.preferences.avatar_width = self.avatar.width()
        if self.writable:
            try:
                write_preferences(self.path, self.preferences)
                self.problem = ""
            except (OSError, ValueError) as exc:
                self.problem = f"表示設定を保存できません：{exc}"

    def refresh(self, running, microphone, symbol, text, problem):
        if isinstance(self.avatar.canvas, AudioLiveCanvas):
            self.avatar.canvas.meter = getattr(self.main.runtime, "playback_meter", None) or self.avatar.meter
        self.microphone.set_state(running, microphone, problem or self.problem)
        self.avatar.set_state(symbol, text)

    def shutdown(self):
        self.flush()
        self.closed = True
        self.timer.deleteLater()
        for widget in (self.microphone, self.avatar):
            if widget is self.avatar:
                self.avatar.release()
            widget.hide()
            widget.deleteLater()
