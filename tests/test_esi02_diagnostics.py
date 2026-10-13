import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from esi02 import diagnostics
from esi02.config import Settings
from esi02.session import Session


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(diagnostics, "audio_devices", lambda: [])
    monkeypatch.setattr(diagnostics, "stream_format", lambda *args: (48000, 2, "test device"))
    monkeypatch.setattr(diagnostics, "nvidia_devices", lambda: ([], "test: no GPU"))
    monkeypatch.setattr(
        diagnostics,
        "probe_asr",
        lambda: {"state": "detected", "cuda_devices": 0, "verified_inference": False},
    )
    monkeypatch.setattr(diagnostics, "service_check", lambda *args: {"state": "not_checked"})


def test_report_reads_existing_save_without_changes_or_conversation_export(tmp_path, isolated):
    path = tmp_path / "save.sqlite3"
    session = Session(path, Settings(playback=False))
    turn = session.create_turn("PRIVATE_USER_MESSAGE")
    session.update_turn(turn["id"], reply="PRIVATE_AI_REPLY", stage="complete")
    view = session.view()
    session.close()
    before = path.read_bytes()
    observation = diagnostics.runtime_observation(view["status"], {}, view["turns"])
    report = diagnostics.collect_report(tmp_path, Settings(), path, observation=observation)
    saved = diagnostics.save_report(tmp_path, report)
    text = saved.read_text(encoding="utf-8")
    assert report["checks"]["save"]["state"] == "valid"
    assert path.read_bytes() == before
    assert "PRIVATE_USER_MESSAGE" not in text and "PRIVATE_AI_REPLY" not in text
    assert not (tmp_path / "audio").exists() and not (tmp_path / "output").exists()
    assert report["observations"]["physical_sound_confirmed"] is False
    assert report["checks"]["audio"]["formats"]["output"]["stream_opened"] is False


def test_missing_save_is_not_created_and_corrupt_configuration_is_reported(tmp_path, isolated):
    missing = tmp_path / "missing.sqlite3"
    config = tmp_path / "bad.toml"
    config.write_text("not valid toml", encoding="utf-8")
    report = diagnostics.collect_from_paths(tmp_path, missing, config)
    assert not missing.exists()
    assert report["checks"]["configuration"]["state"] == "error"
    assert report["checks"]["save"]["state"] == "not_created"
    assert not (tmp_path / "data").exists()


def test_corrupt_save_validation_never_exports_private_field_values(tmp_path, isolated):
    path = tmp_path / "bad.sqlite3"
    Session(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE snapshots SET payload=?",
            (json.dumps({"release": "ESI-02", "elapsed": "PRIVATE_CORRUPT_VALUE", "epoch": 0, "turns": []}),),
        )
    report = diagnostics.collect_from_paths(tmp_path, path)
    assert report["checks"]["save"]["state"] == "error"
    assert "PRIVATE_CORRUPT_VALUE" not in json.dumps(report)


def test_other_owned_process_cannot_keep_dead_language_gpu_verification(tmp_path):
    from esi02.services import Services

    manager = Services(tmp_path, {})
    settings = Settings()
    dead = SimpleNamespace(poll=lambda: 1)
    voice = SimpleNamespace(poll=lambda: None)
    manager.owned = [dead, voice]
    manager.language_process = dead
    manager.language_signature = manager.signature(settings)
    manager.backend = {"source": "owned_process", "device": "cuda", "verified": True}
    assert not manager.current_backend(settings)["verified"]
    manager.language_process = voice
    assert manager.current_backend(settings)["verified"]
    changed = settings.model_copy(update={"context_tokens": 2048})
    assert not manager.current_backend(changed)["verified"]


def test_dead_language_connection_keeps_stop_available_and_blocks_new_start(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    backend = {"source": "owned_process", "device": "cuda", "verified": True}
    services = SimpleNamespace(current_backend=lambda settings: dict(backend))
    window = gui.Window(Session(tmp_path / "save.sqlite3", Settings(playback=False)), backend, services)
    stopped = []
    window.runtime = SimpleNamespace(stop=lambda: stopped.append(True))
    try:
        backend.update(device="unknown", verified=False)
        window.refresh()
        assert not window.connection_ready and window.start_button.isEnabled()
        assert window.desktop.microphone.start_action.isEnabled()
        assert not window.retry_connection_button.isEnabled()
        window.toggle()
        assert stopped == [True] and window.runtime is None
        assert not window.start_button.isEnabled() and window.retry_connection_button.isEnabled()
        window.toggle()
        assert window.runtime is None
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_optional_model_hash_check_reports_mismatch_without_replacing_data(tmp_path, isolated, monkeypatch):
    expected = hashlib.sha256(b"expected model").hexdigest()
    monkeypatch.setattr(diagnostics, "MODELS", [("", "models/asr/model.bin", expected)])
    folder = tmp_path / "models/asr"
    folder.mkdir(parents=True)
    model = folder / "model.bin"
    model.write_bytes(b"user model")
    settings = Settings(asr_model_path="models/asr")
    quick = diagnostics.asset_check(tmp_path, settings)
    checked = diagnostics.asset_check(tmp_path, settings, verify=True)
    assert quick["files"][0]["present"] and not quick["files"][0]["checksum_verified"]
    assert checked["files"][0]["present"] and not checked["files"][0]["checksum_verified"]
    assert model.read_bytes() == b"user model"
    model.write_bytes(b"expected model")
    assert diagnostics.asset_check(tmp_path, settings, verify=True)["files"][0]["checksum_verified"]


def test_routing_error_does_not_abort_other_diagnostics(tmp_path, isolated):
    report = diagnostics.collect_report(
        tmp_path,
        Settings(),
        selections={"input": {"name": "missing microphone", "hostapi": "WASAPI"}, "output": None},
    )
    audio = report["checks"]["audio"]["formats"]
    assert audio["input"]["state"] == "error"
    assert audio["output"]["state"] == "compatible"
    assert report["checks"]["gpu_hardware"]["state"] == "unavailable"
    assert report["checks"]["asr_capability"]["verified_inference"] is False


def test_subprocess_native_failure_and_timeout_are_diagnostics_not_gui_crashes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=-11))
    assert diagnostics.probe_asr()["returncode"] == -11

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("test-probe", 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert diagnostics.probe_asr()["error_type"] == "TimeoutExpired"


def test_service_checks_use_only_get_and_do_not_synthesize_or_generate(monkeypatch):
    settings = Settings()
    voice = settings.audio_settings()
    requested = []

    def respond(request):
        requested.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen3-4B"}]})
        if request.url.path == "/version":
            return httpx.Response(200, json="1.1.0")
        if request.url.path.startswith("/aivm_models/"):
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "uuid": voice.tts_model_uuid,
                        "name": voice.tts_model_name,
                        "version": voice.tts_model_version,
                        "speakers": [{"uuid": voice.tts_speaker_uuid}],
                    }
                },
            )
        if request.url.path == "/speakers":
            return httpx.Response(
                200,
                json=[
                    {
                        "speaker_uuid": voice.tts_speaker_uuid,
                        "name": "凛音エル",
                        "styles": [{"name": voice.tts_style_name, "id": 7}],
                    }
                ],
            )
        pytest.fail("Unexpected request: " + str(request.url))

    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)
    )
    llm = diagnostics.service_check(settings, "llm")
    tts = diagnostics.service_check(settings, "aivis")
    assert llm["state"] == tts["state"] == "reachable"
    assert not llm["inference_tested"] and not tts["synthesis_tested"]
    assert tts["engine_version"] == "1.1.0" and tts["voice"]["speaker_name"] == "凛音エル"
    assert all(method == "GET" for method, _ in requested)
    assert not any("synthesis" in path or "completions" in path for _, path in requested)


def test_remote_or_authenticated_endpoint_is_not_contacted(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("Must not contact remote endpoint"))
    settings = Settings(llm_url="https://remote.invalid/?key=SECRET_TOKEN")
    result = diagnostics.service_check(settings, "llm")
    assert result["state"] == "not_checked" and "SECRET_TOKEN" not in json.dumps(result)


def test_report_redacts_account_paths_and_ignores_extra_runtime_fields(tmp_path, isolated):
    status = {
        "running": False,
        "reply": "PRIVATE_MESSAGE",
        "asr_device": {
            "device": "cuda",
            "verified_inference": True,
            "fallback_reason": str(Path.home() / "private.dll"),
        },
    }
    observation = diagnostics.runtime_observation(status, {"device": "cuda", "verified": False})
    report = diagnostics.collect_report(tmp_path, Settings(), observation=observation)
    text = json.dumps(report)
    assert "PRIVATE_MESSAGE" not in text and str(Path.home()) not in text
    assert report["observations"]["llm"]["verified"] is False
    assert "<USER>" in text


def test_doctor_command_generates_report_even_with_invalid_database(tmp_path, isolated, monkeypatch):
    from esi02.__main__ import main

    path = tmp_path / "unrelated.sqlite3"
    path.write_bytes(b"not an ESI database")
    report = tmp_path / "diagnostic.json"
    monkeypatch.chdir(tmp_path)
    assert main(["--database", str(path), "doctor", "--output", str(report)]) == 0
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["checks"]["save"]["state"] == "error"
    assert value["checks"]["configuration"]["state"] == "error"
    assert path.read_bytes() == b"not an ESI database"


def test_gui_diagnostics_run_without_blocking_or_exporting_old_gpu_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, QTimer
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    started, release = threading.Event(), threading.Event()
    observations = []

    def collect(root, settings, path, selections, observation, executable):
        observations.append(observation)
        started.set()
        assert release.wait(3)
        return {"test": True}

    monkeypatch.setattr(diagnostics, "collect_report", collect)
    services = SimpleNamespace(root=tmp_path, owned=[SimpleNamespace(poll=lambda: 0)])
    session = Session(tmp_path / "save.sqlite3", Settings(playback=False))
    window = gui.Window(session, {"source": "owned_process", "device": "cuda", "verified": True}, services)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    try:
        window.save_diagnostics()
        assert started.wait(1)
        deadline = time.monotonic() + 2
        while len(ticks) < 3 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert len(ticks) >= 3 and not window.diagnostics_button.isEnabled()
        assert observations[0]["llm"]["verified"] is False
        assert observations[0]["asr"]["observation_current"] is False
        window.save_diagnostics()
        assert len(observations) == 1
        release.set()
        window.diagnostics_future.result(timeout=3)
        window.refresh()
        assert "診断を保存しました" in window.diagnostics_hint.text()
        assert window.diagnostics_button.isEnabled()
        assert list((tmp_path / "data/esi02/diagnostics").glob("*.json"))
    finally:
        release.set()
        timer.stop()
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
