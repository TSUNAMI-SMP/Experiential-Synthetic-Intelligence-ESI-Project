"""Development observer for conversation. Final desktop/avatar UI is a later required stage."""

import math
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.devices import audio_devices, identity, load_preferences, resolve_device, save_preferences
from esi.icons import icon
from esi02.runtime import Runtime
from esi02.session import Session

PHASES = {
    "stopped": "停止中",
    "waiting": "話しかけてください",
    "queued": "前の処理を止めて返答を準備中",
    "generating": "返答の文章を考えています",
    "synthesizing": "凛音エルの声を作っています",
    "playing": "音声を再生しています",
    "failed": "処理に問題が起きています",
}


class Window(QMainWindow):
    def __init__(self, session, backend=None, services=None):
        super().__init__()
        self.session, self.runtime = session, None
        self.backend = backend or {"device": "unknown", "verified": False}
        self.services = services
        self.ui_error = ""
        self.preferences_path = session.store.path.parent / "audio-devices.json"
        try:
            self.preferences = load_preferences(self.preferences_path)
        except (OSError, ValueError):
            self.preferences = {"input": None, "output": None, "microphone_on_start": True}
        self.setWindowTitle("ESI-02 開発中 — 会話の動作確認")
        self.resize(1000, 760)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        heading = QLabel("ESI-02  |  開発中・未完成")
        heading.setStyleSheet("font-size: 24px; color: #70dccc; font-weight: bold")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.start_button = QPushButton("開始")
        self.start_button.setIcon(icon("start"))
        self.start_button.clicked.connect(self.toggle)
        self.mic_button = QPushButton("マイクOFF")
        self.mic_button.setIcon(icon("microphone"))
        self.mic_button.clicked.connect(lambda: self.send("microphone"))
        self.mic_button.setEnabled(False)
        self.on_start = QCheckBox("開始時にマイクを使う")
        self.on_start.setChecked(self.preferences["microphone_on_start"])
        save = QPushButton("保存")
        save.setIcon(icon("save"))
        save.clicked.connect(self.save)
        open_save = QPushButton("セーブを開く")
        open_save.setIcon(icon("folder"))
        open_save.clicked.connect(self.open_save)
        memories = QPushButton("記憶を見る・直す")
        memories.clicked.connect(self.open_memories)
        recording = QPushButton("録音ファイルを入力")
        recording.clicked.connect(self.recording)
        for widget in (
            self.start_button,
            self.mic_button,
            self.on_start,
            save,
            open_save,
            memories,
            recording,
        ):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)
        routing = QHBoxLayout()
        routing.addWidget(QLabel("入力マイク"))
        self.input_devices = QComboBox()
        routing.addWidget(self.input_devices, 1)
        routing.addWidget(QLabel("出力スピーカー"))
        self.output_devices = QComboBox()
        routing.addWidget(self.output_devices, 1)
        self.reload = QPushButton("機器一覧を更新")
        self.reload.clicked.connect(self.refresh_devices)
        routing.addWidget(self.reload)
        layout.addLayout(routing)
        self.hardware = QLabel()
        self.hardware.setWordWrap(True)
        layout.addWidget(self.hardware)
        self.phase = QLabel("停止中")
        self.phase.setStyleSheet("font-size: 22px; color: #70dccc; font-weight: bold")
        layout.addWidget(self.phase)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        layout.addWidget(self.meter)
        self.capture = QLabel()
        self.capture.setWordWrap(True)
        layout.addWidget(self.capture)
        self.memory_summary = QLabel()
        layout.addWidget(self.memory_summary)
        self.body_action = QLabel()
        layout.addWidget(self.body_action)
        self.request_id = None
        self.request_card = QWidget()
        card = QVBoxLayout(self.request_card)
        self.request_text = QLabel()
        self.request_text.setWordWrap(True)
        card.addWidget(self.request_text)
        buttons = QHBoxLayout()
        self.request_accept = QPushButton()
        self.request_accept.clicked.connect(self.accept_request)
        self.request_later = QPushButton("あとで")
        self.request_later.clicked.connect(lambda: self.respond_request("later"))
        self.request_decline = QPushButton("今回は断る")
        self.request_decline.clicked.connect(lambda: self.respond_request("decline"))
        for button in (self.request_accept, self.request_later, self.request_decline):
            buttons.addWidget(button)
        buttons.addStretch()
        card.addLayout(buttons)
        self.request_card.setStyleSheet("QWidget {background: #264b54; border-radius: 6px;}")
        layout.addWidget(self.request_card)
        self.conversation = QTextEdit()
        self.conversation.setReadOnly(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.conversation, "会話")
        from esi02.world_gui import WorldPanel

        self.world_panel = WorldPanel(self)
        world_scroll = QScrollArea()
        world_scroll.setWidgetResizable(True)
        world_scroll.setWidget(self.world_panel)
        self.tabs.addTab(world_scroll, "仮想の部屋・身体")
        from esi02.motivation_gui import MotivationPanel

        self.motivation_panel = MotivationPanel(self)
        motivation_scroll = QScrollArea()
        motivation_scroll.setWidgetResizable(True)
        motivation_scroll.setWidget(self.motivation_panel)
        self.tabs.addTab(motivation_scroll, "欲求・行動の学習")
        layout.addWidget(self.tabs, 1)
        input_row = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setMaxLength(2000)
        self.text.setPlaceholderText("会話を文字で入力することもできます")
        self.text.returnPressed.connect(self.submit_text)
        input_row.addWidget(self.text, 1)
        self.send_button = QPushButton("話しかける")
        self.send_button.clicked.connect(self.submit_text)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #f9b4bf")
        layout.addWidget(self.error)
        self.setStyleSheet(
            "QWidget {background: #152332; color: #edf5ff; font-size: 13px;}"
            "QPushButton {background: #29465e; border: 0; border-radius: 6px; padding: 8px;}"
            "QPushButton:disabled {color: #8397a9;} QLineEdit, QTextEdit {background: #101d2b; padding: 8px;}"
            "QComboBox {background: #203649; padding: 5px;}"
            "QProgressBar {border: 1px solid #39566f; text-align: center;}"
            "QProgressBar::chunk {background: #4fae9e;}"
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        self.refresh_devices()
        self.refresh()

    def refresh_devices(self):
        if self.runtime:
            return
        try:
            devices = audio_devices()
            for key, combo, direction in [
                ("input", self.input_devices, "inputs"),
                ("output", self.output_devices, "outputs"),
            ]:
                old = combo.currentData() if combo.count() else self.preferences[key]
                combo.clear()
                combo.addItem("OSの既定の機器", None)
                for device in devices:
                    if device[direction]:
                        combo.addItem(f"{device['name']} / {device['hostapi']} / #{device['index']}", device)
                if old:
                    matches = [
                        i for i in range(1, combo.count()) if identity(combo.itemData(i)) == identity(old)
                    ]
                    exact = [i for i in matches if combo.itemData(i)["index"] == old.get("index")]
                    if len(matches) == 1 or len(exact) == 1:
                        combo.setCurrentIndex(matches[0] if len(matches) == 1 else exact[0])
                    else:
                        combo.addItem(f"選び直してください：{old['name']}", identity(old))
                        combo.setCurrentIndex(combo.count() - 1)
        except Exception as exc:
            self.ui_error = f"機器を確認できません：{exc}"
            self.error.setText(self.ui_error)

    def toggle(self):
        if self.runtime:
            try:
                self.runtime.stop()
            except Exception as exc:
                self.ui_error = f"停止処理を確認してください：{exc}"
                self.error.setText(self.ui_error)
                return
            self.runtime = None
        else:
            try:
                self.ui_error = ""
                devices = audio_devices()
                input_id = resolve_device(self.input_devices.currentData(), devices, "inputs")
                output_id = resolve_device(self.output_devices.currentData(), devices, "outputs")
                save_preferences(
                    self.preferences_path,
                    self.input_devices.currentData(),
                    self.output_devices.currentData(),
                    self.on_start.isChecked(),
                )
                self.runtime = Runtime(self.session, input_id, output_id)
                self.runtime.start(microphone=self.on_start.isChecked())
            except Exception as exc:
                if self.runtime:
                    self.runtime.stop()
                    self.runtime = None
                self.ui_error = str(exc)
                self.error.setText(self.ui_error)
        for widget in (self.input_devices, self.output_devices, self.reload, self.on_start):
            widget.setEnabled(self.runtime is None)
        self.mic_button.setEnabled(self.runtime is not None)
        self.start_button.setText("停止" if self.runtime else "開始")
        self.start_button.setIcon(icon("stop" if self.runtime else "start"))
        self.refresh()

    def send(self, name, data=None):
        if not self.runtime:
            self.ui_error = "開始を押してから操作してください。"
            self.error.setText(self.ui_error)
            return False
        if not self.runtime.send(name, data):
            self.ui_error = "処理待ちが満杯です。入力を受け付けられませんでした。"
            self.error.setText(self.ui_error)
            return False
        self.ui_error = ""
        return True

    def submit_text(self):
        text = self.text.text().strip()
        if text and self.send("text", text):
            self.text.clear()

    def save(self):
        if self.runtime:
            self.send("save")
        else:
            self.session.checkpoint()
            self.statusBar().showMessage("保存しました。")

    def recording(self):
        if not self.runtime:
            self.ui_error = "開始を押してから録音を入力してください。"
            self.error.setText(self.ui_error)
            return
        path, _ = QFileDialog.getOpenFileName(self, "録音を入力", "", "音声 (*.wav *.flac *.ogg)")
        if path:
            self.send("recording", path)

    def memory_action(self, operation, data):
        try:
            if self.runtime:
                return self.send("memory", (operation, data))
            self.session.memory_operation(operation, data)
            self.ui_error = ""
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def open_memories(self):
        from esi02.memory_gui import MemoryDialog

        dialog = MemoryDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def world_action(self, operation, data=None):
        try:
            if self.runtime:
                return (
                    self.send("environment", data)
                    if operation == "environment"
                    else self.send("world", (operation, data))
                )
            self.session.world_operation(operation, data)
            self.session.checkpoint()
            self.ui_error = ""
            self.refresh()
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def motivation_action(self, operation, data=None):
        try:
            if self.runtime:
                return self.send("motivation", (operation, data))
            self.session.motivation_operation(operation, data)
            self.session.checkpoint()
            self.ui_error = ""
            self.refresh()
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def respond_request(self, answer):
        if self.request_id:
            return self.motivation_action("response", {"id": self.request_id, "answer": answer})
        return False

    def accept_request(self):
        active = self.session.view()["motivation"]["active_action"]
        if not active or active["id"] != self.request_id:
            return
        kind = active["kind"]
        if kind in {"ask_cool", "ask_warm"}:
            self.world_action("environment", {"temperature": 23.0})
        elif kind == "ask_quiet":
            self.world_action("environment", {"noise_db": 30.0})
        elif kind == "ask_talk":
            self.respond_request("accept")
            self.text.setFocus()

    def refresh_motivation(self, view, status):
        self.motivation_panel.refresh(view)
        active = view["active_action"]
        self.body_action.setText(
            "身体の行動："
            + (active["label"] if active else "様子を見ています")
            + ("（自発的な行動OFF）" if not view["enabled"] else "")
        )
        request = active if active and active["request"] else None
        self.request_id = request["id"] if request else None
        self.request_card.setVisible(bool(request))
        if request:
            self.request_text.setText(
                "ESIからのお願い（対応は任意です）\n"
                + request["request"]
                + (
                    "\n設定の変更を確認しました。身体の変化を観測中です。"
                    if request["response_observed"]
                    else ""
                )
                + ("\n音声の処理：" + status["request_error"] if status.get("request_error") else "")
            )
            self.request_accept.setText(
                {
                    "ask_cool": "仮想の温度を下げる",
                    "ask_warm": "仮想の温度を上げる",
                    "ask_quiet": "仮想の音を小さくする",
                    "ask_talk": "話しかける",
                }[request["kind"]]
            )
            self.request_accept.setEnabled(not request["response_observed"])

    def switch_save(self, path):
        from esi02.migration import validate_save

        path = Path(path)
        result = validate_save(path)
        if result["release"] != "ESI-02":
            raise ValueError("ESI-01は新しい名前のコピーへ移行して開いてください。")
        if path.samefile(self.session.store.path):
            return
        candidate = Session(path)  # validate/restore before stopping the current working session
        try:
            if self.runtime:
                self.toggle()
            if self.services:
                self.services.prepare(candidate.settings)
                self.backend = self.services.backend
        except Exception:
            candidate.close()
            raise
        old, self.session = self.session, candidate
        old.close()
        self.preferences_path = path.parent / "audio-devices.json"
        try:
            self.preferences = load_preferences(self.preferences_path)
        except (OSError, ValueError):
            self.preferences = {"input": None, "output": None, "microphone_on_start": True}
        self.on_start.setChecked(self.preferences["microphone_on_start"])
        self.input_devices.clear()
        self.output_devices.clear()
        self.refresh_devices()
        self.ui_error = ""
        self.refresh()

    def open_save(self):
        from esi02.migration import migrate_esi01, validate_save

        path, _ = QFileDialog.getOpenFileName(
            self, "セーブを開く", str(self.session.store.path.parent), "ESIのセーブ (*.sqlite3 *.db)"
        )
        if not path:
            return
        try:
            result = validate_save(path)
            if result["release"] == "ESI-01":
                destination, _ = QFileDialog.getSaveFileName(
                    self,
                    "ESI-02へのコピーを保存（元のセーブは残ります）",
                    str(Path(path).with_name(Path(path).stem + "-ESI-02.sqlite3")),
                    "ESI-02のセーブ (*.sqlite3)",
                )
                if not destination:
                    return
                migrate_esi01(path, destination, self.session.settings)
                path = destination
            self.switch_save(path)
        except Exception as exc:
            self.ui_error = f"セーブを開けません：{exc}"
            self.error.setText(self.ui_error)

    def refresh(self):
        view = self.session.view()
        status = view["status"]
        self.world_panel.refresh(view["world"])
        self.refresh_motivation(view["motivation"], status)
        self.phase.setText(PHASES.get(status.get("phase"), "状態を確認中"))
        if status.get("phase") == "waiting" and status.get("request_phase") in {"synthesizing", "playing"}:
            self.phase.setText("ESIからのお願いを声にしています")
        backend = self.backend
        mode = (
            "GPU"
            if backend.get("device") == "cuda" and backend.get("verified")
            else "CPU"
            if backend.get("device") == "cpu" and backend.get("verified")
            else "使用機器は未確認"
        )
        asr = status.get("asr_device", {})
        asr_mode = (
            "GPU"
            if asr.get("device") == "cuda" and asr.get("verified_inference")
            else "CPU"
            if asr.get("device") == "cpu"
            else "確認待ち"
        )
        self.hardware.setText(
            f"言語：{mode} / 聞き取り：{asr_mode}\n"
            + (backend.get("reason") or backend.get("fallback_reason") or "")
            + ("\n" + asr["fallback_reason"] if asr.get("fallback_reason") else "")
        )
        mic = bool(self.runtime and self.runtime.microphone)
        self.mic_button.setText("マイクON" if mic else "マイクOFF")
        audio = status.get("audio_input", {})
        rms = audio.get("rms", 0) if mic and (audio.get("seconds_since_packet") or 0) < 2 else 0
        self.meter.setValue(round(max(0, min(100, (20 * math.log10(max(rms, 1e-6)) + 60) / 60 * 100))))
        self.capture.setText(
            f"マイク：{audio.get('device_name', '未接続')} / 聞き取り：{audio.get('recognized_intervals', 0)}件"
            + (" / 音声を言葉に変換中" if audio.get("asr_busy") else "")
        )
        memory = view["memory"]
        self.memory_summary.setText(
            f"記憶 {memory['count']}件 / 整理待ち {memory['pending']}件"
            + (f" / 整理に失敗 {memory['failed']}件（記憶画面で確認）" if memory["failed"] else "")
            + (" / 会話から記憶を整理中" if status.get("memory_phase") == "organizing" else "")
        )
        entries = []
        for turn in view["turns"]:
            entries.append("あなた：" + (turn["input"] or "（聞き取りに失敗）"))
            if turn["reply"]:
                entries.append("ESI-02：" + turn["reply"])
            metrics = turn["metrics"]
            timing = (
                f" / 文章の開始 {metrics['first_token_seconds']:.1f}秒"
                if metrics.get("first_token_seconds") is not None
                else ""
            )
            entries.append(f"［{turn['stage']}{timing}］")
            if turn["chunks"] and not any(c.get("played") for c in turn["chunks"]):
                entries.append("音声ファイルを保存（スピーカー再生は未確認・OFF・中断）")
        content = "\n\n".join(entries)
        if content != self.conversation.toPlainText():
            self.conversation.setPlainText(content)
            self.conversation.verticalScrollBar().setValue(self.conversation.verticalScrollBar().maximum())
        if status.get("error") or status.get("control_error") or self.ui_error:
            self.error.setText(status.get("error") or status.get("control_error") or self.ui_error)
        else:
            self.error.clear()
        self.send_button.setEnabled(bool(self.runtime))
        self.statusBar().showMessage(f"セーブ：{self.session.store.path.name} / 活動 {view['elapsed']:.1f}秒")

    def closeEvent(self, event):
        self.timer.stop()
        if self.runtime:
            try:
                self.runtime.stop()
            except Exception as exc:
                self.ui_error = f"停止を確認できないため、セーブを開いたままにしています：{exc}"
                self.error.setText(self.ui_error)
                self.timer.start(100)
                event.ignore()
                return
            self.runtime = None
        self.session.close()
        event.accept()


def launch(session, backend=None, services=None):
    from PySide6.QtCore import QEvent

    app = QApplication.instance() or QApplication([])
    window = Window(session, backend, services)
    window.show()
    result = app.exec()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    return result
