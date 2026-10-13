import io
import zipfile

import pytest

from esi02.assets import unpack
from esi02.config import Settings
from esi02.perception import AcceleratedPerception


def test_asr_auto_records_cuda_failure_and_uses_cpu(tmp_path, monkeypatch):
    import ctranslate2

    from esi.audio import Perception

    selected = []
    cpu = object()
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(Perception, "_create_model", lambda self: cpu)
    perception = AcceleratedPerception(Settings(), tmp_path, selected.append)
    assert perception._create_model() is cpu
    assert selected[-1]["device"] == "cpu" and selected[-1]["fallback_reason"]
    forced = AcceleratedPerception(Settings(asr_device="cuda"), tmp_path)
    with pytest.raises(RuntimeError, match="GPU音声認識"):
        forced._create_model()


def test_lazy_cuda_failure_during_transcription_retries_once_on_cpu(tmp_path, monkeypatch):
    from esi.audio import Perception

    calls, reported = [], []
    cpu = object()

    def transcribe(self, audio):
        calls.append(self.actual_device)
        if self.actual_device == "cuda":
            raise RuntimeError("cuDNN library unavailable while iterating segments")
        return ["real-segment-fixture"], "info-fixture"

    monkeypatch.setattr(Perception, "_transcribe", transcribe)
    monkeypatch.setattr(Perception, "_create_model", lambda self: cpu)
    perception = AcceleratedPerception(Settings(), tmp_path, reported.append)
    perception.actual_device, perception.compute_type = "cuda", "int8_float16"
    result = perception._transcribe([0.1])
    assert result[0] == ["real-segment-fixture"]
    assert calls == ["cuda", "cpu"]
    assert reported[-1]["verified_inference"] and reported[-1]["device"] == "cpu"
    assert "cuDNN" in reported[-1]["fallback_reason"]


def test_force_cuda_does_not_silently_change_experiment_to_cpu(tmp_path, monkeypatch):
    from esi.audio import Perception

    def fail(self, audio):
        raise RuntimeError("intentional CUDA failure")

    monkeypatch.setattr(Perception, "_transcribe", fail)
    perception = AcceleratedPerception(Settings(asr_device="cuda"), tmp_path)
    perception.actual_device = "cuda"
    with pytest.raises(RuntimeError, match="intentional"):
        perception._transcribe([0.1])
    assert perception.actual_device == "cuda"


@pytest.mark.parametrize("filename", ["../outside.dll", "C:/outside.dll", "nested/../../outside.dll"])
def test_binary_zip_cannot_extract_outside_staging(tmp_path, filename):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(filename, b"not a trusted library")
    archive.seek(0)
    with pytest.raises(ValueError, match="展開先"):
        unpack(archive, tmp_path / "staging")
    assert not (tmp_path / "outside.dll").exists()


def test_conversation_gui_routes_devices_and_displays_streamed_response(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from esi02 import gui
    from esi02.session import Session

    app = QApplication.instance() or QApplication([])
    selections = []
    devices = [
        {"index": 7, "name": "Mic", "hostapi": "WASAPI", "inputs": 1, "outputs": 0},
        {"index": 9, "name": "Speaker", "hostapi": "WASAPI", "inputs": 0, "outputs": 2},
    ]

    class FakeRuntime:
        microphone = None

        def __init__(self, session, input_device, output_device):
            selections.append((input_device, output_device))

        def start(self, microphone=False):
            selections.append(microphone)

        def send(self, name, data=None):
            selections.append((name, data))
            return True

        def stop(self):
            pass

    monkeypatch.setattr(gui, "audio_devices", lambda: devices)
    monkeypatch.setattr(gui, "Runtime", FakeRuntime)
    session = Session(tmp_path / "dialogue.sqlite3", Settings(playback=False))
    window = gui.Window(session, {"device": "cuda", "verified": False})
    window.show()
    try:
        app.processEvents()
        assert "未確認" in window.hardware.text()
        window.input_devices.setCurrentIndex(1)
        window.output_devices.setCurrentIndex(1)
        window.toggle()
        assert selections[:2] == [(7, 9), True]
        window.text.setText("こんにちは")
        window.submit_text()
        assert selections[-1] == ("text", "こんにちは")
        turn = session.create_turn("こんにちは")
        session.update_turn(turn["id"], reply="こんにちは。", stage="complete")
        window.refresh()
        assert "ESI-02：こんにちは。" in window.conversation.toPlainText()
        assert not window.input_devices.isEnabled()
        window.toggle()
        assert window.input_devices.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
