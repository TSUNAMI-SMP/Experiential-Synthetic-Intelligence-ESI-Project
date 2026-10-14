import hashlib
import io
import json
import sqlite3
import threading
import time

import pytest

from esi02.config import Settings
from esi02.session import Session

_app = None


@pytest.fixture
def app(monkeypatch):
    global _app
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    return _app


class FakeServices:
    def __init__(self, root, preferences, notify):
        self.cancel = threading.Event()
        self.preferences = preferences
        self.notify = notify
        self.backend = {"device": "unknown", "verified": False}
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def stop_owned(self):
        self.calls.append("stop_owned")

    def prepare(self, settings):
        self.calls.append(settings)
        self.started.set()
        self.notify("test: connecting engines")
        while not self.release.wait(0.01):
            if self.cancel.is_set():
                raise RuntimeError("test: startup cancelled")

    def close(self):
        self.calls.append("close")
        self.cancel.set()


def make_launcher(tmp_path, monkeypatch):
    from esi02 import gui, launcher

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    monkeypatch.setattr(launcher, "models_missing", lambda root: False)
    monkeypatch.setattr(launcher, "prepare_binaries", lambda *args: None)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/esi02-windows.toml").write_text("playback = false\n", encoding="utf-8")
    return launcher.Launcher(tmp_path, services_factory=FakeServices)


def until(app, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("startup did not reach expected state")


def dispose(startup, app):
    from PySide6.QtCore import QEvent

    startup.services.release.set()
    if startup.future:
        startup.future.result(timeout=3)
    if startup.window:
        startup.window.close()
        startup.window.deleteLater()
    startup.close()
    startup.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_startup_is_responsive_opens_main_and_remembers_save_chosen_in_main(app, tmp_path, monkeypatch):
    from esi02.launcher import read_preferences

    startup = make_launcher(tmp_path, monkeypatch)
    startup.show()
    try:
        startup.start()
        assert startup.services.started.wait(1)
        app.processEvents()
        assert startup.future and not startup.retry.isEnabled()
        assert not startup.offline.isEnabled()
        assert not startup.save_path.exists()  # Do not create a save before engines have passed.
        startup.start()
        assert startup.services.calls.count("stop_owned") == 1
        startup.services.release.set()
        until(app, lambda: startup.window is not None)
        assert not startup.isVisible() and startup.window.isVisible()
        assert startup.window.runtime is None
        assert read_preferences(startup.preferences_path).save_path == str(startup.save_path)
        selected = tmp_path / "selected.sqlite3"
        Session(selected, Settings(playback=False)).close()
        startup.window.switch_save(selected)
        until(app, lambda: startup.window.preparation is None)
        assert read_preferences(startup.preferences_path).save_path == str(selected)
    finally:
        dispose(startup, app)


def test_missing_previous_save_does_not_silently_create_new_one_or_stop_services(app, tmp_path, monkeypatch):
    startup = make_launcher(tmp_path, monkeypatch)
    startup.preferences.save_path = str(tmp_path / "missing.sqlite3")
    startup.save_path = tmp_path / "missing.sqlite3"
    try:
        startup.start()
        assert "前回のセーブが見つかりません" in startup.status.text()
        assert not startup.future and not startup.services.calls
        assert not startup.save_path.exists()
    finally:
        dispose(startup, app)


def test_close_during_prepare_waits_for_cancellation_and_never_opens_save(app, tmp_path, monkeypatch):
    startup = make_launcher(tmp_path, monkeypatch)
    startup.show()
    startup.start()
    assert startup.services.started.wait(1)
    startup.close()
    assert startup.closing and startup.isVisible()
    assert "準備を中止しています" in startup.status.text()
    until(app, lambda: startup.finished and not startup.isVisible())
    assert startup.window is None and not startup.save_path.exists()
    assert startup.services.cancel.is_set()
    dispose(startup, app)


def test_engine_failure_preserves_save_and_can_retry(app, tmp_path, monkeypatch):
    startup = make_launcher(tmp_path, monkeypatch)
    startup.services.prepare = lambda settings: (_ for _ in ()).throw(ValueError("test: missing Rion El"))
    startup.show()
    try:
        startup.start()
        until(app, lambda: startup.future is None)
        assert "missing Rion El" in startup.status.text()
        assert startup.retry.isEnabled() and startup.window is None
        assert not startup.save_path.exists()
        assert (startup.data / "logs/startup.log").is_file()
        startup.services.prepare = lambda settings: None
        startup.start()
        until(app, lambda: startup.window is not None)
        assert startup.window.session.settings.playback is False
    finally:
        dispose(startup, app)


def test_offline_memory_view_is_labelled_unverified_and_does_not_start_services(app, tmp_path, monkeypatch):
    startup = make_launcher(tmp_path, monkeypatch)
    try:
        startup.open_offline()
        assert startup.window and not startup.services.calls
        assert startup.window.runtime is None
        assert "未確認" in startup.window.hardware.text()
    finally:
        dispose(startup, app)


def test_startup_diagnostic_runs_independently_of_engine_preparation(app, tmp_path, monkeypatch):
    from esi02 import diagnostics

    started, release = threading.Event(), threading.Event()

    def collect(root, path, config):
        started.set()
        assert release.wait(3)
        return {"test": True}

    monkeypatch.setattr(diagnostics, "collect_from_paths", collect)
    startup = make_launcher(tmp_path, monkeypatch)
    try:
        startup.save_diagnostics()
        assert started.wait(1)
        startup.start()
        assert startup.services.started.wait(1)
        assert startup.future and startup.diagnostics_future
        assert not startup.save_path.exists()
        release.set()
        startup.diagnostics_future.result(timeout=3)
        startup.poll()
        assert "診断を保存しました" in startup.diagnostics_hint.text()
        assert startup.future and not startup.save_path.exists()
    finally:
        release.set()
        dispose(startup, app)


def test_invalid_launcher_preferences_are_retained_and_not_replaced(app, tmp_path, monkeypatch):
    root = tmp_path / "data/esi02"
    root.mkdir(parents=True)
    path = root / "launcher.json"
    original = b'{"save_path": true}'
    path.write_bytes(original)
    startup = make_launcher(tmp_path, monkeypatch)
    try:
        startup.start()
        assert "元ファイルは保持" in startup.status.text()
        assert path.read_bytes() == original
        assert not startup.services.calls
    finally:
        dispose(startup, app)


def test_first_run_model_preparation_is_explicit_and_keeps_ui_responsive(app, tmp_path, monkeypatch):
    from esi02 import launcher

    startup = make_launcher(tmp_path, monkeypatch)
    prepared = []
    monkeypatch.setattr(launcher, "models_missing", lambda root: True)
    monkeypatch.setattr(launcher, "prepare_assets", lambda *args: prepared.append(args[0]))
    try:
        startup.start()
        assert "AIデータの準備" in startup.status.text()
        assert startup.future is None and not prepared
        startup.start(prepare=True)
        assert startup.services.started.wait(1)
        assert prepared == [tmp_path] and not startup.retry.isEnabled()
        startup.services.release.set()
        until(app, lambda: startup.window is not None)
    finally:
        dispose(startup, app)


def test_old_save_requires_new_migration_destination_and_unrelated_database_unchanged(tmp_path):
    from esi.core import Core
    from esi.models import Settings as OldSettings
    from esi02.launcher import select_save

    old = tmp_path / "old.sqlite3"
    Core(old, OldSettings(llm_mode="off")).close()
    original = old.read_bytes()
    with pytest.raises(ValueError, match="コピー"):
        select_save(old, Settings(playback=False))
    destination = tmp_path / "new.sqlite3"
    assert select_save(old, Settings(playback=False), destination) == destination
    assert old.read_bytes() == original
    migrated = Session(destination)
    try:
        assert migrated.settings.release == "ESI-02"
    finally:
        migrated.close()
    unrelated = tmp_path / "unrelated.sqlite3"
    unrelated.write_bytes(b"not an ESI save")
    with pytest.raises(sqlite3.DatabaseError):
        select_save(unrelated, Settings())
    assert unrelated.read_bytes() == b"not an ESI save"


def test_verified_models_download_then_reuse_and_preserve_different_existing_data(tmp_path, monkeypatch):
    from esi02 import setup_assets

    contents = {name: ("test: " + name).encode() for name in setup_assets.ASR_FILES}
    metadata = {
        "sha": setup_assets.ASR_REVISION,
        "siblings": [
            {"rfilename": name, "blobId": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()}
            for name, data in contents.items()
        ],
    }

    def urlopen(url, **kwargs):
        return io.BytesIO(
            json.dumps(metadata).encode() if "/api/" in url else contents[url.rsplit("/", 1)[-1]]
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(setup_assets, "MODELS", ())
    setup_assets.prepare_models(tmp_path, lambda message: None)
    setup_assets.prepare_models(tmp_path, lambda message: None)
    path = tmp_path / "models/faster-whisper-small/config.json"
    path.write_bytes(b"user-data")
    with pytest.raises(ValueError, match="元ファイルは保持"):
        setup_assets.prepare_models(tmp_path, lambda message: None)
    assert path.read_bytes() == b"user-data"


def test_binary_preparation_installs_cpu_fallback_and_cuda_without_calling_it_verified(tmp_path, monkeypatch):
    from esi02 import setup_assets

    installed, notices = [], []

    def install(root, device, notify, cancel=None):
        installed.append(device)
        path = root / ("llama-cuda" if device == "cuda" else "llama-esi02-cpu")
        path.mkdir()
        (path / "llama-server.exe").write_bytes(b"fixture only, not a real executable")

    monkeypatch.setattr(setup_assets, "install_llama", install)
    monkeypatch.setattr(setup_assets, "nvidia_devices", lambda: ([object()], ""))
    setup_assets.prepare_binaries(tmp_path, Settings(), notices.append, windows=True)
    assert installed == ["cpu", "cuda"]
    assert "実計算は起動・聞き取り後" in notices[-1]
    setup_assets.prepare_binaries(tmp_path, Settings(), notices.append, windows=True)
    assert installed == ["cpu", "cuda"]


def test_interrupted_asset_download_removes_partial_and_never_replaces_existing_file(tmp_path, monkeypatch):
    from esi02.assets import download

    cancel = threading.Event()

    class Stream(io.BytesIO):
        def read(self, size=-1):
            block = super().read(size)
            cancel.set()
            return block

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Stream(b"test-data"))
    destination = tmp_path / "model.gguf"
    with pytest.raises(RuntimeError, match="中止"):
        download(
            "https://test.invalid/model", destination, hashlib.sha256(b"test-data").hexdigest(), cancel=cancel
        )
    assert not destination.exists() and not destination.with_suffix(".gguf.partial").exists()
    destination.write_bytes(b"user-model")
    with pytest.raises(ValueError, match="既存のダウンロード"):
        download("https://test.invalid/model", destination, hashlib.sha256(b"test-data").hexdigest())
    assert destination.read_bytes() == b"user-model"


def test_launcher_displays_known_transfer_and_switches_to_verification(app, tmp_path, monkeypatch):
    from esi02.assets import DownloadProgress

    startup = make_launcher(tmp_path, monkeypatch)
    startup.enabled(False)
    startup.notify(DownloadProgress("model.gguf", "download", 500_000, 1_000_000, 250_000))
    startup.poll()
    assert startup.progress.maximum() == 100 and startup.progress.value() == 50
    assert "0.25 MB/秒" in startup.status.text() and "残り約" in startup.status.text()
    startup.notify(DownloadProgress("model.gguf", "verify"))
    startup.poll()
    assert startup.progress.maximum() == 0 and "照合中" in startup.status.text()
    startup.notify(DownloadProgress("model.gguf", "download", 500_000, None, 250_000))
    startup.poll()
    assert startup.progress.maximum() == 0 and "総容量不明" in startup.status.text()
    dispose(startup, app)


def test_owned_gpu_verification_is_kept_for_same_settings_and_context_change_restarts_owned_only(
    tmp_path, monkeypatch
):
    from esi02 import services
    from esi02.gpu import Placement

    class Process:
        code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = 0

        def wait(self, timeout=None):
            return self.code

    model = tmp_path / "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"unit-test-model")
    monkeypatch.setattr(services, "MODEL_SHA", hashlib.sha256(model.read_bytes()).hexdigest())
    monkeypatch.setattr(services, "plan_llama", lambda *args: (Placement("cpu", 0, "fixture CPU"), {}))
    manager = services.Services(tmp_path, {}, executable=tmp_path / "fixture-server")
    settings = Settings(llm_device="cuda")
    manager.language_signature = manager.signature(settings)
    manager.backend = {"source": "owned_process", "device": "cuda", "verified": True}
    process = Process()
    manager.owned.append(process)
    manager.language_process = process
    monkeypatch.setattr(manager, "alive", lambda *args: process.code is None)
    monkeypatch.setattr(manager, "ensure_language", lambda *args: None)
    started = []
    monkeypatch.setattr(
        manager, "_start_llama", lambda settings, *args: started.append(settings.context_tokens)
    )
    try:
        manager.ensure_engine(settings, "llama")
        assert manager.backend["verified"] and not started and process.code is None
        changed = settings.model_copy(update={"context_tokens": 8192, "llm_device": "auto"})
        manager.ensure_engine(changed, "llama")
        assert started == [8192] and process.code == 0
        assert not manager.backend["verified"]  # The terminated GPU process is no longer evidence.
    finally:
        manager.close()
