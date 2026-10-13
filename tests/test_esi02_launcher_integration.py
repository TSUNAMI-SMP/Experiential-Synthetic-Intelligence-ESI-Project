import os
import time
from pathlib import Path

import httpx
import pytest

from esi02.config import Settings
from esi02.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual Qwen/Aivis services required"
    ),
]


def test_real_gui_startup_qwen_reply_rion_wave_and_remembered_save(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from esi02 import gui
    from esi02.launcher import Launcher, read_preferences

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    root = tmp_path / "application"
    (root / "config").mkdir(parents=True)
    (root / "models").symlink_to("/workspace/esi-environment/models", target_is_directory=True)
    (root / "config/esi02-windows.toml").write_text(
        'playback = false\nreply_tokens = 64\nasr_device = "cpu"\nllm_device = "cpu"\n'
        'asr_model_path = "/workspace/esi-environment/models/faster-whisper-small"\n',
        encoding="utf-8",
    )
    startup = Launcher(root)
    startup.show()
    startup.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and startup.window is None:
            app.processEvents()
            if startup.future is None and startup.window is None:
                pytest.fail(startup.status.text())
            time.sleep(0.01)
        assert startup.window is not None, startup.status.text()
        window = startup.window
        assert window.backend["source"] == "external_process" and not window.backend["verified"]
        window.on_start.setChecked(False)
        window.toggle()
        window.text.setText("2足す3は？ひとことで教えて。")
        window.submit_text()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            app.processEvents()
            turns = window.session.view()["turns"]
            if turns and turns[-1]["stage"] in {"complete", "failed"}:
                break
            time.sleep(0.02)
        else:
            pytest.fail("CPU dialogue did not finish within 120 seconds")
        turn = turns[-1]
        assert turn["stage"] == "complete", turn["error"]
        assert "5" in turn["reply"] and turn["chunks"]
        assert all(
            c["audio"]["model_name"] == "凛音エル" and Path(c["audio"]["path"]).is_file() and not c["played"]
            for c in turn["chunks"]
        )
        path = startup.save_path
        assert read_preferences(startup.preferences_path).save_path == str(path)
    finally:
        if startup.window:
            startup.window.close()
            startup.window.deleteLater()
        startup.close()
        startup.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
    restored = Session(path)
    try:
        assert restored.view()["turns"][-1]["reply"] == turn["reply"]
    finally:
        restored.close()


def test_real_owned_cpu_start_retains_verification_on_same_settings_and_restarts_for_changed_context(
    tmp_path,
):
    from esi02.services import Services

    (tmp_path / "models").symlink_to("/workspace/esi-environment/models", target_is_directory=True)
    settings = Settings(llm_url="http://127.0.0.1:8082", llm_device="cpu", context_tokens=4096)
    manager = Services(tmp_path, {}, executable="/workspace/esi-environment/llama/llama-b11429/llama-server")
    try:
        manager.ensure_engine(settings, "llama")
        first = manager.owned[0]
        assert manager.backend["verified"] and manager.backend["device"] == "cpu"
        manager.ensure_engine(settings, "llama")
        assert manager.owned == [first] and manager.backend["verified"]
        changed = settings.model_copy(update={"context_tokens": 2048})
        manager.ensure_engine(changed, "llama")
        assert first.poll() is not None and len(manager.owned) == 1 and manager.owned[0] is not first
        assert manager.language_signature == manager.signature(changed)
        with httpx.Client(trust_env=False, timeout=3) as client:
            assert client.get("http://127.0.0.1:8080/health").json()["status"] == "ok"
    finally:
        manager.close()
    assert not manager.owned
