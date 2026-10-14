"""Responsive double-click startup. ESI-02 remains an unreleased development build."""

import json
import os
import queue
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import QLockFile, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from esi.icons import icon
from esi02.assets import DownloadProgress
from esi02.config import Settings
from esi02.desktop_preferences import write_json
from esi02.gui import Window
from esi02.services import Services
from esi02.session import Session, inspect_database
from esi02.setup_assets import models_missing, prepare_assets, prepare_binaries


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)
    save_path: str | None = Field(default=None, max_length=4096)
    aivis_executable: str | None = Field(default=None, max_length=4096)


def read_preferences(path):
    path = Path(path)
    if not path.exists():
        return Preferences()
    if path.stat().st_size > 16_000:
        raise ValueError("起動設定のファイルが大きすぎます。")
    return Preferences.model_validate(json.loads(path.read_text(encoding="utf-8")))


def select_save(path, fallback, destination=None):
    """Read-only preflight first; migrate ESI-01 only into an explicitly named new file."""
    from esi02.migration import migrate_esi01, validate_save

    path = Path(path).resolve(strict=True)
    if validate_save(path)["release"] == "ESI-01":
        if not destination:
            raise ValueError("ESI-01は元のセーブを残してコピーします。新しい保存先を選んでください。")
        migrate_esi01(path, destination, fallback)
        path = Path(destination).resolve(strict=True)
    inspect_database(path)
    return path


class Launcher(QWidget):
    def __init__(self, root, services_factory=Services, window_factory=Window):
        super().__init__()
        self.root = Path(root).resolve()
        self.data = self.root / "data/esi02"
        self.data.mkdir(parents=True, exist_ok=True)
        self.preferences_path = self.data / "launcher.json"
        self.preferences_error = ""
        try:
            self.preferences = read_preferences(self.preferences_path)
        except (OSError, ValueError) as exc:
            self.preferences = Preferences()
            self.preferences_error = f"起動設定を読み込めません。元ファイルは保持しました：{exc}"
        self.save_path = Path(self.preferences.save_path or self.data / "session.sqlite3")
        self.fallback = Settings.read(self.root / "config/esi02-windows.toml")
        self.messages = queue.Queue(maxsize=64)
        self.services = services_factory(self.root, self.preferences.model_dump(), self.notify)
        self.window_factory = window_factory
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi02-startup")
        self.future = None
        self.diagnostics_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="esi02-startup-diagnostic"
        )
        self.diagnostics_future = None
        self.window = None
        self.closing = False
        self.finished = False
        self.setWindowTitle("ESI-02を起動 — 開発中")
        self.resize(650, 330)
        layout = QVBoxLayout(self)
        heading = QLabel("ESI-02  |  起動の準備")
        heading.setStyleSheet("font-size: 25px; color: #70dccc; font-weight: bold;")
        layout.addWidget(heading)
        self.save_label = QLabel()
        self.save_label.setWordWrap(True)
        layout.addWidget(self.save_label)
        self.status = QLabel("会話・聞き取り・音声の接続を確認します。")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status, 1)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.retry = QPushButton("起動・再試行")
        self.retry.setIcon(icon("start"))
        self.retry.clicked.connect(lambda: self.start())
        self.prepare = QPushButton("AIデータの準備")
        self.prepare.setToolTip(
            "指定版をダウンロード・照合します。初回は数GBあります。既存データは上書きしません。"
        )
        self.prepare.clicked.connect(lambda: self.start(prepare=True))
        self.choose_engine = QPushButton("AivisSpeechを選ぶ")
        self.choose_engine.clicked.connect(self.choose_aivis)
        self.choose_save_button = QPushButton("セーブを選ぶ")
        self.choose_save_button.setIcon(icon("folder"))
        self.choose_save_button.clicked.connect(self.choose_save)
        for button in (self.retry, self.prepare, self.choose_engine, self.choose_save_button):
            row.addWidget(button)
        layout.addLayout(row)
        bottom = QHBoxLayout()
        self.offline = QPushButton("保存・記憶の画面だけ開く")
        self.offline.clicked.connect(self.open_offline)
        logs = QPushButton("ログを開く")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data / "logs"))))
        aivis_download = QPushButton("AivisSpeechの公式配布")
        aivis_download.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://aivis-project.com/")))
        self.diagnostics_button = QPushButton("診断を保存")
        self.diagnostics_button.clicked.connect(self.save_diagnostics)
        for button in (self.offline, logs, aivis_download, self.diagnostics_button):
            bottom.addWidget(button)
        layout.addLayout(bottom)
        self.diagnostics_hint = QLabel()
        self.diagnostics_hint.setWordWrap(True)
        layout.addWidget(self.diagnostics_hint)
        self.setStyleSheet(
            "QWidget {background: #152332; color: #edf5ff; font-size: 13px;}"
            "QPushButton {background: #29465e; border: 0; border-radius: 6px; padding: 8px;}"
            "QPushButton:disabled {color: #8397a9;}"
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.enabled(True)
        self.update_save_label()
        if self.preferences_error:
            self.status.setText(self.preferences_error)

    def notify(self, message):
        try:
            self.messages.put_nowait(message if isinstance(message, DownloadProgress) else str(message))
        except queue.Full:
            pass  # Telemetry must never block a cancellation or the startup worker.

    def enabled(self, idle):
        for button in (self.retry, self.prepare, self.choose_engine, self.choose_save_button, self.offline):
            button.setEnabled(idle and not self.closing)
        self.progress.setRange(0, 100 if idle else 0)
        self.progress.setValue(0)

    def update_save_label(self):
        self.save_label.setText("開くセーブ：" + str(self.save_path))

    def save_preferences(self):
        if self.preferences_error:
            raise ValueError(self.preferences_error)
        write_json(self.preferences_path, self.preferences.model_dump())

    def settings(self):
        if self.preferences_error:
            raise ValueError(self.preferences_error)
        if self.preferences.save_path and not self.save_path.is_file():
            raise FileNotFoundError("前回のセーブが見つかりません。「セーブを選ぶ」で選び直してください。")
        return inspect_database(self.save_path) if self.save_path.exists() else self.fallback

    def record_error(self, exc):
        logs = self.data / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "startup.log").open("a", encoding="utf-8") as stream:
                stream.write("".join(traceback.format_exception(exc)) + "\n")
        except OSError:
            pass
        self.status.setText(str(exc))
        self.enabled(True)

    def start(self, prepare=False):
        if self.future or self.closing or self.window:
            return
        try:
            settings = self.settings()  # Invalid/missing saves never stop existing services.
            if not prepare and models_missing(self.root):
                raise FileNotFoundError(
                    "初回のAIデータが揃っていません。「AIデータの準備」を押してください。"
                    "\nAivisSpeech本体は公式配布から導入し、ここで選べます。"
                )
            self.status.setText("準備しています。初回の読み込みには時間がかかります…")
            self.enabled(False)
            self.future = self.pool.submit(self.prepare_job, settings, prepare)
        except Exception as exc:
            self.record_error(exc)

    def prepare_job(self, settings, prepare):
        self.services.stop_owned()
        warning = ""
        if prepare:
            prepare_assets(self.root, settings, self.notify, self.services.cancel)
        else:
            try:
                prepare_binaries(self.root, settings, self.notify, self.services.cancel)
            except (OSError, ValueError) as exc:
                if settings.llm_device != "auto" or not self.services._cpu_executable(None):
                    raise
                warning = f"GPUソフトの準備を完了できませんでした。既存のCPU版で起動します：{exc}"
                self.notify(warning)
        self.services.prepare(settings)
        if warning:
            self.services.backend["reason"] = self.services.backend.get("reason", "") + "\n" + warning
        return settings

    def poll(self):
        if self.diagnostics_future and self.diagnostics_future.done():
            completed, self.diagnostics_future = self.diagnostics_future, None
            try:
                self.diagnostics_hint.setText("診断を保存しました：" + str(completed.result()))
            except Exception as exc:
                self.diagnostics_hint.setText(f"診断を保存できません：{exc}")
            self.diagnostics_button.setEnabled(not self.closing)
        if not self.closing:
            while not self.messages.empty():
                message = self.messages.get_nowait()
                self.status.setText(str(message))
                if isinstance(message, DownloadProgress) and message.percent is not None:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(message.percent)
                elif isinstance(message, DownloadProgress) and message.phase == "complete":
                    self.progress.setRange(0, 100)
                    self.progress.setValue(100)
                else:
                    self.progress.setRange(0, 0)
        if self.future and self.future.done():
            completed, self.future = self.future, None
            if self.closing:
                self.shutdown()
                self.close()
                return
            try:
                self.open_main(completed.result())
            except Exception as exc:
                self.record_error(exc)

    def remember_save(self, path):
        self.save_path = Path(path)
        self.preferences.save_path = str(self.save_path.resolve())
        try:
            self.save_preferences()
        except (OSError, ValueError) as exc:
            self.window.ui_error = f"次回の起動用の保存先を記録できません：{exc}"
        self.update_save_label()

    def save_diagnostics(self):
        if self.diagnostics_future or self.closing:
            return
        from esi02.diagnostics import collect_from_paths, save_report

        root, path = self.root, self.save_path
        config = root / "config/esi02-windows.toml" if not path.exists() else None

        def collect():
            report = collect_from_paths(root, path, config)
            return save_report(root, report)

        self.diagnostics_hint.setText("音声機器と接続を確認しています…")
        self.diagnostics_button.setEnabled(False)
        self.diagnostics_future = self.diagnostics_pool.submit(collect)

    def open_main(self, settings, offline=False):
        session = Session(self.save_path, settings)
        try:
            backend = (
                {"device": "unknown", "verified": False, "reason": "会話・音声の接続は未確認です。"}
                if offline
                else self.services.backend
            )
            window = self.window_factory(session, backend, self.services)
            if offline:
                window.connection_ready = False
                window.refresh()
        except Exception:
            session.close()
            raise
        self.window = window
        window.save_changed.connect(self.remember_save)
        self.remember_save(self.save_path)
        window.show()
        self.hide()
        self.timer.stop()

    def open_offline(self):
        if self.future or self.closing or self.window:
            return
        try:
            self.open_main(self.settings(), offline=True)
        except Exception as exc:
            self.record_error(exc)

    def choose_aivis(self):
        path, _ = QFileDialog.getOpenFileName(self, "AivisSpeech.exeを選ぶ", "", "ソフト (*.exe)")
        if path:
            self.preferences.aivis_executable = path
            self.services.preferences = self.preferences.model_dump()
            try:
                self.save_preferences()
                self.start()
            except (OSError, ValueError) as exc:
                self.record_error(exc)

    def choose_save(self):
        from esi02.migration import validate_save

        path, _ = QFileDialog.getOpenFileName(self, "セーブを選ぶ", str(self.data), "セーブ (*.sqlite3 *.db)")
        if not path:
            return
        try:
            destination = None
            if validate_save(path)["release"] == "ESI-01":
                destination, _ = QFileDialog.getSaveFileName(
                    self,
                    "ESI-02へのコピーを保存（元セーブは残ります）",
                    str(Path(path).with_name(Path(path).stem + "-ESI-02.sqlite3")),
                    "セーブ (*.sqlite3)",
                )
                if not destination:
                    return
            selected = select_save(path, self.fallback, destination)
            self.save_path = selected
            self.preferences.save_path = str(selected)
            self.save_preferences()
            self.update_save_label()
            self.start()
        except Exception as exc:
            self.record_error(exc)

    def shutdown(self):
        if not self.finished:
            self.finished = True
            self.timer.stop()
            self.services.close()
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.diagnostics_pool.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        if self.future and not self.future.done():
            self.closing = True
            self.services.cancel.set()
            self.enabled(False)
            self.status.setText("準備を中止しています。進行中の通信が終わるまでお待ちください…")
            event.ignore()
            return
        self.shutdown()
        event.accept()


def main(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    os.chdir(root)  # Relative ASR/model paths are defined against the application directory.
    data = root / "data/esi02"
    data.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    lock = QLockFile(str(data / "launcher.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "ESI-02", "ESI-02は起動済みです。タスクバーのESI画面を開いてください。")
        return 1
    launcher = None
    try:
        launcher = Launcher(root)
        launcher.show()
        QTimer.singleShot(0, launcher.start)
        return app.exec()
    finally:
        if launcher:
            launcher.shutdown()
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
