import json
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.core import Core
from esi.experiments import compare
from esi.models import Observation, Outcome, uid
from esi.runtime import Runtime


class Plot(QWidget):
    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.labels, self.values = labels, []
        self.setMinimumHeight(130)

    def update_values(self, values):
        self.values = values[-300:]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#12212e"))
        colors = ["#63d6c2", "#ffc26f", "#ee7f9e", "#9dadff"]
        width, height = self.width(), self.height()
        painter.setPen(QColor("#e7edf5"))
        painter.drawText(10, 20, "  /  ".join(self.labels))
        if len(self.values) < 2:
            return
        for column, label in enumerate(self.labels):
            painter.setPen(QPen(QColor(colors[column % len(colors)]), 2))
            previous = None
            for i, row in enumerate(self.values):
                value = row[column]
                if value is None:
                    previous = None
                    continue
                point = QPointF(
                    10 + i * (width - 20) / (len(self.values) - 1),
                    height - 12 - max(0, min(1, value)) * (height - 42),
                )
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QTableWidget.NoEditTriggers)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            widget.setItem(i, j, QTableWidgetItem(str(value)))


class Window(QMainWindow):
    def __init__(self, core: Core):
        super().__init__()
        self.core = core
        self.runtime = None
        self.history = []
        self.study_pool = ThreadPoolExecutor(max_workers=1)
        self.study = None
        self.setWindowTitle("ESI-01 — 体験型合成知能 / 研究観察")
        self.resize(1280, 900)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.heading = QLabel("ESI-01  |  体験型合成知能")
        self.heading.setStyleSheet("font-size: 23px; font-weight: bold; color: #63d6c2")
        layout.addWidget(self.heading)
        controls = QHBoxLayout()
        self.start_button = QPushButton("開始")
        self.start_button.clicked.connect(self.toggle)
        controls.addWidget(self.start_button)
        for label, callback in [
            ("保存", lambda: self.submit("save")),
            ("実験分岐", self.branch),
            ("録音ファイル入力", self.recording),
            ("マイク開始／停止", lambda: self.submit("microphone")),
            ("標準課題の比較実験", self.experiment),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        self.learning_box = QCheckBox("学習を有効")
        self.learning_box.setChecked(core.learning_enabled)
        self.learning_box.toggled.connect(lambda enabled: self.submit("learning", enabled))
        controls.addWidget(self.learning_box)
        layout.addLayout(controls)
        self.identity = QLabel()
        layout.addWidget(self.identity)
        self.state_plot = Plot(["覚醒 A", "負荷 L", "未解決度 U", "慣れ H（接触値 / 10）"])
        layout.addWidget(self.state_plot)
        middle = QSplitter(Qt.Horizontal)
        left, right = QWidget(), QWidget()
        left_layout, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        left_layout.addWidget(QLabel("知覚：文字起こしと時刻付き音響特徴"))
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMaximumHeight(110)
        left_layout.addWidget(self.transcript)
        self.audio_plot = Plot(["音量 RMS × 10", "F0 / 500 Hz（欠損は線を切断）"])
        left_layout.addWidget(self.audio_plot)
        left_layout.addWidget(QLabel("共有領域（最大容量あり）"))
        self.workspace_table = table(["イベント", "入力源", "観測内容"])
        left_layout.addWidget(self.workspace_table)
        right_layout.addWidget(QLabel("候補評価の内訳（未知の結果は学習しない）"))
        self.candidate_table = table(["候補", "回復", "証拠", "改善", "新規性", "仮説", "費用", "合計"])
        right_layout.addWidget(self.candidate_table)
        self.action_table = table(["行動 ID", "選択", "進行状態", "探索"])
        right_layout.addWidget(self.action_table)
        self.last_output = QLabel("音声出力：未実行")
        self.last_output.setWordWrap(True)
        right_layout.addWidget(self.last_output)
        middle.addWidget(left)
        middle.addWidget(right)
        layout.addWidget(middle, 1)
        stimulus = QHBoxLayout()
        stimulus.addWidget(QLabel("模擬刺激（管理命令には変換しません）"))
        self.stimulus = QLineEdit()
        stimulus.addWidget(self.stimulus, 1)
        inject = QPushButton("観測に追加")
        inject.clicked.connect(
            lambda: self.submit(
                "observe", Observation(text=self.stimulus.text(), category="research_stimulus")
            )
        )
        stimulus.addWidget(inject)
        layout.addLayout(stimulus)
        feedback = QHBoxLayout()
        feedback.addWidget(QLabel("研究者の結果訂正：対象行動"))
        self.action_ids = QComboBox()
        feedback.addWidget(self.action_ids, 1)
        self.evidence = QComboBox()
        self.evidence.addItems(["結果不明", "証拠取得あり", "証拠取得なし"])
        feedback.addWidget(self.evidence)
        self.resolve = QCheckBox("この行動の疑問が解決した")
        feedback.addWidget(self.resolve)
        correct = QPushButton("対象へ記録")
        correct.clicked.connect(self.feedback)
        feedback.addWidget(correct)
        layout.addLayout(feedback)
        self.diagnostics = QTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumHeight(150)
        layout.addWidget(self.diagnostics)
        self.statusBar().showMessage("停止中。開始を押すと状態更新・選択が続きます。")
        self.setStyleSheet(
            "QMainWindow, QWidget {background: #172633; color: #e7edf5;} QPushButton {padding: 8px; background: #294357;} QLineEdit, QTextEdit, QTableWidget {background: #12212e;} QHeaderView::section {background: #294357; padding: 5px;}"
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    def toggle(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
            self.start_button.setText("開始")
        else:
            self.runtime = Runtime(self.core)
            self.runtime.start()
            self.start_button.setText("停止")

    def submit(self, name, data=None):
        try:
            if self.runtime:
                if not self.runtime.send(name, data):
                    raise RuntimeError("処理待ちキューが満杯です。追加を記録できませんでした。")
            elif name == "save":
                self.core.checkpoint()
            elif name == "learning":
                self.core.learning(data)
            elif name == "branch":
                self.core.branch(data)
            else:
                raise RuntimeError("開始を押してから操作してください。")
            self.statusBar().showMessage(f"研究者操作を受付：{name}")
        except Exception as exc:
            self.statusBar().showMessage(str(exc))

    def recording(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "録音入力（ASR と音響特徴を抽出）", "", "音声 (*.wav *.flac *.ogg)"
        )
        if path:
            self.submit("recording", path)

    def branch(self):
        path, _ = QFileDialog.getSaveFileName(self, "別の実験 DB として保存", "", "SQLite (*.sqlite3)")
        if path:
            self.submit("branch", path)

    def feedback(self):
        aid = self.action_ids.currentData()
        if not aid:
            self.statusBar().showMessage("対象の行動がありません。")
            return
        action = self.core.actions[aid]
        choice = self.evidence.currentIndex()
        evidence = None if choice == 0 else 1.0 if choice == 1 else 0.0
        outcome = Outcome(
            action_id=aid,
            evidence=evidence,
            resolve_event_id=action.event_id if self.resolve.isChecked() and evidence == 1 else None,
            note="Researcher-specified evidence result; not a mood button",
        )
        self.submit("feedback", outcome)

    def experiment(self):
        if self.study and not self.study.done():
            return
        directory = self.core.store.path.parent / "experiments" / uid()
        self.study = self.study_pool.submit(compare, directory)
        self.statusBar().showMessage("学習あり／停止条件の標準課題を、ライブ状態と別の実験で比較中。")

    def refresh(self):
        view = self.core.view()
        self.identity.setText(
            f"agent {view['agent_id'][:12]}  /  branch {view['branch_id'][:12]}  /  活動 {view['elapsed']:.1f} 秒  /  学習 {'有効' if view['learning'] else '停止'}"
        )
        state = view["state"]
        self.history.append(
            (
                state["arousal"],
                state["load"],
                state["unresolved"],
                min(1, max(state["habituation"].values(), default=0) / 10),
            )
        )
        self.history = self.history[-300:]
        self.state_plot.update_values(self.history)
        workspace = view["workspace"]
        fill(self.workspace_table, [(e["event_id"][:12], e["source"], e["text"]) for e in workspace])
        if workspace:
            e = workspace[-1]
            self.transcript.setPlainText(
                f"{e['source']} / {e['duration']:.2f}秒 / ID {e['event_id']}\n{e['text']}\n推定信頼性は未校正 / 混入: {e['metadata'].get('tts_contamination_possible', False)}"
            )
            self.audio_plot.update_values(
                [(f["rms"] * 10, f["f0_hz"] / 500 if f["f0_hz"] else None) for f in e["acoustic_frames"]]
            )
        actions = view["actions"]
        fill(
            self.action_table, [(a["action_id"][:12], a["kind"], a["stage"], a["explored"]) for a in actions]
        )
        if actions:
            fill(
                self.candidate_table,
                [
                    (
                        c["kind"],
                        *(
                            f"{c['contributions'][k]:.3f}"
                            for k in ("recovery", "evidence", "progress", "novelty")
                        ),
                        f"{c['contributions']['llm_suggestion']:.3f}",
                        f"{c['contributions']['compute_cost'] + c['contributions']['output_cost']:.3f}",
                        f"{c['score']:.3f}",
                    )
                    for c in actions[-1]["candidates"]
                ],
            )
            selected = self.action_ids.currentData()
            current_ids = [self.action_ids.itemData(i) for i in range(self.action_ids.count())]
            if current_ids != [a["action_id"] for a in reversed(actions)]:
                self.action_ids.clear()
                for a in reversed(actions):
                    self.action_ids.addItem(f"{a['kind']} / {a['action_id'][:12]}", a["action_id"])
                if selected:
                    index = self.action_ids.findData(selected)
                    if index >= 0:
                        self.action_ids.setCurrentIndex(index)
            spoken = [a for a in actions if a["text"]]
            if spoken:
                a = spoken[-1]
                self.last_output.setText(
                    f"最後の出力（{a['stage']}）：{a['text']}\n音声: {a['audio_ref']}\n参照: {a['used_workspace_ids']}"
                )
        self.candidate_table.resizeColumnsToContents()
        outcomes = self.core.store.recent_records("outcome", limit=3)
        self.diagnostics.setPlainText(
            json.dumps(
                {
                    "技術状態": view["status"],
                    "学習済み成分数": view["counts"],
                    "直近結果と予測誤差": [r["payload"] for r in outcomes],
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        if self.study and self.study.done():
            try:
                report = self.study.result()
                self.statusBar().showMessage(
                    "比較完了：留保 MSE 学習あり %.4f ／ 停止 %.4f"
                    % tuple(report["conditions"][k]["heldout"]["mse"] for k in ("learning", "frozen"))
                )
            except Exception as exc:
                self.statusBar().showMessage(str(exc))
            self.study = None

    def closeEvent(self, event):
        self.timer.stop()
        if self.runtime:
            self.runtime.stop()
        self.study_pool.shutdown(wait=False, cancel_futures=True)
        self.core.close()
        event.accept()


def launch(core):
    from PySide6.QtCore import QEvent

    app = QApplication.instance() or QApplication([])
    window = Window(core)
    window.show()
    result = app.exec()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    return result
