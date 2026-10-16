"""Adaptive questioning and autonomous activity controls without a question timer."""

from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel

from esi03.growth_gui import GrowthPanel as PreviousPanel
from esi04.affect import LABELS


class GrowthPanel(PreviousPanel):
    def _identity(self):
        super()._identity()
        self.style.setItemText(self.style.findData("precise"), "丁寧（標準・です／ます）")
        self.style.setItemText(self.style.findData("gentle"), "やわらかい敬語")
        self.style.setItemText(self.style.findData("casual"), "親しみのある常体")
        self.style.setToolTip("通常の会話にも反映します。標準は敬語で、引用内の言葉は変えません。")
        self.affect_status = QLabel()
        self.affect_status.setWordWrap(True)
        self.style.parentWidget().layout().addRow("現在の感情の傾向", self.affect_status)
        self.style.parentWidget().layout().addRow(QLabel("モデルの一般知識を使って会話します。教わった記憶・観測とは区別します。"))

    def refresh(self):
        super().refresh()
        affect = self.controller.session.cognition.affect
        self.affect_status.setText(" / ".join(f"{label} {getattr(affect, key):.0%}" for key, label in LABELS.items())
                                  + ("\n"+affect.reason if affect.reason else ""))

    def _interests(self):
        super()._interests()
        pane = self.question_interval.parentWidget()
        layout = pane.layout()
        form = next((layout.itemAt(i).layout() for i in range(layout.count())
                     if isinstance(layout.itemAt(i).layout(), QFormLayout)), None)
        if form:
            form.setRowVisible(self.question_interval, False)
            form.insertRow(2, QLabel("質問の時機は、関心・欲求・相手の都合と他の活動を比べて選びます。"))
            self.idle_activity = QCheckBox("会話がない間も、体験を見直して活動する")
            self.autonomous_motion = QCheckBox("感情と意図に合わせて、自分で体を動かす")
            self.cursor_tracking = QCheckBox("カーソルに視線を合わせる")
            self.thought_bubbles = QCheckBox("アバターの上に活動の吹き出しを表示する")
            for check in (self.idle_activity, self.autonomous_motion, self.cursor_tracking, self.thought_bubbles):
                form.insertRow(form.rowCount()-1, check)
        self.budget.setToolTip("Web・フォルダーの自律探索に使う上限です。質問の間隔は指定しません。")

    def save_preferences(self):
        self.controller.character_action("preferences", {
            "proactive": self.proactive.isChecked(), "exploration": self.exploration.isChecked(),
            "min_question_interval": 0.0, "daily_tool_budget": self.budget.value(),
            "max_attachment": self.attachment.value()/100,
            "idle_activity": self.idle_activity.isChecked(), "autonomous_motion": self.autonomous_motion.isChecked(),
            "cursor_tracking": self.cursor_tracking.isChecked(), "thought_bubbles": self.thought_bubbles.isChecked(),
        })

    def load_controls(self):
        super().load_controls()
        preferences = self.controller.session.experience.state.preferences
        for key in ("idle_activity", "autonomous_motion", "cursor_tracking", "thought_bubbles"):
            getattr(self, key).setChecked(getattr(preferences, key))
