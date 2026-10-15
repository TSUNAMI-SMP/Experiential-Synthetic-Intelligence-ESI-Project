"""Main-thread screen capture and explicit, session-only computer access controls."""

import os
import queue
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap

from esi.models import utc
from esi04.computer import ComputerHub, ComputerMemory
from esi04.computer_models import Rect
from esi04.computer_panel import ComputerPanel as ComputerPanel
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
        self.recording_game = False
        self.native_recorder = None
        self.last_demo_frame = None
        self.sequence = 0
        self.emergency_latched = False
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
        # UI Automation can take seconds in a browser; a game canvas needs fresh frames.
        elements = [] if self.hub.interactive or self.recording_game else self.hub.native.elements(foreground)
        return foreground, elements

    def poll(self):
        try:
            fast = self.hub.interactive or self.recording_game
            self.timer.setInterval(80 if fast else 250)
            if self.hub.native.emergency_pressed():
                if not self.emergency_latched:
                    self.emergency_latched = True
                    self.revoke()
                    self.problem.emit("F8で予約・画面共有とPC操作を停止しました。")
                return
            self.emergency_latched = False
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
                        if (current.application == self.recording.application
                                and (not self.hub.grant.handle or current.handle == self.hub.grant.handle)):
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
                or (grant.handle and current.handle != grant.handle)
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
        try:
            native.start()
        except Exception:
            native.stop()
            raise
        self.recording, self.native_recorder = demonstration, native
        self.last_demo_frame = self.hub.latest
        self.changed.emit()

    def start_game_recording(self, profile):
        from esi04.games import GameDemonstration
        if self.recording or self.hub.interactive:
            raise RuntimeError("記録や操作を停止してから実演してください。")
        demonstration = GameDemonstration(self.hub, profile)
        native = recorder(game=True)
        try:
            native.start()
        except Exception:
            native.stop()
            raise
        self.recording_game = True
        self.recording, self.native_recorder = demonstration, native
        self.last_demo_frame = self.hub.latest
        self.changed.emit()

    def stop_recording(self, save=True):
        if self.native_recorder:
            self.native_recorder.stop()
        self.native_recorder = None
        self.recording_game = False
        demonstration, self.recording = self.recording, None
        if demonstration:
            if save:
                if self.last_demo_frame:
                    demonstration.captured(self.last_demo_frame)
                skill = demonstration.finish()
                if hasattr(skill, "steps"):
                    self.skill_saved.emit(skill.id)
            else:
                self.hub.memory.pinned.clear()
        self.changed.emit()

    def shutdown(self):
        self.revoke()
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
