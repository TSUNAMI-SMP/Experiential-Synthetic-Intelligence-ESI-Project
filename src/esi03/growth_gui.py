"""Character settings and visible reasons for learning, questions and research."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi03.personality import NEED_LABELS, TRAIT_LABELS

VALUE_LABELS = {
    "self_respect": "自己尊重",
    "honesty": "誠実さ",
    "growth": "成長を大切にする",
    "reciprocity": "相互尊重",
    "independence": "自立",
}
STAGE_LABELS = {
    "open": "知りたい",
    "asking": "尋ねている",
    "investigating": "確かめている",
    "resolved": "記録を得た",
    "paused": "保留",
    "pending": "待機",
    "running": "実行中",
    "complete": "記録済み",
    "cancelled": "中止",
    "failed": "未取得",
}


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().hide()
    return widget


def populate(widget, rows, identifiers=None):
    current = widget.currentRow()
    selected = (
        widget.item(current, 0).data(Qt.ItemDataRole.UserRole)
        if current >= 0 and widget.item(current, 0)
        else None
    )
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            cell = QTableWidgetItem(str(value))
            if column == 0 and identifiers is not None:
                cell.setData(Qt.ItemDataRole.UserRole, identifiers[row])
            widget.setItem(row, column, cell)
        if identifiers is not None and identifiers[row] == selected:
            widget.selectRow(row)


class GrowthPanel(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.loaded_agent = None
        self.signature = None
        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addWidget(self.tabs, 1)
        self._identity()
        self._needs()
        self._interests()
        self._beliefs()
        self._research()
        self._history()

    def _identity(self):
        pane = QWidget()
        layout = QFormLayout(pane)
        self.name = QLineEdit()
        self.name.setMaxLength(40)
        self.style = QComboBox()
        for text, value in [
            ("親しみのある話し方", "casual"),
            ("穏やかな話し方", "gentle"),
            ("丁寧な話し方", "precise"),
        ]:
            self.style.addItem(text, value)
        layout.addRow("名前", self.name)
        layout.addRow("口調", self.style)
        self.axes = {}
        for group, labels in [("values", VALUE_LABELS), ("traits", TRAIT_LABELS)]:
            layout.addRow(
                QLabel(
                    "人格の軸（経験だけでは変わりません）"
                    if group == "values"
                    else "経験で緩やかに変わる傾向"
                )
            )
            for key, label in labels.items():
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(0, 100)
                slider.setToolTip("0〜100。右ほど強い傾向です。")
                self.axes[group, key] = slider
                layout.addRow(label, slider)
        self.plasticity = QSlider(Qt.Orientation.Horizontal)
        self.plasticity.setRange(0, 100)
        layout.addRow("経験による変わりやすさ", self.plasticity)
        apply = QPushButton("性格と人格の軸を保存")
        apply.clicked.connect(self.save_character)
        layout.addRow(apply)
        self.tabs.addTab(pane, "性格")

    def _needs(self):
        pane = QWidget()
        layout = QFormLayout(pane)
        layout.addRow(QLabel("現在の仮想の欲求。行動の選択には身体の維持と休息も影響します。"))
        self.needs = {}
        for key, label in NEED_LABELS.items():
            meter = QProgressBar()
            meter.setRange(0, 100)
            self.needs[key] = meter
            layout.addRow(label, meter)
        self.habits = table(["活動", "経験数", "期待する報酬", "好み", "慣れ", "満足"])
        layout.addRow(self.habits)
        self.modulators = QLabel()
        self.modulators.setWordWrap(True)
        layout.addRow(self.modulators)
        self.tabs.addTab(pane, "欲求・習慣")

    def _interests(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.proactive = QCheckBox("自分から気になることを尋ねる")
        self.exploration = QCheckBox("関心から自分で資料を調べる")
        self.question_interval = QSpinBox()
        self.question_interval.setRange(30, 3600)
        self.question_interval.setSuffix(" 秒")
        self.budget = QSpinBox()
        self.budget.setRange(0, 100)
        self.budget.setSuffix(" 回／日")
        self.attachment = QSpinBox()
        self.attachment.setRange(0, 75)
        self.attachment.setSuffix(" %")
        form = QFormLayout()
        form.addRow(self.proactive)
        form.addRow(self.exploration)
        form.addRow("質問の最短間隔", self.question_interval)
        form.addRow("探索・PC操作の上限", self.budget)
        form.addRow("愛着傾向の上限", self.attachment)
        save = QPushButton("自発性の設定を保存")
        save.clicked.connect(self.save_preferences)
        form.addRow(save)
        layout.addLayout(form)
        self.interests = table(["気になっていること", "理由", "状態", "試みた回数"])
        self.interests.itemSelectionChanged.connect(self.show_interest)
        layout.addWidget(self.interests, 1)
        self.interest_detail = QLabel()
        self.interest_detail.setWordWrap(True)
        layout.addWidget(self.interest_detail)
        row = QHBoxLayout()
        for text, callback in [
            ("関心を追加", self.add_interest),
            ("保留にする", self.pause_interest),
            ("選んだ疑問を調べる", self.research_interest),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.tabs.addTab(pane, "関心・成長目標")

    def _beliefs(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.beliefs = table(["対象", "現在の考え", "確信度", "食い違い"])
        self.beliefs.itemSelectionChanged.connect(self.show_belief)
        layout.addWidget(self.beliefs, 1)
        self.belief_detail = QTextEdit()
        self.belief_detail.setReadOnly(True)
        layout.addWidget(self.belief_detail, 1)
        button = QPushButton("教わった知識を追加・編集・削除")
        button.clicked.connect(self.controller.open_memories)
        layout.addWidget(button)
        self.tabs.addTab(pane, "信念・出典・訂正")

    def _research(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        form = QFormLayout()
        self.web = QCheckBox("Webで調べる")
        self.provider = QComboBox()
        for text, value in [("Bing", "bing"), ("DuckDuckGo", "duckduckgo"), ("Wikipedia", "wikipedia")]:
            self.provider.addItem(text, value)
        self.domains = QLineEdit()
        self.domains.setPlaceholderText("調べるホスト名をカンマで区切る")
        self.roots = QTextEdit()
        self.roots.setMaximumHeight(90)
        self.roots.setPlaceholderText("読めるフォルダーを一行に一つ。下のボタンで選択できます。")
        choose = QPushButton("調査用フォルダーを追加")
        choose.clicked.connect(self.choose_folder)
        self.notes = QLineEdit()
        choose_notes = QPushButton("学習ノートの保存先を選ぶ")
        choose_notes.clicked.connect(self.choose_notes)
        self.open_url = QCheckBox("取得したURLをブラウザーで開ける")
        self.open_file = QCheckBox("範囲内の資料を別のアプリで開ける")
        self.save_notes = QCheckBox("取得した資料を出典付きの学習ノートに保存する")
        form.addRow(self.web)
        form.addRow("検索先", self.provider)
        form.addRow("調べるWebの範囲", self.domains)
        form.addRow("読めるフォルダー", self.roots)
        form.addRow(choose)
        form.addRow("学習ノートの保存先", self.notes)
        form.addRow(choose_notes)
        for widget in (self.open_url, self.open_file, self.save_notes):
            form.addRow(widget)
        save = QPushButton("探索・PC操作の範囲を保存")
        save.clicked.connect(self.save_policy)
        form.addRow(save)
        layout.addLayout(form)
        self.tools_status = QLabel()
        self.tools_status.setWordWrap(True)
        layout.addWidget(self.tools_status)
        self.jobs = table(["操作", "状態", "取得した資料", "結果・理由"])
        layout.addWidget(self.jobs, 1)
        row = QHBoxLayout()
        for text, callback in [
            ("資料を開く", self.open_evidence),
            ("調査・操作を停止", lambda: self.controller.send("stop_tools")),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.tabs.addTab(pane, "探索・PC操作")

    def _history(self):
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.tabs.addTab(self.history, "成長の履歴")

    def save_character(self):
        data = self.controller.session.experience.state.character.model_dump()
        data.update(
            name=self.name.text().strip(),
            style=self.style.currentData(),
            plasticity=self.plasticity.value() / 100,
        )
        for group in ("values", "traits"):
            data[group] = {
                key: slider.value() / 100 for (scope, key), slider in self.axes.items() if scope == group
            }
        data["initial_traits"] = dict(data["traits"])
        self.controller.character_action("character", data)

    def save_preferences(self):
        self.controller.character_action(
            "preferences",
            {
                "proactive": self.proactive.isChecked(),
                "exploration": self.exploration.isChecked(),
                "min_question_interval": float(self.question_interval.value()),
                "daily_tool_budget": self.budget.value(),
                "max_attachment": self.attachment.value() / 100,
            },
        )

    def save_policy(self):
        data = self.controller.session.research.policy.model_dump()
        data.update(
            web_enabled=self.web.isChecked(),
            search_provider=self.provider.currentData(),
            allowed_domains=[value.strip() for value in self.domains.text().split(",") if value.strip()],
            read_roots=[value.strip() for value in self.roots.toPlainText().splitlines() if value.strip()],
            note_root=self.notes.text().strip(),
            allow_open_url=self.open_url.isChecked(),
            allow_open_file=self.open_file.isChecked(),
            allow_save_note=self.save_notes.isChecked(),
        )
        self.controller.tool_policy_action(data)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "読めるフォルダーを選択")
        if path:
            roots = self.roots.toPlainText().splitlines()
            if path not in roots:
                self.roots.setPlainText("\n".join(roots + [path]).strip())

    def choose_notes(self):
        path = QFileDialog.getExistingDirectory(self, "学習ノート用のフォルダーを選択")
        if path:
            self.notes.setText(path)

    def selected(self, widget):
        row = widget.currentRow()
        return (
            widget.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 and widget.item(row, 0) else None
        )

    def add_interest(self):
        topic, ok = QInputDialog.getText(self, "関心の方向", "気になっている話題")
        if not ok or not topic.strip():
            return
        question, ok = QInputDialog.getText(self, "疑問を追加", "どんなことを知りたいか")
        if ok and question.strip():
            self.controller.character_action(
                "interest", {"topic": topic.strip()[:120], "question": question.strip()[:240]}
            )

    def pause_interest(self):
        identifier = self.selected(self.interests)
        if identifier:
            self.controller.character_action("pause_interest", identifier)

    def research_interest(self):
        identifier = self.selected(self.interests)
        if identifier:
            self.controller.send("research_interest", identifier)

    def show_interest(self):
        identifier = self.selected(self.interests)
        item = next(
            (item for item in self.controller.session.experience.state.interests if item.id == identifier),
            None,
        )
        self.interest_detail.setText(
            item.question + "\n目標: 疑問を確かめ、出典と説明を記録する。" if item else ""
        )

    def show_belief(self):
        identifier = self.selected(self.beliefs)
        knowledge = self.controller.session.knowledge
        item = next((item for item in knowledge.entries() if item["id"] == identifier), None)
        if not item:
            self.belief_detail.clear()
            return
        lines = [
            item["label"],
            "現在の考え: " + item["value"],
            f"確信度: {item['confidence']:.0%}",
            "",
            "教え・資料の出典",
        ]
        for alternative in knowledge.alternatives(item["key"]):
            lines.append(f"\n{alternative['value']}（{alternative['confidence']:.0%}）")
            for evidence in alternative.get("evidence", []):
                lines.extend(
                    [
                        evidence["at"],
                        evidence.get("url") or evidence.get("path") or evidence["source"],
                        evidence["quote"],
                    ]
                )
        lines.append("\n変更の履歴")
        lines.extend(
            entry["at"] + " " + entry["reason"] + " → " + entry["value"]
            for entry in knowledge.history(item["key"], 20)
        )
        self.belief_detail.setPlainText("\n".join(lines))

    def open_evidence(self):
        identifier = self.selected(self.jobs)
        request = next(
            (request for request in self.controller.session.research.entries() if request.id == identifier),
            None,
        )
        if not request or not request.result.get("evidence_ids"):
            return
        document = self.controller.session.research.evidence(request.result["evidence_ids"][0])
        if document:
            action, args = (
                ("open_url", {"url": document["url"]})
                if document["kind"] == "web"
                else ("open_file", {"path": document["path"]})
            )
            self.controller.send("tool", (action, args))

    def load_controls(self):
        session = self.controller.session
        character, preferences = session.experience.state.character, session.experience.state.preferences
        self.name.setText(character.name)
        self.style.setCurrentIndex(self.style.findData(character.style))
        for (group, key), slider in self.axes.items():
            slider.setValue(round(getattr(getattr(character, group), key) * 100))
        self.plasticity.setValue(round(character.plasticity * 100))
        self.proactive.setChecked(preferences.proactive)
        self.exploration.setChecked(preferences.exploration)
        self.question_interval.setValue(round(preferences.min_question_interval))
        self.budget.setValue(preferences.daily_tool_budget)
        self.attachment.setValue(round(preferences.max_attachment * 100))
        policy = session.research.policy
        self.web.setChecked(policy.web_enabled)
        self.provider.setCurrentIndex(self.provider.findData(policy.search_provider))
        self.domains.setText(", ".join(policy.allowed_domains))
        self.roots.setPlainText("\n".join(policy.read_roots))
        self.notes.setText(policy.note_root)
        self.open_url.setChecked(policy.allow_open_url)
        self.open_file.setChecked(policy.allow_open_file)
        self.save_notes.setChecked(policy.allow_save_note)
        self.loaded_agent = session.agent_id

    def refresh(self):
        session = self.controller.session
        if session.agent_id != self.loaded_agent:
            self.load_controls()
        with session.lock:
            state = session.experience.state.model_copy(deep=True)
        self.summary.setText(
            f"{state.character.name}の成長 — 学習の記録 {state.acquired_claims} / 訂正の記録 {state.corrections} / 自分の質問 {state.questions} / 取得した資料 {state.source_documents}\n"
            f"現在の自発的質問: {'ON' if state.preferences.proactive else 'OFF'} / 自分で調べる: {'ON' if state.preferences.exploration else 'OFF'}"
        )
        for key, meter in self.needs.items():
            meter.setValue(round(getattr(state.needs, key) * 100))
        labels = {
            "question": "質問",
            "research": "調査",
            "conversation": "会話",
            "rest": "休息",
            "explore": "探索",
            "play": "遊び",
        }
        populate(
            self.habits,
            [
                (
                    labels[key],
                    habit.trials,
                    f"{habit.expected_reward:+.2f}",
                    f"{habit.strength:.0%}",
                    f"{habit.tolerance:.0%}",
                    f"{habit.satiation:.0%}",
                )
                for key, habit in state.habits.items()
            ],
        )
        self.modulators.setText(
            "仮想の調整信号: "
            + " / ".join(f"{key} {value:.2f}" for key, value in state.modulators.model_dump().items())
        )
        signature = (
            state.sequence,
            session.knowledge.revision,
            len(session.research.entries()),
            session.status.get("tools_phase"),
        )
        if signature != self.signature:
            self.signature = signature
            populate(
                self.interests,
                [
                    (item.topic, item.reason, STAGE_LABELS[item.stage], item.attempts)
                    for item in state.interests
                ],
                [item.id for item in state.interests],
            )
            beliefs = session.knowledge.entries()
            populate(
                self.beliefs,
                [
                    (
                        item["label"],
                        item["value"],
                        f"{item['confidence']:.0%}",
                        "再考中" if item.get("conflicted") else "",
                    )
                    for item in beliefs
                ],
                [item["id"] for item in beliefs],
            )
            requests = session.research.entries()
            populate(
                self.jobs,
                [
                    (
                        request.action,
                        STAGE_LABELS[request.stage],
                        len(request.result.get("evidence_ids", [])),
                        request.error,
                    )
                    for request in requests
                ],
                [request.id for request in requests],
            )
            self.history.setPlainText(
                "\n\n".join(
                    f"活動 {entry.at:.0f}秒 — {entry.description}\n根拠: {entry.source_id}"
                    for entry in reversed(state.traces)
                )
            )
        error = session.status.get("tools_error") or session.status.get("tools_learning_notice") or ""
        usage = session.store.meta("esi03_tool_usage") or {"count": 0}
        self.tools_status.setText(
            f"操作回数 {usage['count']} / {state.preferences.daily_tool_budget}　" + str(error)
        )
