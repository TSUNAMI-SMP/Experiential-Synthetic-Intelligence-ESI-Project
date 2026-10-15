"""Clickable activity summaries and self-chosen avatar positions during permitted PC work."""

import math
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout

from esi02.avatar_assets import BUNDLED_MODEL as PREVIOUS_MODEL
from esi02.desktop import AvatarWindow as PreviousAvatar
from esi02.desktop import DesktopViews as PreviousViews
from esi02.desktop_preferences import DesktopPreferences, write_preferences
from esi04.motion import AutonomousCanvas

BUNDLED_MODEL = "builtin:rinne-el04"


def bundled_model_path():
    return Path(__file__).resolve().parents[1] / "esi/resources/rinne-el04/rinne-el.model3.json"


class AvatarWindow(PreviousAvatar):
    canvas_class = AutonomousCanvas

    def __init__(self, *args, **kwargs):
        self.manual_attention = None
        self.current_attention = None
        super().__init__(*args, **kwargs)

    def set_model(self, path):
        if path in {BUNDLED_MODEL, PREVIOUS_MODEL}:
            path = bundled_model_path()
        super().set_model(path)

    def begin_drag(self, event):
        self.manual_attention = self.current_attention
        super().begin_drag(event)


class DesktopViews(PreviousViews):
    avatar_class = AvatarWindow

    def __init__(self, main, path):
        if not Path(path).exists():
            write_preferences(path, DesktopPreferences(model_path=BUNDLED_MODEL))
        super().__init__(main, path)
        self.bubble = QPushButton(None)
        self.bubble.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                  | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.bubble.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.bubble.setStyleSheet("QPushButton { background: #e6f4f5; color: #173847; border: 1px solid #7dbac4; border-radius: 12px; padding: 8px; }")
        self.bubble.setMaximumWidth(250)
        self.bubble.setToolTip("クリックして、今の活動・関心・選んだ理由・確かめた結果を見る")
        self.bubble.clicked.connect(self.show_summaries)
        self.summary_dialog = None
        self.motion_target = None
        self.motion_timer = QTimer(main)
        self.motion_timer.timeout.connect(self.advance_position)
        self.motion_timer.start(40)
        self.summary_signature = None

    def state(self):
        with self.main.session.lock:
            session = self.main.session
            preferences = session.experience.state.preferences
            cognition = session.cognition.model_dump(exclude={"processed", "screen_teachings", "last_screen"})
            cognition["summaries"] = cognition["summaries"][-12:]
            body = session.world.body.model_dump()
            if session.plugins.active_id:
                external = session.environment_world().body
                body.update(energy=external.energy, fatigue=0, hydration=1, condition=external.condition)
            state = {"cognition": cognition, "body": body, "autonomous_motion": preferences.autonomous_motion,
                     "cursor_tracking": preferences.cursor_tracking, "thought_bubbles": preferences.thought_bubbles}
            obj = next((o for o in session.world.objects if o.id == session.cognition.attention_object), None) if not session.plugins.active_id else None
            if obj:
                state["gaze"] = (max(-1, min(1, (obj.position.x-session.world.position.x)/3)),
                                 max(-1, min(1, (obj.position.y-1.4)/2)))
        supervisor = getattr(self.main, "computer_supervisor", None)
        if supervisor and supervisor.hub.attention and supervisor.hub.latest and supervisor.hub.attention.get("point"):
            frame, point = supervisor.hub.latest, supervisor.hub.attention["point"]
            if frame.physical.contains(*point):
                x, y = frame.logical_point(*point)
                state["gaze"] = (max(-1, min(1, (x-self.avatar.x()-self.avatar.width()/2)/400)),
                                 max(-1, min(1, -(y-self.avatar.y()-self.avatar.height()*0.2)/400)))
        return state

    def refresh(self, *args):
        super().refresh(*args)
        state = self.state()
        if isinstance(self.avatar.canvas, AutonomousCanvas):
            self.avatar.canvas.state_provider = self.state
        cognition = state["cognition"]
        summaries = cognition["summaries"]
        label = summaries[-1]["activity"]+"："+summaries[-1]["subject"] if summaries else "今は、周りの様子を見ているよ"
        self.bubble.setText(label[:60]+"  …")
        self.bubble.adjustSize()
        self.bubble.move(self.avatar.x()+max(0, self.avatar.width()-self.bubble.width()), self.avatar.y())
        self.bubble.setVisible(self.avatar.isVisible() and state["thought_bubbles"])
        self.choose_position(state)
        if self.summary_dialog and self.summary_dialog.isVisible():
            self.update_summary()

    def choose_position(self, state):
        supervisor = getattr(self.main, "computer_supervisor", None)
        if not supervisor:
            return
        hub, avatar = supervisor.hub, self.avatar
        attention, frame = hub.attention, hub.latest
        if (not hub.grant.control or not hub.grant.move_avatar or not state["autonomous_motion"]
                or not avatar.isVisible() or avatar.drag_origin or not attention or not frame
                or not attention.get("point")):
            self.motion_target = None
            return
        key = (attention["frame_id"], attention["at"])
        if avatar.manual_attention == key:
            self.motion_target = None
            return
        if avatar.current_attention == key:
            return
        avatar.current_attention = key
        if avatar.manual_attention == key:
            return
        if not frame.physical.contains(*attention["point"]):
            return
        x, y = frame.logical_point(*attention["point"])
        screen = next((s for s in QGuiApplication.screens() if s.name() == hub.grant.screen), None)
        if not screen:
            return
        area = screen.availableGeometry()
        w, h = avatar.width(), avatar.height()
        candidates = [(x-w-30, y-h*0.25), (x+35, y-h*0.25),
                      (area.left()+15, area.top()+30), (area.right()-w-15, area.top()+30),
                      (area.left()+15, area.bottom()-h-15), (area.right()-w-15, area.bottom()-h-15)]
        choices = []
        for px, py in candidates:
            px = max(area.left(), min(area.right()-w, round(px)))
            py = max(area.top(), min(area.bottom()-h, round(py)))
            covers_target = px-24 <= x <= px+w+24 and py-24 <= y <= py+h+24
            score = math.hypot(px+w/2-x, py+h*0.2-y)+5000*int(covers_target)
            choices.append((score, QPoint(px, py)))
        self.motion_target = min(choices, key=lambda i: i[0])[1]
        if self.main.runtime:
            self.main.send("avatar_move", {"target": [self.motion_target.x(), self.motion_target.y()],
                                           "frame": frame.id, "reason": "見ている操作対象を隠さず、その近くから観察する位置を選んだ"})

    def advance_position(self):
        if self.closed or self.avatar.drag_origin or not self.motion_target:
            return
        supervisor = getattr(self.main, "computer_supervisor", None)
        if not supervisor or not supervisor.hub.grant.move_avatar:
            self.motion_target = None
            return
        delta = self.motion_target-self.avatar.pos()
        if delta.manhattanLength() < 3:
            self.avatar.move(self.motion_target)
            self.motion_target = None
        else:
            self.avatar.move(self.avatar.pos()+QPoint(round(delta.x()*0.1), round(delta.y()*0.1)))

    def show_summaries(self):
        if self.summary_dialog is None:
            self.summary_dialog = QDialog(self.main)
            self.summary_dialog.setWindowTitle("今の活動と関心")
            self.summary_dialog.resize(620, 500)
            layout = QVBoxLayout(self.summary_dialog)
            self.summary_text = QTextBrowser()
            layout.addWidget(self.summary_text)
        self.update_summary()
        self.summary_dialog.show()
        self.summary_dialog.raise_()

    def update_summary(self):
        state = self.state()
        cognition = state["cognition"]
        lines = ["今していること："+cognition["activity"], "注意を向けているもの："+cognition["attention"], ""]
        for entry in reversed(cognition["summaries"]):
            lines.extend([entry["activity"]+" — "+entry["subject"], "選んだ理由："+entry["reason"]])
            if entry["alternatives"]:
                lines.append("比べた選択肢："+"、".join(entry["alternatives"]))
            if entry["outcome"]:
                lines.append("結果："+entry["outcome"])
            lines.extend(["記録の種類："+{"observed": "観察・確認したこと", "inferred": "推測・行動の案", "planned": "これから行う予定"}[entry["certainty"]], ""])
        self.summary_text.setPlainText("\n".join(lines))

    def shutdown(self):
        self.motion_timer.stop()
        self.bubble.close()
        self.bubble.deleteLater()
        if self.summary_dialog:
            self.summary_dialog.close()
        super().shutdown()
