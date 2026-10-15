"""Main-thread screen capture and explicit, session-only computer access controls."""

import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.models import uid, utc
from esi04.computer import ComputerHub, ComputerMemory
from esi04.computer_models import Rect, ScreenTeaching
from esi04.demonstration import Demonstration
from esi04.native_computer import backend, recorder


def physical_screen(screen):
    geometry = screen.geometry()
    ratio = screen.devicePixelRatio()
    fallback = Rect(x=round(geometry.x()*ratio), y=round(geometry.y()*ratio),
                    width=round(geometry.width()*ratio), height=round(geometry.height()*ratio))
    if os.name != "nt":
        return fallback
    # Windows physical monitor origins are independent of Qt's logical/DPI coordinates.
    import ctypes
    from ctypes import wintypes as w
    class Info(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD),
                    ("device", w.WCHAR*32)]
    matches = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    user = ctypes.windll.user32
    user.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(Info)]
    def visit(handle, dc, rect, data):
        info = Info(size=ctypes.sizeof(Info))
        if user.GetMonitorInfoW(handle, ctypes.byref(info)) and info.device.casefold() == screen.name().casefold():
            r = info.monitor
            matches.append(Rect(x=r.left, y=r.top, width=r.right-r.left, height=r.bottom-r.top))
        return True
    callback = callback_type(visit)
    user.EnumDisplayMonitors(None, None, callback, 0)
    return matches[0] if matches else fallback


class ComputerSupervisor(QObject):
    changed = Signal()
    problem = Signal(str)
    skill_saved = Signal(str)

    def __init__(self, root, limit_mb=512, parent=None, native=None):
        super().__init__(parent)
        self.hub = ComputerHub(ComputerMemory(root, limit_mb), native or backend())
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi04-accessibility")
        self.future = None
        self.sample_generation = None
        self.recording = None
        self.native_recorder = None
        self.last_demo_frame = None
        self.sequence = 0
        self.last_png_hash = ""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(180 if os.name != "nt" else 250)

    def configure(self, **values):
        self.stop_recording(save=False)
        grant = self.hub.configure(**values)
        self.last_png_hash = ""
        self.changed.emit()
        return grant

    def revoke(self):
        self.hub.revoke()
        self.stop_recording(save=False)
        self.changed.emit()

    def metadata(self):
        foreground = self.hub.native.foreground()
        elements = self.hub.native.elements(foreground)
        return foreground, elements

    def poll(self):
        try:
            if self.native_recorder:
                if self.native_recorder.error:
                    raise RuntimeError(self.native_recorder.error)
                while True:
                    try:
                        event = self.native_recorder.events.get_nowait()
                    except queue.Empty:
                        break
                    if self.recording:
                        current = self.hub.native.foreground()
                        if current.application == self.recording.application:
                            self.recording.event(event, self.hub.latest)
            if self.future and self.future.done():
                future, self.future = self.future, None
                foreground, elements = future.result()
                grant = self.hub.grant
                if grant.observe and self.sample_generation == grant.generation:
                    self.capture(foreground, elements, grant)
            if self.hub.grant.observe and self.future is None:
                self.sample_generation = self.hub.grant.generation
                self.future = self.pool.submit(self.metadata)
        except Exception as exc:
            self.revoke()
            self.problem.emit(str(exc))

    def capture(self, foreground, elements, grant):
        screen = next((s for s in QGuiApplication.screens() if s.name() == grant.screen), None)
        if screen is None:
            raise RuntimeError("選択した画面が接続されていません。画面共有を停止しました。")
        current = self.hub.native.foreground()
        if (current.handle != foreground.handle or (grant.pid and current.pid != grant.pid)
                or (grant.application and current.application != grant.application)):
            return
        physical = physical_screen(screen)
        geometry = screen.geometry()
        logical = Rect(x=geometry.x(), y=geometry.y(), width=geometry.width(), height=geometry.height())
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            raise RuntimeError("画面を取得できません。OSの画面収録設定を確認してください。")
        if grant.application and foreground.bounds:
            area = foreground.bounds
            left, top = max(area.x, physical.x), max(area.y, physical.y)
            right, bottom = min(area.x+area.width, physical.x+physical.width), min(area.y+area.height, physical.y+physical.height)
            if left >= right or top >= bottom:
                return
            image = pixmap.toImage()
            crop = image.copy(round((left-physical.x)*image.width()/physical.width),
                              round((top-physical.y)*image.height()/physical.height),
                              round((right-left)*image.width()/physical.width),
                              round((bottom-top)*image.height()/physical.height))
            pixmap = QPixmap.fromImage(crop)
            logical = Rect(x=logical.x+round((left-physical.x)*logical.width/physical.width),
                           y=logical.y+round((top-physical.y)*logical.height/physical.height),
                           width=max(1, round((right-left)*logical.width/physical.width)),
                           height=max(1, round((bottom-top)*logical.height/physical.height)))
            physical = Rect(x=left, y=top, width=right-left, height=bottom-top)
        # Bound visual inference memory/cost while retaining the original coordinate mapping.
        image = pixmap.toImage()
        if image.width() > 1600 or image.height() > 1200:
            image = image.scaled(1600, 1200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        png = bytes(data)
        self.sequence += 1
        metadata = dict(sequence=self.sequence, generation=grant.generation, captured_at=time.monotonic(),
                        observed_at=utc(), screen=screen.name(), physical=physical, logical=logical,
                        image_width=image.width(), image_height=image.height(), foreground=foreground,
                        elements=elements, cursor=self.hub.native.cursor())
        frame = self.hub.memory.save_frame(png, **metadata)
        if self.hub.publish(frame):
            if self.recording:
                self.recording.captured(frame)
                if frame.foreground.application == self.recording.application:
                    self.last_demo_frame = frame
            self.changed.emit()

    def start_recording(self, name, goal):
        if self.recording:
            raise RuntimeError("実演を記録中です。")
        if not self.hub.latest:
            raise RuntimeError("共有画面が届いてから実演を開始してください。")
        demonstration = Demonstration(self.hub, name, goal)
        native = recorder()
        native.start()
        self.recording, self.native_recorder = demonstration, native
        self.last_demo_frame = self.hub.latest
        self.changed.emit()

    def stop_recording(self, save=True):
        if self.native_recorder:
            self.native_recorder.stop()
        self.native_recorder = None
        demonstration, self.recording = self.recording, None
        if demonstration:
            if save:
                if self.last_demo_frame:
                    demonstration.captured(self.last_demo_frame)
                skill = demonstration.finish()
                self.skill_saved.emit(skill.id)
            else:
                self.hub.memory.pinned.clear()
        self.changed.emit()

    def shutdown(self):
        self.revoke()
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)


class ComputerPanel(QWidget):
    def __init__(self, window, supervisor):
        super().__init__()
        self.window, self.supervisor = window, supervisor
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("選択した画面を見せたり、操作の実演を教えたりできます。アクセスは終了・停止時に解除されます。"))
        form = QFormLayout()
        self.screens = QComboBox()
        self.refresh_screens()
        form.addRow("共有する画面", self.screens)
        self.allow_observe = QCheckBox("画面の観察を許可")
        self.allow_control = QCheckBox("選択画面内でマウス・キーボード操作を許可")
        self.allow_record = QCheckBox("実演の記録を許可（記録開始時だけ記録）")
        self.allow_move = QCheckBox("操作中にアバターが自分で位置を変える")
        self.allow_move.setChecked(True)
        for check in (self.allow_observe, self.allow_control, self.allow_record, self.allow_move):
            form.addRow(check)
        layout.addLayout(form)
        row = QHBoxLayout()
        for title, callback in (("設定を適用", self.apply), ("すべて停止", self.stop), ("画面一覧を更新", self.refresh_screens)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel("画面共有・PC操作：停止")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.question = QLineEdit()
        self.question.setPlaceholderText("例：この画面の青いボタンには何と書いてある？")
        layout.addWidget(self.question)
        row = QHBoxLayout()
        observe = QPushButton("今の画面を見てもらう")
        observe.clicked.connect(lambda: self.command("observe", {"question": self.question.text() or "今の画面には何が見える？"}))
        row.addWidget(observe)
        teach = QPushButton("画面についての説明を教える")
        teach.clicked.connect(self.teach)
        row.addWidget(teach)
        layout.addLayout(row)
        self.skill_name, self.skill_goal = QLineEdit(), QLineEdit()
        self.skill_name.setPlaceholderText("実演の名前（例：メモを書く）")
        self.skill_goal.setPlaceholderText("何を達成する操作か")
        layout.addWidget(self.skill_name)
        layout.addWidget(self.skill_goal)
        row = QHBoxLayout()
        for label, callback in (("実演の記録を開始", self.record), ("記録を終了して覚える", self.finish_record),
                                ("記録を破棄", lambda: self.supervisor.stop_recording(False))):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.skills = QListWidget()
        layout.addWidget(self.skills)
        replay = QPushButton("選んだ実演を再現する")
        replay.clicked.connect(self.replay)
        layout.addWidget(replay)
        self.observation = QTextEdit()
        self.observation.setReadOnly(True)
        self.observation.setMaximumHeight(170)
        layout.addWidget(self.observation)
        supervisor.problem.connect(self.status.setText)
        supervisor.changed.connect(self.refresh)
        supervisor.skill_saved.connect(self.skill_added)
        self.loaded_skills = None
        self.refresh()

    def refresh_screens(self):
        self.screens.clear()
        for screen in QGuiApplication.screens():
            self.screens.addItem(screen.name(), screen.name())

    def apply(self):
        try:
            self.supervisor.configure(screen=self.screens.currentData() or "", observe=self.allow_observe.isChecked(),
                                      control=self.allow_control.isChecked(), demonstrate=self.allow_record.isChecked(),
                                      move_avatar=self.allow_move.isChecked())
        except Exception as exc:
            self.status.setText(str(exc))

    def stop(self):
        self.supervisor.revoke()
        for check in (self.allow_observe, self.allow_control, self.allow_record):
            check.setChecked(False)
        if self.window.runtime:
            self.window.send("stop_computer", None)

    def command(self, operation, values):
        if not self.window.runtime:
            self.status.setText("会話を開始してから画面認識・PC操作を使ってください。")
            return
        self.window.send("computer", (operation, values))

    def teach(self):
        text, frame = self.question.text().strip(), self.supervisor.hub.latest
        if not text or frame is None:
            self.status.setText("画面を共有し、説明を入力してください。")
            return
        entry = ScreenTeaching(id=uid(), frame_id=frame.id, frame_sha256=frame.sha256,
                               user_text=text, observed_at=utc())
        self.supervisor.hub.memory.record("teachings", entry)
        self.command("teaching", entry.model_dump())

    def record(self):
        self.status.setText("対象アプリへ切り替えてください。3秒後に実演の記録を開始します。")
        QTimer.singleShot(3000, self.begin_record)

    def begin_record(self):
        try:
            self.supervisor.start_recording(self.skill_name.text(), self.skill_goal.text())
        except Exception as exc:
            self.status.setText(str(exc))

    def finish_record(self):
        try:
            self.supervisor.stop_recording()
        except Exception as exc:
            self.status.setText(str(exc))

    def skill_added(self, identifier):
        self.loaded_skills = None
        self.refresh()
        if self.window.runtime:
            self.window.send("computer", ("skill_learned", {"id": identifier}))

    def replay(self):
        item = self.skills.currentItem()
        if item:
            self.command("replay", {"id": item.data(Qt.ItemDataRole.UserRole)})

    def refresh(self):
        grant, frame = self.supervisor.hub.grant, self.supervisor.hub.latest
        self.status.setText(("画面共有："+grant.screen+" / PC操作："+("許可" if grant.control else "停止")
                            + (" / 実演を記録中" if self.supervisor.recording else "")
                            + (" / "+frame.foreground.application if frame else " / 画面を待っています"))
                           if grant.observe else "画面共有・PC操作：停止")
        if self.loaded_skills is None:
            self.skills.clear()
            skills = self.supervisor.hub.memory.skills()
            for skill in skills:
                self.skills.addItem(skill.name+f"（{len(skill.steps)}操作）")
                self.skills.item(self.skills.count()-1).setData(Qt.ItemDataRole.UserRole, skill.id)
            self.loaded_skills = len(skills)
