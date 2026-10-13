import json
import sqlite3

import httpx
import pytest

from esi.core import Core
from esi.desktop import Services, saved_settings
from esi.models import Observation, Settings

_app = None


@pytest.fixture
def app(monkeypatch):
    global _app
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    return _app


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.avatar.deleteLater()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_topmost_controls_work_while_main_minimized_and_do_not_change_learning(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from esi import gui

    class FakeRuntime:
        def __init__(self, core, *devices):
            self.microphone = None

        def start(self, microphone=False):
            self.microphone = object() if microphone else None

        def send(self, command, data=None):
            assert command == "microphone"
            self.microphone = None if self.microphone else object()
            return True

        def stop(self):
            self.microphone = None

    monkeypatch.setattr(gui, "Runtime", FakeRuntime)
    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    core = Core(tmp_path / "live.sqlite3", Settings(llm_mode="off"))
    fingerprint = core.settings.fingerprint
    window = gui.Window(core)
    try:
        window.show()
        window.avatar_enabled.setChecked(True)
        window.showMinimized()
        app.processEvents()
        assert window.isMinimized()
        assert window.avatar.parent() is None and window.avatar.isVisible()
        assert window.avatar.windowFlags() & Qt.WindowStaysOnTopHint
        window.avatar.start_button.click()
        assert window.runtime and window.runtime.microphone
        window.avatar.microphone_button.click()
        window.refresh()
        assert window.runtime.microphone is None
        assert window.avatar.microphone_button.text() == "マイクOFF"
        window.avatar.scale(40)
        prefs = json.loads((tmp_path / "desktop.json").read_text())
        assert prefs["avatar"]["width"] == window.avatar.width()
        window.avatar.main_requested.emit()
        app.processEvents()
        assert not window.isMinimized()
        window.avatar.start_button.click()
        assert window.runtime is None
        assert not window.avatar.microphone_button.isEnabled()
        assert core.settings.fingerprint == fingerprint
        assert not core.store.records("observation") and not core.store.records("outcome")
        window.avatar.hide_avatar()
        assert not window.avatar_enabled.isChecked() and not window.avatar.isVisible()
    finally:
        dispose(window, app)


def test_open_save_stops_and_checkpoints_old_session_restores_stored_settings(app, tmp_path, monkeypatch):
    from esi.gui import Window

    old = Core(tmp_path / "old.sqlite3", Settings(seed=1, llm_mode="off"))
    old.observe(Observation(text="previous experience"))
    selected = tmp_path / "selected.sqlite3"
    saved = Core(selected, Settings(seed=7, llm_mode="off", learning=False))
    saved.observe(Observation(text="saved experience"))
    saved.tick(3)
    agent, fingerprint = saved.agent_id, saved.settings.fingerprint
    saved.close()
    window = Window(old)

    class Running:
        microphone = None

        def stop(self):
            old.checkpoint()

    window.runtime = Running()
    try:
        window.switch_save(selected)
        assert window.runtime is None
        assert window.core.agent_id == agent
        assert window.core.settings.fingerprint == fingerprint
        assert window.core.elapsed == 3
        assert not window.learning_box.isChecked()
        assert "saved experience" in window.heard_text.toPlainText()
        assert "selected.sqlite3" in window.save_label.text()
        with pytest.raises(sqlite3.ProgrammingError):
            old.store.meta("settings")
        reopened = Core(tmp_path / "old.sqlite3", read_only=True)
        try:
            assert any(e.text == "previous experience" for e in reopened.events.values())
        finally:
            reopened.close()
        corrupt = tmp_path / "bad.sqlite3"
        corrupt.write_bytes(b"not a database")
        with pytest.raises(sqlite3.DatabaseError):
            window.switch_save(corrupt)
        assert window.core.agent_id == agent and corrupt.read_bytes() == b"not a database"
        assert json.loads((tmp_path / "desktop.json").read_text())["save_path"] == str(selected)
    finally:
        dispose(window, app)


def test_invalid_save_cannot_stop_or_replace_current_session(app, tmp_path):
    from esi.gui import Window

    core = Core(tmp_path / "current.sqlite3", Settings(llm_mode="off"))
    window = Window(core)

    class Running:
        microphone = None

        def stop(self):
            pytest.fail("invalid save stopped current runtime")

    running = Running()
    window.runtime = running
    bad = tmp_path / "unrelated.sqlite3"
    db = sqlite3.connect(bad)
    db.execute("CREATE TABLE unrelated (id INTEGER)")
    db.close()
    before = bad.read_bytes()
    try:
        with pytest.raises(sqlite3.OperationalError):
            window.switch_save(bad)
        assert window.runtime is running and window.core is core
        assert bad.read_bytes() == before
    finally:
        window.runtime = None
        dispose(window, app)


def test_missing_exported_assets_rejected_before_replacing_avatar(app, tmp_path):
    from PySide6.QtCore import QRect

    from esi.avatar import AvatarWindow, validate_model, visible_geometry

    avatar = AvatarWindow({})
    original = avatar.canvas
    model = tmp_path / "el.model3.json"
    model.write_text(
        json.dumps({"Version": 3, "FileReferences": {"Moc": "missing.moc3", "Textures": ["missing.png"]}})
    )
    try:
        with pytest.raises(FileNotFoundError):
            avatar.set_model(model)
        assert avatar.canvas is original
        outside = tmp_path / "nested"
        outside.mkdir()
        model = outside / "el.model3.json"
        (tmp_path / "real.moc3").write_bytes(b"MOC3" + b"0" * 100)
        model.write_text(
            json.dumps({"Version": 3, "FileReferences": {"Moc": "../real.moc3", "Textures": ["missing.png"]}})
        )
        with pytest.raises(ValueError, match="フォルダーの外"):
            validate_model(model)
        screen = QRect(0, 0, 900, 650)
        assert screen.contains(visible_geometry([3000, 2000], 700, [screen]))
    finally:
        avatar.shutdown()
        avatar.deleteLater()


@pytest.mark.parametrize("engine_version", ["1.1.0", "1.2.0"])
def test_launcher_reuses_healthy_engines_without_starting_or_killing_them(tmp_path, monkeypatch, engine_version):
    settings = Settings()
    original_client = httpx.Client
    calls = []

    def response(request):
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen3-4B-Q4_K_M.gguf"}]})
        if request.url.path == "/version":
            return httpx.Response(200, json=engine_version)
        if request.url.path.startswith("/aivm_models/"):
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "uuid": settings.tts_model_uuid,
                        "name": settings.tts_model_name,
                        "version": settings.tts_model_version,
                        "speakers": [{"uuid": settings.tts_speaker_uuid}],
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "speaker_uuid": settings.tts_speaker_uuid,
                    "name": "凛音エル",
                    "styles": [{"name": "ノーマル", "id": 1388823424}],
                }
            ],
        )

    monkeypatch.setattr(
        "esi.desktop.httpx.Client",
        lambda **kw: original_client(**kw, transport=httpx.MockTransport(response)),
    )
    monkeypatch.setattr(
        "esi.desktop.subprocess.Popen", lambda *a, **k: pytest.fail("restarted a healthy service")
    )
    asr = tmp_path / settings.asr_model_path
    asr.mkdir(parents=True)
    (asr / "model.bin").touch()
    services = Services(tmp_path, {})
    services.prepare(settings)
    services.close()
    assert not services.owned
    assert "/speakers" in calls


def test_launcher_starts_correct_llama_and_only_stops_its_own_process(tmp_path, monkeypatch):
    import esi.desktop as desktop

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            assert self.stopped

    process = Process()
    commands = []

    def popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(desktop.subprocess, "Popen", popen)
    executable = tmp_path / "llama" / ("llama-server.exe" if desktop.os.name == "nt" else "llama-server")
    executable.parent.mkdir()
    executable.touch()
    model = tmp_path / "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.touch()
    services = Services(tmp_path, {})
    monkeypatch.setattr(services, "alive", lambda *a: bool(commands))
    services.ensure_engine(Settings(), "llama")
    assert commands[0][0] == str(executable)
    assert commands[0][commands[0].index("--model") + 1] == str(model)
    assert commands[0][-3:] == ["--reasoning", "off", "--jinja"]
    services.close()
    assert process.stopped
    with pytest.raises(RuntimeError, match="中止"):
        services.start_process([str(executable)], "llama")
    assert len(commands) == 1


def test_opening_saved_settings_preserves_config_and_does_not_create_missing_save(tmp_path):
    path = tmp_path / "saved.sqlite3"
    expected = Settings(seed=42, llm_mode="off")
    core = Core(path, expected)
    core.close()
    assert saved_settings(path, Settings()).fingerprint == expected.fingerprint
    missing = tmp_path / "missing.sqlite3"
    assert saved_settings(missing, expected) is expected
    assert not missing.exists()


def test_healthy_server_with_other_language_model_is_rejected(tmp_path, monkeypatch):
    original_client = httpx.Client
    monkeypatch.setattr(
        "esi.desktop.httpx.Client",
        lambda **kw: original_client(
            **kw,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"data": [{"id": "OtherModel"}]})
            ),
        ),
    )
    services = Services(tmp_path, {})
    with pytest.raises(ValueError, match="一致しません"):
        services.ensure_language(Settings())


def test_native_probe_failure_preserves_current_avatar(app, tmp_path, monkeypatch):
    from PySide6.QtGui import QImage

    import esi.avatar as avatar_module
    from esi.avatar import AvatarWindow

    (tmp_path / "model.moc3").write_bytes(b"MOC3" + b"0" * 100)
    image = QImage(2, 2, QImage.Format_RGBA8888)
    image.fill(0xFFEEEEEE)
    image.save(str(tmp_path / "texture.png"))
    model = tmp_path / "el.model3.json"
    model.write_text(
        json.dumps({"Version": 3, "FileReferences": {"Moc": "model.moc3", "Textures": ["texture.png"]}})
    )
    monkeypatch.setattr(
        avatar_module.subprocess, "run", lambda *a, **kw: type("Failed", (), {"returncode": -11})()
    )
    avatar = AvatarWindow({})
    before = avatar.canvas
    try:
        with pytest.raises(ValueError, match="読み込めません"):
            avatar.set_model(model)
        assert avatar.canvas is before
    finally:
        avatar.shutdown()
        avatar.deleteLater()
