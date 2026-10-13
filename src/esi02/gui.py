"""ESI-02 development UI and independent desktop controls."""

import math
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
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

from esi.adapters import test_tone
from esi.devices import audio_devices, identity, load_preferences, resolve_device, save_preferences
from esi.icons import icon
from esi02.desktop import DesktopViews
from esi02.preparation import PreparationResult, prepare_save, restore_services
from esi02.presentation import BODY_SYMBOLS, activity
from esi02.runtime import Runtime
from esi02.session import Session


class Window(QMainWindow):
    save_changed = Signal(str)

    def __init__(self, session, backend=None, services=None):
        super().__init__()
        self.session, self.runtime = session, None
        self.backend = backend or {"device": "unknown", "verified": False}
        self.services = services
        self.connection_ready = True
        self.preparation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi02-save-prepare")
        self.preparation = None
        self.preparation_cancel = threading.Event()
        self.preparation_messages = queue.Queue(maxsize=64)
        self.closing = False
        self.audio_test = None
        self.audio_test_stop = threading.Event()
        self.audio_test_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi02-output-test")
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
        self.start_button.clicked.connect(lambda: self.toggle())
        self.mic_button = QPushButton("マイクOFF")
        self.mic_button.setIcon(icon("microphone"))
        self.mic_button.clicked.connect(lambda: self.send("microphone"))
        self.mic_button.setEnabled(False)
        self.on_start = QCheckBox("開始時にマイクを使う")
        self.on_start.setChecked(self.preferences["microphone_on_start"])
        save = QPushButton("保存")
        save.setIcon(icon("save"))
        save.clicked.connect(self.save)
        open_save = self.open_save_button = QPushButton("セーブを開く")
        open_save.setIcon(icon("folder"))
        open_save.clicked.connect(self.open_save)
        memories = QPushButton("記憶を見る・直す")
        memories.setIcon(icon("reprocess"))
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
        desktop_controls = QHBoxLayout()
        self.desktop_mic = QCheckBox("丸型マイクボタンを表示")
        self.desktop_mic.toggled.connect(lambda enabled: self.set_desktop_visible("microphone", enabled))
        self.desktop_avatar = QCheckBox("立ち絵を表示（制作中）")
        self.desktop_avatar.toggled.connect(lambda enabled: self.set_desktop_visible("avatar", enabled))
        reset_mic = QPushButton("マイクの位置を戻す")
        reset_mic.clicked.connect(lambda: self.desktop.reset_position("microphone"))
        reset_avatar = QPushButton("立ち絵の位置を戻す")
        reset_avatar.clicked.connect(lambda: self.desktop.reset_position("avatar"))
        for widget in (self.desktop_mic, self.desktop_avatar, reset_mic, reset_avatar):
            desktop_controls.addWidget(widget)
        desktop_controls.addStretch()
        layout.addLayout(desktop_controls)
        model_controls = QHBoxLayout()
        self.model_label = QLabel()
        self.model_label.setWordWrap(True)
        model_controls.addWidget(self.model_label, 1)
        self.choose_model_button = QPushButton("Live2Dモデルを選ぶ")
        self.choose_model_button.clicked.connect(self.choose_model)
        self.reload_model_button = QPushButton("モデルを再読込み")
        self.reload_model_button.clicked.connect(self.reload_model)
        self.still_button = QPushButton("立ち絵に戻す")
        self.still_button.clicked.connect(lambda: self.set_model(None))
        for button in (self.choose_model_button, self.reload_model_button, self.still_button):
            model_controls.addWidget(button)
        layout.addLayout(model_controls)
        preparation_row = QHBoxLayout()
        self.preparation_label = QLabel()
        self.preparation_label.setWordWrap(True)
        preparation_row.addWidget(self.preparation_label, 1)
        self.cancel_preparation_button = QPushButton("準備を中止")
        self.cancel_preparation_button.clicked.connect(self.cancel_preparation)
        preparation_row.addWidget(self.cancel_preparation_button)
        self.retry_connection_button = QPushButton("接続を確認し直す")
        self.retry_connection_button.clicked.connect(self.retry_connection)
        preparation_row.addWidget(self.retry_connection_button)
        layout.addLayout(preparation_row)
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
        self.sound_test = QPushButton("音のテスト")
        self.sound_test.clicked.connect(self.test_output)
        routing.addWidget(self.sound_test)
        layout.addLayout(routing)
        self.routing_hint = QLabel(
            "機器の変更と音のテストは停止中にできます。音量が動くマイクを選んでください。"
        )
        self.routing_hint.setWordWrap(True)
        layout.addWidget(self.routing_hint)
        self.hardware = QLabel()
        self.hardware.setWordWrap(True)
        layout.addWidget(self.hardware)
        self.phase = QLabel("停止中")
        self.phase.setStyleSheet("font-size: 22px; color: #70dccc; font-weight: bold")
        phase_row = QHBoxLayout()
        self.phase_icon = QLabel()
        self.phase_icon.setFixedSize(32, 32)
        phase_row.addWidget(self.phase_icon)
        phase_row.addWidget(self.phase, 1)
        layout.addLayout(phase_row)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        layout.addWidget(self.meter)
        self.capture = QLabel()
        self.capture.setWordWrap(True)
        layout.addWidget(self.capture)
        self.memory_summary = QLabel()
        layout.addWidget(self.memory_summary)
        self.body_action = QLabel()
        body_row = QHBoxLayout()
        self.body_icon = QLabel()
        self.body_icon.setFixedSize(24, 24)
        body_row.addWidget(self.body_icon)
        body_row.addWidget(self.body_action, 1)
        layout.addLayout(body_row)
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
        self.desktop = DesktopViews(self, session.store.path.parent / "esi02-desktop.json")
        self.sync_desktop_checks()
        self.refresh_devices()
        self.refresh()

    def sync_desktop_checks(self):
        for checkbox, name in ((self.desktop_mic, "microphone"), (self.desktop_avatar, "avatar")):
            checkbox.blockSignals(True)
            checkbox.setChecked(getattr(self.desktop.preferences, name + "_visible"))
            checkbox.blockSignals(False)

    def set_desktop_visible(self, name, enabled):
        self.desktop.set_visible(name, enabled)
        self.sync_desktop_checks()

    def show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def desktop_microphone(self):
        if self.preparation or not self.connection_ready:
            return
        self.reap_runtime()
        if self.runtime:
            self.send("microphone")
        else:
            self.toggle(microphone=True)

    def reap_runtime(self):
        runtime = self.runtime
        thread = getattr(runtime, "thread", None)
        if thread is not None and not thread.is_alive():
            runtime.stop()
            self.runtime = None

    def refresh_devices(self):
        if self.runtime or self.audio_test:
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

    def toggle(self, microphone=None):
        if self.preparation or not self.connection_ready:
            self.ui_error = "接続の準備が終わってから開始してください。"
            self.error.setText(self.ui_error)
            return
        if self.audio_test:
            self.ui_error = "音のテストが終わってから開始してください。"
            self.error.setText(self.ui_error)
            return
        self.reap_runtime()
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
                self.runtime.start(microphone=self.on_start.isChecked() if microphone is None else microphone)
            except Exception as exc:
                if self.runtime:
                    self.runtime.stop()
                    self.runtime = None
                self.ui_error = str(exc)
                self.error.setText(self.ui_error)
        self.refresh()

    def test_output(self):
        if self.runtime or self.audio_test or self.preparation:
            return
        try:
            device = resolve_device(self.output_devices.currentData(), audio_devices(), "outputs")
            self.audio_test_stop.clear()
            self.audio_test = self.audio_test_pool.submit(test_tone, device, self.audio_test_stop)
            self.routing_hint.setText("選んだスピーカーで、短いテスト音を再生しています。")
            self.ui_error = ""
            self.refresh()
        except Exception as exc:
            self.ui_error = f"音のテストを開始できません：{exc}"
            self.error.setText(self.ui_error)

    def send(self, name, data=None):
        if self.preparation:
            self.ui_error = "セーブの準備が終わってから操作してください。"
            return False
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
        if self.preparation:
            return
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
        if self.preparation:
            return False
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
        if self.preparation:
            return False
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
        if self.preparation:
            return False
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
        self.body_icon.setPixmap(
            icon(BODY_SYMBOLS.get(active["kind"], "wait") if active else "wait").pixmap(24, 24)
        )
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

        if self.preparation:
            raise RuntimeError("準備中です。終わるか中止してからセーブを選んでください。")
        if self.audio_test:
            raise RuntimeError("音のテストが終わってからセーブを開いてください。")
        path = Path(path)
        result = validate_save(path)
        if result["release"] != "ESI-02":
            raise ValueError("ESI-01は新しい名前のコピーへ移行して開いてください。")
        if path.samefile(self.session.store.path):
            return
        candidate = Session(path)  # validate/restore before stopping the current working session
        if self.services:
            self.begin_preparation(candidate)
            return
        try:
            if self.runtime:
                self.toggle()
                if self.runtime:
                    raise RuntimeError("動作を停止できないため、セーブを切り替えませんでした。")
            self.finish_save_switch(candidate)
            self.refresh()
        except Exception:
            candidate.store.close()
            raise

    def finish_save_switch(self, candidate):
        path = candidate.store.path
        # Preflight disk errors and allocate replacement views before closing the old save.
        self.session.checkpoint()
        replacement = DesktopViews(self, path.parent / "esi02-desktop.json")
        try:
            self.session.store.close()
        except Exception:
            replacement.shutdown()
            raise
        self.session = candidate
        self.desktop.shutdown()
        self.desktop = replacement
        self.sync_desktop_checks()
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
        self.save_changed.emit(str(path.resolve()))

    def notify_preparation(self, message):
        try:
            self.preparation_messages.put_nowait(str(message))
        except queue.Full:
            pass

    def begin_preparation(self, candidate):
        self.preparation_cancel.clear()
        self.preparation_label.setText("接続の準備を開始します…")
        self.preparation = {
            "candidate": candidate,
            "future": self.preparation_pool.submit(
                prepare_save,
                self.session,
                candidate,
                self.runtime,
                self.services,
                self.preparation_cancel,
                self.notify_preparation,
            ),
        }
        self.refresh()

    def cancel_preparation(self):
        if self.preparation:
            self.preparation_cancel.set()
            if self.services:
                self.services.cancel.set()
            self.preparation_label.setText("中止しています。通信と復旧の完了を待っています…")

    def retry_connection(self):
        if not self.preparation and not self.runtime and not self.audio_test and self.services:
            self.begin_preparation(self.session)

    def poll_preparation(self):
        if not self.preparation:
            return
        while not self.preparation_messages.empty():
            self.preparation_label.setText(self.preparation_messages.get_nowait())
        job = self.preparation
        if not job["future"].done():
            return
        result = job["future"].result()
        if result.stopped:
            self.runtime = None
        if self.services:
            self.backend = dict(self.services.backend)
        if result.ready and self.preparation_cancel.is_set() and not job.get("restoring"):
            # Cancellation can arrive after the worker's last check and before GUI polling.
            job["restore_needed"] = True
            result.ready = False
            result.error = "準備を中止しました。"
        candidate = job["candidate"]
        if result.ready and candidate is not self.session:
            try:
                self.finish_save_switch(candidate)
            except Exception as exc:
                result.ready = False
                result.error = f"セーブを切り替えられません：{exc}"
        if (
            not result.ready
            and not job.get("restoring")
            and result.recovered
            and result.stopped
            and (job.get("restore_needed") or result.error.startswith("セーブを切り替えられません"))
        ):

            def recover():
                error = restore_services(self.services, self.session.settings)
                return PreparationResult(
                    stopped=result.stopped,
                    recovered=not error,
                    error=result.error + ("\n" + error if error else ""),
                )

            job["restoring"] = True
            job["future"] = self.preparation_pool.submit(recover)
            self.preparation_label.setText("元のセーブの接続を戻しています…")
            return
        self.preparation = None
        self.preparation_cancel.clear()
        self.connection_ready = result.ready or result.recovered
        if not result.ready and candidate is not self.session:
            candidate.store.close()  # Do not retry a failed checkpoint during disposal.
        self.ui_error = result.error
        self.preparation_label.setText(
            "接続を確認しました。" if result.ready else "元のセーブを保持しました。"
        )
        if self.closing:
            QTimer.singleShot(0, self.close)

    def set_model(self, path):
        try:
            if not self.desktop.writable:
                raise ValueError(self.desktop.problem)
            self.desktop.avatar.set_model(path)
            self.desktop.preferences.model_path = (
                str(self.desktop.avatar.model_path) if self.desktop.avatar.model_path else None
            )
            self.desktop.flush()
            self.ui_error = ""
        except Exception as exc:
            self.ui_error = f"モデルを読み込めません：{exc}"
        self.refresh()

    def choose_model(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "書き出し済みLive2Dモデルを選ぶ", "", "Cubismモデル (*.model3.json)"
        )
        if path:
            self.set_model(path)

    def reload_model(self):
        path = self.desktop.preferences.model_path
        if path:
            self.set_model(path)

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
        self.poll_preparation()
        if not self.preparation:
            self.reap_runtime()
        if self.audio_test and self.audio_test.done():
            try:
                played = self.audio_test.result()
                self.ui_error = ""
                self.routing_hint.setText(
                    "テスト音を出力しました。聞こえなければ別のスピーカーを選んでください。"
                    if played
                    else "音のテストを中断しました。"
                )
            except Exception as exc:
                self.ui_error = f"テスト音を再生できません：{exc}"
            self.audio_test = None
        view = self.session.view()
        status = view["status"]
        self.world_panel.refresh(view["world"])
        self.refresh_motivation(view["motivation"], status)
        symbol, phase_text = activity(status)
        self.phase.setText(phase_text)
        self.phase_icon.setPixmap(icon(symbol).pixmap(32, 32))
        running = self.runtime is not None
        preparing = self.preparation is not None
        self.cancel_preparation_button.setVisible(preparing)
        self.cancel_preparation_button.setEnabled(preparing and not self.preparation_cancel.is_set())
        self.retry_connection_button.setVisible(bool(self.services) and not self.connection_ready)
        self.retry_connection_button.setEnabled(not preparing and not running)
        self.open_save_button.setEnabled(not preparing and not self.audio_test)
        for widget in (self.input_devices, self.output_devices, self.reload, self.on_start, self.sound_test):
            widget.setEnabled(not running and not self.audio_test and not preparing)
        self.start_button.setEnabled(not self.audio_test and not preparing and self.connection_ready)
        self.mic_button.setEnabled(running)
        self.start_button.setText("停止" if running else "開始")
        self.start_button.setIcon(icon("stop" if running else "start"))
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
        capture = getattr(self.runtime, "microphone", None)
        stop_event = getattr(capture, "stop_event", None)
        mic = bool(capture and not (stop_event and stop_event.is_set()))
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
            stage = {
                "queued": "返答の準備中",
                "generating": "文章を考えています",
                "synthesizing": "声を作っています",
                "playing": "発声中",
                "complete": "返答を完了",
                "cancelled": "中断しました",
                "failed": "処理に問題がありました",
            }.get(turn["stage"], turn["stage"])
            entries.append(f"［{stage}{timing}］")
            if turn["chunks"] and not any(c.get("played") for c in turn["chunks"]):
                entries.append("音声ファイルを保存（スピーカー再生は未確認・OFF・中断）")
        content = "\n\n".join(entries)
        if content != self.conversation.toPlainText():
            self.conversation.setPlainText(content)
            self.conversation.verticalScrollBar().setValue(self.conversation.verticalScrollBar().maximum())
        model = self.desktop.avatar
        self.model_label.setText(
            "Live2D：" + str(model.model_path)
            if model.model_path
            else "静止画の下書き。凛音エルの完成Live2Dは制作中です。"
        )
        self.reload_model_button.setEnabled(bool(self.desktop.preferences.model_path) and not preparing)
        self.choose_model_button.setEnabled(not preparing)
        self.still_button.setEnabled(not preparing)
        problem = (
            status.get("error")
            or status.get("control_error")
            or self.ui_error
            or self.desktop.problem
            or model.problem
        )
        if problem:
            self.error.setText(problem)
        else:
            self.error.clear()
        self.desktop.refresh(running, mic, symbol, phase_text, problem)
        self.desktop.microphone.start_action.setEnabled(not preparing and self.connection_ready)
        self.desktop.microphone.mic_action.setEnabled(not preparing and self.connection_ready)
        self.desktop.microphone.setEnabled(not preparing)
        self.send_button.setEnabled(running)
        self.statusBar().showMessage(f"セーブ：{self.session.store.path.name} / 活動 {view['elapsed']:.1f}秒")

    def closeEvent(self, event):
        if self.preparation:
            self.closing = True
            self.cancel_preparation()
            event.ignore()
            return
        self.timer.stop()
        self.audio_test_stop.set()
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
        try:
            self.session.checkpoint()
        except Exception as exc:
            self.ui_error = f"保存できないため、セーブを開いたままにしています：{exc}"
            self.error.setText(self.ui_error)
            self.closing = False
            self.timer.start(100)
            event.ignore()
            return
        self.desktop.shutdown()
        self.audio_test_pool.shutdown(wait=False, cancel_futures=True)
        self.preparation_pool.shutdown(wait=False, cancel_futures=True)
        self.session.store.close()
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
