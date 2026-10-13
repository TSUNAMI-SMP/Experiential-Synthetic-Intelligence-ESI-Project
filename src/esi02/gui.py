"""Development observer for conversation. Final desktop/avatar UI is a later required stage."""

import math

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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.devices import audio_devices, identity, load_preferences, resolve_device, save_preferences
from esi.icons import icon
from esi02.runtime import Runtime

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
    def __init__(self, session, backend=None):
        super().__init__()
        self.session, self.runtime = session, None
        self.backend = backend or {"device": "unknown", "verified": False}
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
        recording = QPushButton("録音ファイルを入力")
        recording.clicked.connect(self.recording)
        for widget in (self.start_button, self.mic_button, self.on_start, save, recording):
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
        self.conversation = QTextEdit()
        self.conversation.setReadOnly(True)
        layout.addWidget(self.conversation, 1)
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
            self.runtime.stop()
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

    def refresh(self):
        view = self.session.view()
        status = view["status"]
        self.phase.setText(PHASES.get(status.get("phase"), "状態を確認中"))
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
        if status.get("error") or self.ui_error:
            self.error.setText(status.get("error") or self.ui_error)
        else:
            self.error.clear()
        self.send_button.setEnabled(bool(self.runtime))
        self.statusBar().showMessage(f"セーブ：{self.session.store.path.name} / 活動 {view['elapsed']:.1f}秒")

    def closeEvent(self, event):
        self.timer.stop()
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
        self.session.close()
        event.accept()


def launch(session, backend=None):
    from PySide6.QtCore import QEvent

    app = QApplication.instance() or QApplication([])
    window = Window(session, backend)
    window.show()
    result = app.exec()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    return result
