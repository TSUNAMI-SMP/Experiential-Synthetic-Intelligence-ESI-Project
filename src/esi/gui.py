import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.adapters import test_tone
from esi.avatar import AvatarWindow
from esi.core import Core
from esi.desktop_preferences import read_preferences, write_preferences
from esi.devices import audio_devices, identity, load_preferences, resolve_device, save_preferences
from esi.experiments import compare
from esi.icons import activity_symbol, icon
from esi.models import Observation, Outcome, uid
from esi.presentation import ACTION_LABELS, SOURCE_LABELS, describe
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
    def __init__(self, core: Core, preferences_directory=None):
        super().__init__()
        self.core = core
        self.runtime = None
        self.history = []
        self.study_pool = ThreadPoolExecutor(max_workers=1)
        self.study = None
        self.audio_test_pool = ThreadPoolExecutor(max_workers=1)
        self.audio_test = None
        self.audio_test_stop = threading.Event()
        preferences_directory = Path(preferences_directory or core.store.path.parent)
        self.preferences_path = preferences_directory / "audio-devices.json"
        self.desktop_path = preferences_directory / "desktop.json"
        try:
            self.desktop_preferences = read_preferences(self.desktop_path)
        except (OSError, ValueError):
            self.desktop_preferences = {}
        if not isinstance(self.desktop_preferences.get("avatar"), dict):
            self.desktop_preferences["avatar"] = {}
        self.avatar = AvatarWindow(self.desktop_preferences.setdefault("avatar", {}))
        self.avatar.start_requested.connect(self.toggle)
        self.avatar.microphone_requested.connect(lambda: self.submit("microphone"))
        self.avatar.main_requested.connect(self.show_main)
        self.avatar.hidden_requested.connect(lambda: self.avatar_enabled.setChecked(False))
        self.avatar.preferences_changed.connect(self.save_desktop_preferences)
        try:
            self.preferences = load_preferences(self.preferences_path)
            self.device_error = ""
        except (OSError, ValueError) as exc:
            self.preferences = {"input": None, "output": None, "microphone_on_start": True}
            self.device_error = str(exc)
        self.setWindowTitle("ESI-01 — 体験型合成知能 / 研究観察")
        screen = QApplication.primaryScreen()
        size = screen.availableGeometry() if screen else None
        self.resize(
            min(1280, round(size.width() * 0.92)) if size else 1280,
            min(900, round(size.height() * 0.9)) if size else 900,
        )
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.heading = QLabel("ESI-01  |  体験型合成知能")
        self.heading.setStyleSheet("font-size: 23px; font-weight: bold; color: #63d6c2")
        layout.addWidget(self.heading)
        controls = QHBoxLayout()
        self.start_button = QPushButton("開始")
        self.start_button.setIcon(icon("start"))
        self.start_button.clicked.connect(self.toggle)
        controls.addWidget(self.start_button)
        for label, callback in [
            ("保存", lambda: self.submit("save")),
            ("セーブを開く", self.load_save),
            ("録音ファイル入力", self.recording),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            if label != "録音ファイル入力":
                button.setIcon(icon("save" if label == "保存" else "folder"))
            controls.addWidget(button)
        self.microphone_button = QPushButton("マイクを使う")
        self.microphone_button.setIcon(icon("microphone"))
        self.microphone_button.clicked.connect(lambda: self.submit("microphone"))
        self.microphone_button.setEnabled(False)
        controls.addWidget(self.microphone_button)
        self.microphone_on_start = QCheckBox("開始時にマイクを使う")
        self.microphone_on_start.setChecked(self.preferences["microphone_on_start"])
        controls.addWidget(self.microphone_on_start)
        controls.addStretch()
        layout.addLayout(controls)
        desktop_controls = QHBoxLayout()
        self.save_label = QLabel()
        self.save_label.setWordWrap(True)
        desktop_controls.addWidget(self.save_label, 1)
        self.avatar_enabled = QCheckBox("最前面にアバターを表示")
        self.avatar_enabled.toggled.connect(self.toggle_avatar)
        desktop_controls.addWidget(self.avatar_enabled)
        self.model_button = QPushButton("Live2Dモデルを選ぶ")
        self.model_button.setIcon(icon("avatar"))
        self.model_button.clicked.connect(self.select_avatar_model)
        desktop_controls.addWidget(self.model_button)
        self.still_button = QPushButton("立ち絵に戻す")
        self.still_button.clicked.connect(self.reset_avatar_model)
        desktop_controls.addWidget(self.still_button)
        layout.addLayout(desktop_controls)
        routing = QHBoxLayout()
        routing.addWidget(QLabel("入力マイク"))
        self.input_devices = QComboBox()
        self.input_devices.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.input_devices.setMinimumContentsLength(12)
        routing.addWidget(self.input_devices, 1)
        routing.addWidget(QLabel("出力スピーカー"))
        self.output_devices = QComboBox()
        self.output_devices.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.output_devices.setMinimumContentsLength(12)
        routing.addWidget(self.output_devices, 1)
        self.reload_devices = QPushButton("機器一覧を更新")
        self.reload_devices.clicked.connect(self.refresh_devices)
        routing.addWidget(self.reload_devices)
        self.sound_test = QPushButton("音のテスト")
        self.sound_test.clicked.connect(self.test_output)
        routing.addWidget(self.sound_test)
        layout.addLayout(routing)
        self.routing_hint = QLabel("機器の変更と音のテストは、停止中に行えます。ヘッドホンで始めてください。")
        self.routing_hint.setWordWrap(True)
        layout.addWidget(self.routing_hint)
        tabs = QTabWidget()
        basic, research = QWidget(), QWidget()
        self.basic_scroll, self.research_scroll = QScrollArea(), QScrollArea()
        for scroll, child in [(self.basic_scroll, basic), (self.research_scroll, research)]:
            scroll.setWidgetResizable(True)
            scroll.setWidget(child)
        tabs.addTab(self.basic_scroll, "いまの動作")
        tabs.addTab(self.research_scroll, "詳しい観察")
        layout.addWidget(tabs, 1)
        basic_layout = QVBoxLayout(basic)
        phase_row = QHBoxLayout()
        self.phase_icon = QLabel()
        self.phase_icon.setFixedSize(76, 76)
        phase_row.addWidget(self.phase_icon)
        phase_text = QVBoxLayout()
        self.phase_title = QLabel()
        self.phase_title.setWordWrap(True)
        phase_text.addWidget(self.phase_title)
        self.phase_detail = QLabel()
        self.phase_detail.setWordWrap(True)
        phase_text.addWidget(self.phase_detail)
        phase_row.addLayout(phase_text, 1)
        basic_layout.addLayout(phase_row)
        stages = QHBoxLayout()
        self.stage_cards = []
        for name, label in [
            ("microphone", "① 入力"),
            ("ear", "② 聞き取り"),
            ("think", "③ 検討"),
            ("speak", "④ 発声"),
        ]:
            card = QFrame()
            card.setObjectName("stageCard")
            row = QHBoxLayout(card)
            picture = QLabel()
            picture.setPixmap(icon(name).pixmap(30, 30))
            row.addWidget(picture)
            row.addWidget(QLabel(label), 1)
            stages.addWidget(card, 1)
            self.stage_cards.append(card)
        basic_layout.addLayout(stages)
        self.phase_steps = QLabel("①マイク → ②聞き取り → ③検討 → ④発声")
        self.phase_steps.setWordWrap(True)
        basic_layout.addWidget(self.phase_steps)
        meter_row = QHBoxLayout()
        meter_row.addWidget(QLabel("マイクの入力音量"))
        self.input_meter = QProgressBar()
        self.input_meter.setRange(0, 100)
        meter_row.addWidget(self.input_meter, 1)
        basic_layout.addLayout(meter_row)
        self.input_summary = QLabel()
        self.input_summary.setWordWrap(True)
        basic_layout.addWidget(self.input_summary)
        basic_layout.addWidget(QLabel("最後に聞き取った言葉"))
        self.heard_text = QTextEdit()
        self.heard_text.setReadOnly(True)
        self.heard_text.setMaximumHeight(110)
        basic_layout.addWidget(self.heard_text)
        basic_layout.addWidget(QLabel("最後の発声"))
        self.basic_output = QLabel("まだ発声していません。")
        self.basic_output.setWordWrap(True)
        basic_layout.addWidget(self.basic_output)
        basic_layout.addWidget(QLabel("動作の記録（新しいものが下）"))
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        basic_layout.addWidget(self.activity_log, 1)
        layout = QVBoxLayout(research)
        research_controls = QHBoxLayout()
        for label, callback in [("実験分岐", self.branch), ("標準課題の比較実験", self.experiment)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            research_controls.addWidget(button)
        self.learning_box = QCheckBox("学習を有効")
        self.learning_box.setChecked(core.learning_enabled)
        self.learning_box.toggled.connect(lambda enabled: self.submit("learning", enabled))
        research_controls.addWidget(self.learning_box)
        layout.addLayout(research_controls)
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
            "QMainWindow, QWidget {background: #152332; color: #e7edf5; font-size: 12px;}"
            "QPushButton {padding: 8px 10px; background: #294357; border: 1px solid #39566f; border-radius: 6px;}"
            "QPushButton:hover {background: #355770;} QPushButton:disabled {color: #8194a7; background: #203244;}"
            "QLineEdit, QTextEdit, QTableWidget {background: #101d2b; border: 1px solid #30465b; border-radius: 6px; padding: 5px;}"
            "QComboBox {padding: 5px; background: #203649; border: 1px solid #39566f; border-radius: 5px;}"
            "QHeaderView::section {background: #294357; padding: 5px;}"
            "QTabBar::tab {padding: 8px 16px; background: #203244;} QTabBar::tab:selected {background: #355770;}"
            "QProgressBar {border: 1px solid #39566f; border-radius: 4px; text-align: center; min-height: 16px;}"
            "QProgressBar::chunk {background: #4fae9e; border-radius: 3px;}"
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh_devices()
        self.refresh()
        saved_model = self.desktop_preferences["avatar"].get("model")
        if saved_model:
            try:
                self.avatar.set_model(saved_model)
            except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
                self.statusBar().showMessage(f"Live2Dを読み込めません。立ち絵で開きました：{exc}")

    def save_desktop_preferences(self):
        self.desktop_preferences["save_path"] = str(self.core.store.path.resolve())
        try:
            write_preferences(self.desktop_path, self.desktop_preferences)
        except OSError as exc:
            self.statusBar().showMessage(f"画面設定を保存できません：{exc}")

    def toggle_avatar(self, enabled):
        if enabled:
            self.avatar.show()
            self.avatar.raise_()
        else:
            self.avatar.hide()

    def show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def select_avatar_model(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "書き出し済みLive2Dモデルを選ぶ", "", "Live2D (*.model3.json)"
        )
        if path:
            try:
                self.avatar.set_model(path)
                self.desktop_preferences["avatar"]["model"] = path
                self.save_desktop_preferences()
                self.avatar_enabled.setChecked(True)
            except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
                self.statusBar().showMessage(f"Live2Dを読み込めません：{exc}")

    def reset_avatar_model(self):
        self.avatar.set_model(None)
        self.desktop_preferences["avatar"].pop("model", None)
        self.save_desktop_preferences()

    def load_save(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "以前のセーブを開く", str(self.core.store.path.parent), "ESIのセーブ (*.sqlite3)"
        )
        if path:
            try:
                self.switch_save(path)
            except Exception as exc:
                self.statusBar().showMessage(f"セーブを開けません。現在のセーブは保持しています：{exc}")

    def switch_save(self, path):
        path = Path(path).resolve(strict=True)
        if path == self.core.store.path.resolve():
            self.statusBar().showMessage("このセーブはすでに開いています。")
            return
        if self.audio_test:
            raise RuntimeError("音のテストが終わってからセーブを開いてください。")
        # Validate without writing to the selected file before stopping the current session.
        probe = Core(path, read_only=True)
        probe.close()
        if self.runtime:
            self.toggle()
        replacement = Core(path)
        previous = self.core
        self.core = replacement
        previous.close()
        self.history.clear()
        self.learning_box.blockSignals(True)
        self.learning_box.setChecked(replacement.learning_enabled)
        self.learning_box.blockSignals(False)
        self.transcript.clear()
        self.audio_plot.update_values([])
        self.last_output.setText("音声出力：未実行")
        self.candidate_table.setRowCount(0)
        self.action_ids.clear()
        self.save_desktop_preferences()
        self.refresh()
        self.statusBar().showMessage("セーブを開きました。続けるには「開始」を押してください。")

    def refresh_devices(self):
        if self.runtime:
            return
        previous = {
            "input": self.input_devices.currentData()
            if self.input_devices.count()
            else self.preferences["input"],
            "output": self.output_devices.currentData()
            if self.output_devices.count()
            else self.preferences["output"],
        }
        try:
            available = audio_devices()
        except Exception as exc:
            available = []
            self.device_error = f"音声機器の一覧を取得できません：{exc}"
        else:
            self.device_error = ""
        for key, combo, direction in [
            ("input", self.input_devices, "inputs"),
            ("output", self.output_devices, "outputs"),
        ]:
            combo.clear()
            combo.addItem("Windows / OS の既定の機器", None)
            for device in available:
                if device[direction] > 0:
                    combo.addItem(f"{device['name']}  /  {device['hostapi']}  /  #{device['index']}", device)
            selected = previous[key]
            if selected:
                matches = [
                    i for i in range(1, combo.count()) if identity(combo.itemData(i)) == identity(selected)
                ]
                explicit = [i for i in matches if combo.itemData(i)["index"] == selected.get("index")]
                if len(matches) == 1 or len(explicit) == 1:
                    combo.setCurrentIndex(matches[0] if len(matches) == 1 else explicit[0])
                else:
                    combo.addItem(f"選び直してください：{selected['name']}", identity(selected))
                    combo.setCurrentIndex(combo.count() - 1)
        if self.device_error:
            self.routing_hint.setText(self.device_error)
        elif not available:
            self.routing_hint.setText(
                "音声機器が見つかりません。接続して「機器一覧を更新」を押してください。"
            )
        else:
            self.routing_hint.setText(
                "機器の変更と音のテストは停止中に行えます。音量が動くマイクを選んでください。"
            )

    def selected_devices(self):
        available = audio_devices()
        chosen_input, chosen_output = self.input_devices.currentData(), self.output_devices.currentData()
        input_id = resolve_device(chosen_input, available, "inputs")
        output_id = resolve_device(chosen_output, available, "outputs")
        save_preferences(
            self.preferences_path, chosen_input, chosen_output, self.microphone_on_start.isChecked()
        )
        return input_id, output_id

    def test_output(self):
        if self.runtime or self.audio_test:
            return
        try:
            output_id = resolve_device(self.output_devices.currentData(), audio_devices(), "outputs")
            save_preferences(
                self.preferences_path,
                self.input_devices.currentData(),
                self.output_devices.currentData(),
                self.microphone_on_start.isChecked(),
            )
            self.audio_test_stop.clear()
            self.audio_test = self.audio_test_pool.submit(test_tone, output_id, self.audio_test_stop)
            self.routing_hint.setText("選んだ出力機器で、短いテスト音を再生しています。")
            self.core.store.append(
                "activity",
                {"message": "選んだ出力機器の音のテストを開始しました。", "elapsed": self.core.elapsed},
            )
            self._enable_devices(False)
        except Exception as exc:
            self.routing_hint.setText(f"音のテストを開始できません：{exc}")

    def _enable_devices(self, enabled):
        for widget in (
            self.input_devices,
            self.output_devices,
            self.reload_devices,
            self.sound_test,
            self.microphone_on_start,
        ):
            widget.setEnabled(enabled)
        self.start_button.setEnabled(not bool(self.audio_test))

    def toggle(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
            self.start_button.setText("開始")
            self.start_button.setIcon(icon("start"))
            self._enable_devices(True)
            self.microphone_button.setEnabled(False)
            self.statusBar().showMessage("停止中。機器を選び直してから再開できます。")
        else:
            try:
                input_id, output_id = self.selected_devices()
                self.runtime = Runtime(self.core, input_id, output_id)
                self.runtime.start(microphone=self.microphone_on_start.isChecked())
                self.start_button.setText("停止")
                self.start_button.setIcon(icon("stop"))
                self._enable_devices(False)
                self.microphone_button.setEnabled(True)
                self.statusBar().showMessage("処理中。入力音量と「いまの動作」で進行を確認できます。")
            except Exception as exc:
                if self.runtime:
                    self.runtime.stop()
                    self.runtime = None
                self.routing_hint.setText(str(exc))
        self.refresh()

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
            names = {
                "microphone": "マイクの切替",
                "save": "保存",
                "recording": "録音ファイルの入力",
                "observe": "文字・模擬入力",
                "learning": "学習の切替",
                "branch": "実験の分岐",
                "feedback": "結果の記録",
            }
            self.statusBar().showMessage(f"{names.get(name, name)}を受け付けました。")
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
        self.save_label.setText(f"開いているセーブ：{self.core.store.path.name}")
        self.save_label.setToolTip(str(self.core.store.path.resolve()))
        self.refresh_basic(view)
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
            self.action_table,
            [(a["action_id"][:12], ACTION_LABELS[a["kind"]], a["stage"], a["explored"]) for a in actions],
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

    def refresh_basic(self, view):
        current = describe(view, bool(self.runtime))
        self.phase_icon.setPixmap(icon(activity_symbol(view, current["phase"])).pixmap(68, 68))
        active_stage = {
            "listening": 0,
            "recognizing": 1,
            "interpreting": 2,
            "generating": 2,
            "synthesizing": 3,
            "playing": 3,
        }.get(current["phase"])
        for index, card in enumerate(self.stage_cards):
            card.setStyleSheet(
                "QFrame#stageCard {background: %s; border: 1px solid %s; border-radius: 8px;}"
                % (("#244c51", "#63d6c2") if index == active_stage else ("#1c3043", "#30465b"))
            )
        self.phase_title.setText(current["title"])
        self.phase_title.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {'#ee7f9e' if current['error'] else '#63d6c2'}"
        )
        self.phase_detail.setText(current["detail"])
        audio = view["status"].get("audio_input", {})
        rms = audio.get("rms", 0) if self.runtime and (audio.get("seconds_since_packet") or 0) < 2 else 0
        level = round(max(0, min(100, (20 * math.log10(max(rms, 1e-6)) + 60) / 60 * 100)))
        self.input_meter.setValue(level)
        mic = bool(self.runtime and self.runtime.microphone)
        self.avatar.set_status(view, current, bool(self.runtime), mic, enabled=not bool(self.audio_test))
        self.microphone_button.setText("マイクを止める" if mic else "マイクを使う")
        count = audio.get("recognized_intervals", 0)
        voice = "検出中" if audio.get("speaking") and mic else "待機"
        self.input_summary.setText(
            f"マイク：{audio.get('device_name', '未接続')}  /  音声受信：{audio.get('input_seconds', 0):.1f}秒  /  声の検出：{voice}  /  聞き取り：{count}件"
        )
        labels = {
            "listening": "①マイクで声を検出中",
            "recognizing": "②聞き取り中",
            "interpreting": "③内容を検討中",
            "generating": "③発声文を準備中",
            "synthesizing": "④声を作成中",
            "playing": "④音声を再生中",
        }
        self.phase_steps.setText(
            "①マイク → ②聞き取り → ③検討 → ④発声\n"
            + labels.get(current["phase"], "マイクON" if mic else "マイクOFF")
        )
        event = view.get("latest_input")
        self.heard_text.setPlainText(
            f"{SOURCE_LABELS.get(event['source'], event['source'])}：{event['text'] or '聞き取った文字は空でした。'}"
            if event
            else "まだ聞き取った言葉はありません。話すと入力音量が動くか確認してください。"
        )
        spoken = view.get("latest_output")
        if spoken:
            played = spoken["metadata"].get("played", False)
            mode = "スピーカーで再生済み" if played else "音声ファイルを保存（未再生・中断を含む）"
            self.basic_output.setText(f"{spoken['text']}\n{mode}")
        else:
            self.basic_output.setText("まだ発声していません。")
        records = self.core.store.recent_records("activity", limit=10)
        self.activity_log.setPlainText(
            "\n".join(f"{r['payload']['elapsed']:.1f}秒  {r['payload']['message']}" for r in records)
            or "開始すると、実際に進んだ処理をここに表示します。"
        )
        if self.audio_test and self.audio_test.done():
            try:
                played = self.audio_test.result()
                self.routing_hint.setText(
                    "テスト音を再生しました。聞こえなければ別の出力機器を選んでください。"
                    if played
                    else "音のテストを中断しました。"
                )
            except Exception as exc:
                self.routing_hint.setText(f"テスト音の再生に失敗しました：{exc}")
            self.audio_test = None
            self.core.store.append(
                "activity", {"message": self.routing_hint.text(), "elapsed": self.core.elapsed}
            )
            self._enable_devices(True)

    def closeEvent(self, event):
        self.timer.stop()
        self.avatar.shutdown()
        if self.runtime:
            self.runtime.stop()
        self.study_pool.shutdown(wait=False, cancel_futures=True)
        self.audio_test_stop.set()
        self.audio_test_pool.shutdown(wait=False, cancel_futures=True)
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
