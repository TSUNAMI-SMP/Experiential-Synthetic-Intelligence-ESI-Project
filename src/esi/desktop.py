"""Double-click startup with a responsive GUI and ownership of spawned llama only."""

import hashlib
import os
import queue
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

from esi.adapters import TTS
from esi.core import Core
from esi.desktop_preferences import read_preferences, write_preferences
from esi.models import Settings


def saved_settings(path, fallback):
    if not path.exists():
        return fallback
    core = Core(path, read_only=True)
    try:
        return core.settings
    finally:
        core.close()


class Services:
    def __init__(self, root, preferences, notify=lambda text: None, timeout=120):
        self.root = Path(root)
        self.preferences = preferences
        self.notify = notify
        self.timeout = timeout
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.owned = []

    def alive(self, settings, kind):
        url = settings.llm_url if kind == "llama" else settings.aivis_url
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=2) as client:
                response = client.get("/health" if kind == "llama" else "/version")
                response.raise_for_status()
                value = response.json()
                return value.get("status") == "ok" if kind == "llama" else isinstance(value, str)
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def aivis_executable(self):
        choices = [self.preferences.get("aivis_executable")]
        for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(name)
            if base:
                choices.extend(
                    [
                        str(Path(base) / "Programs/AivisSpeech/AivisSpeech.exe"),
                        str(Path(base) / "AivisSpeech/AivisSpeech.exe"),
                    ]
                )
        return next((Path(p) for p in choices if p and Path(p).is_file()), None)

    def start_process(self, command, kind):
        logs = self.root / "data/logs"
        logs.mkdir(parents=True, exist_ok=True)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with self.lock:
            if self.cancel.is_set():
                raise RuntimeError("起動を中止しました。")
            with (logs / f"{kind}.log").open("ab") as stream:
                process = subprocess.Popen(
                    command, cwd=self.root, stdout=stream, stderr=subprocess.STDOUT, **options
                )
            if kind == "llama":
                self.owned.append(process)
        return process

    def ensure_engine(self, settings, kind):
        label = "言語ソフト" if kind == "llama" else "AivisSpeech"
        self.notify(f"{label}の接続を確認しています…")
        if self.alive(settings, kind):
            self.notify(f"{label}は起動済みです。")
            return
        url = urlparse(settings.llm_url if kind == "llama" else settings.aivis_url)
        expected_port = 8080 if kind == "llama" else 10101
        if url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.port != expected_port:
            raise RuntimeError(f"保存設定の{label}につながりません：{url.geturl()}")
        if kind == "llama":
            executable = self.root / "llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")
            model = self.root / "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf"
            if not executable.is_file() or not model.is_file():
                raise FileNotFoundError(
                    "言語ソフトかAIデータが見つかりません。導入手順のダウンロードを完了してください。"
                )
            command = [
                str(executable),
                "--model",
                str(model),
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
                "--ctx-size",
                "4096",
                "--parallel",
                "1",
                "--threads",
                str(settings.cpu_threads),
                "--gpu-layers",
                "0",
                "--reasoning",
                "off",
                "--jinja",
            ]
        else:
            executable = self.aivis_executable()
            if not executable:
                raise FileNotFoundError(
                    "AivisSpeechを見つけられません。「AivisSpeechを選ぶ」で、インストール済みのAivisSpeech.exeを選んでください。"
                )
            command = [str(executable)]
        self.notify(f"{label}を起動しています。AIデータの読み込みを待っています…")
        process = self.start_process(command, kind)
        deadline = time.monotonic() + self.timeout
        while not self.cancel.is_set() and time.monotonic() < deadline:
            if self.alive(settings, kind):
                return
            code = process.poll()
            if code is not None and (kind == "llama" or code != 0):
                raise RuntimeError(
                    f"{label}が終了しました（{code}）。「ログを開く」で記録を確認してください。"
                )
            self.cancel.wait(0.25)
        raise RuntimeError(
            "起動を中止しました。"
            if self.cancel.is_set()
            else f"{label}につながりません。読み込みが終わったら「再試行」を押してください。"
        )

    def ensure_voice(self, settings):
        self.notify("凛音エルの音声モデルを確認しています…")
        with httpx.Client(base_url=settings.aivis_url, trust_env=False, timeout=30) as client:
            version = client.get("/version")
            version.raise_for_status()
            reported_version = version.json()
            if not isinstance(reported_version, str) or not reported_version.strip():
                raise ValueError("AivisSpeech Engineの応答を確認できません。")
            self.notify(f"AivisSpeech Engine {reported_version} に接続しました。凛音エルを確認します…")
            model = client.get(f"/aivm_models/{settings.tts_model_uuid}")
            if model.status_code == 404:
                path = self.root / "models/aivis" / f"{settings.tts_model_uuid}.aivmx"
                if not path.is_file():
                    raise FileNotFoundError(
                        "凛音エルのデータが見つかりません。AIデータのダウンロードを完了してください。"
                    )
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != settings.tts_model_sha256:
                        raise ValueError(
                            "凛音エルのデータが指定版と一致しません。元のファイルを保持しました。"
                        )
                self.notify("ダウンロード済みの凛音エルをAivisSpeechに導入しています…")
                with path.open("rb") as stream:
                    result = client.post("/aivm_models/install", files={"file": (path.name, stream)})
                result.raise_for_status()
            else:
                model.raise_for_status()
            TTS(settings).resolve_voice(client)

    def ensure_language(self, settings):
        with httpx.Client(base_url=settings.llm_url, trust_env=False, timeout=3) as client:
            response = client.get("/v1/models")
            response.raise_for_status()
            models = response.json().get("data", [])
            if not any(settings.llm_model.lower() in str(model.get("id", "")).lower() for model in models):
                raise ValueError(
                    "起動済みの言語ソフトは保存設定のAIモデルと一致しません。対応する言語ソフトを起動してください。"
                )

    def prepare(self, settings):
        if not (self.root / settings.asr_model_path / "model.bin").is_file():
            raise FileNotFoundError(
                "聞き取り用のAIデータが見つかりません。AIデータのダウンロードを完了してください。"
            )
        if settings.llm_mode != "off":
            self.ensure_engine(settings, "llama")
            self.ensure_language(settings)
        self.ensure_engine(settings, "aivis")
        self.ensure_voice(settings)
        if self.cancel.is_set():
            raise RuntimeError("起動を中止しました。")

    def stop_owned(self):
        with self.lock:
            processes, self.owned = self.owned, []
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def close(self):
        self.cancel.set()
        self.stop_owned()


def main():
    from PySide6.QtCore import QLockFile, QTimer, QUrl
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

    from esi.gui import Window

    root = Path.cwd()
    data = root / "data"
    data.mkdir(exist_ok=True)
    app = QApplication.instance() or QApplication([])
    lock = QLockFile(str(data / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(
            None, "ESI-01", "ESI-01はすでに起動しています。タスクバーのESI画面を開いてください。"
        )
        return 1
    preferences_path = data / "launcher.json"
    try:
        preferences = read_preferences(preferences_path)
        desktop = read_preferences(data / "desktop.json")
    except (OSError, ValueError):
        preferences, desktop = {}, {}
    save = Path(desktop.get("save_path") or data / "esi-01.sqlite3")
    fallback = Settings.read(root / "config/windows.toml")
    messages = queue.SimpleQueue()
    services = Services(root, preferences, messages.put)
    pool = ThreadPoolExecutor(max_workers=1)
    future, window = None, None
    startup = QWidget()
    startup.setWindowTitle("ESI-01を起動")
    startup.resize(620, 350)
    layout = QVBoxLayout(startup)
    heading = QLabel("ESI-01  |  凛音エル")
    heading.setStyleSheet("font-size: 25px; font-weight: bold; color: #70dccc")
    layout.addWidget(heading)
    status = QLabel("起動の準備をしています…")
    status.setWordWrap(True)
    layout.addWidget(status, 1)
    progress = QProgressBar()
    progress.setRange(0, 0)
    layout.addWidget(progress)
    buttons = QHBoxLayout()
    retry = QPushButton("再試行")
    choose = QPushButton("AivisSpeechを選ぶ")
    choose_save = QPushButton("セーブを選ぶ")
    offline = QPushButton("画面だけ開く")
    logs = QPushButton("ログを開く")
    for button in (retry, choose, choose_save, offline, logs):
        buttons.addWidget(button)
    layout.addLayout(buttons)
    startup.setStyleSheet(
        "QWidget {background: #152332; color: #edf5ff; font-size: 13px;}"
        "QPushButton {background: #29465e; border: 0; border-radius: 6px; padding: 8px;}"
        "QPushButton:disabled {color: #8397a9;}"
    )

    def enabled(value):
        for button in (retry, choose, choose_save, offline):
            button.setEnabled(value)
        progress.setRange(0, 100 if value else 0)

    def record_error(exc):
        (data / "logs").mkdir(exist_ok=True)
        with (data / "logs/desktop.log").open("a", encoding="utf-8") as stream:
            stream.write("".join(traceback.format_exception(exc)) + "\n")
        status.setText(str(exc))
        enabled(True)

    def start():
        nonlocal future
        enabled(False)
        try:
            if desktop.get("save_path") and not save.is_file():
                raise FileNotFoundError(
                    "前回のセーブが見つかりません。「セーブを選ぶ」でファイルを指定してください。"
                )
            settings = saved_settings(save, fallback)
            services.stop_owned()
            future = pool.submit(services.prepare, settings)
        except Exception as exc:
            record_error(exc)

    def open_main():
        nonlocal window
        try:
            if desktop.get("save_path") and not save.is_file():
                raise FileNotFoundError(
                    "前回のセーブが見つかりません。「セーブを選ぶ」でファイルを指定してください。"
                )
            settings = saved_settings(save, fallback)
            core = Core(save, settings)
            try:
                window = Window(core, preferences_directory=data)
            except Exception:
                core.close()
                raise
            window.show()
            window.save_desktop_preferences()
            startup.hide()
            timer.stop()
        except Exception as exc:
            record_error(exc)

    def poll():
        nonlocal future
        while not messages.empty():
            status.setText(messages.get_nowait())
        if future and future.done():
            completed, future = future, None
            try:
                completed.result()
                open_main()
            except Exception as exc:
                record_error(exc)

    def choose_aivis():
        path, _ = QFileDialog.getOpenFileName(startup, "AivisSpeech.exeを選ぶ", "", "ソフト (*.exe)")
        if path:
            preferences["aivis_executable"] = path
            write_preferences(preferences_path, preferences)
            start()

    def choose_database():
        nonlocal save
        path, _ = QFileDialog.getOpenFileName(startup, "ESIのセーブを選ぶ", str(data), "セーブ (*.sqlite3)")
        if path:
            save = Path(path)
            desktop["save_path"] = path
            start()

    retry.clicked.connect(start)
    choose.clicked.connect(choose_aivis)
    choose_save.clicked.connect(choose_database)
    offline.clicked.connect(open_main)
    logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data / "logs"))))
    timer = QTimer(startup)
    timer.timeout.connect(poll)
    timer.start(200)
    startup.show()
    QTimer.singleShot(0, start)
    try:
        return app.exec()
    finally:
        services.close()
        pool.shutdown(wait=True, cancel_futures=True)
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
