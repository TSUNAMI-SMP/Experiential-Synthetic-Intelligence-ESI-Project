from types import SimpleNamespace

import httpx
import pytest

from esi02 import services
from esi02.config import Settings
from esi02.gpu import GPU, choose_placement, confirmed_backend, llama_command
from esi02.perception import AcceleratedPerception


def test_trace_logging_and_latest_offload_record_avoid_false_cpu_fallback():
    placement = choose_placement("auto", [GPU(0, "RTX 5060", 8151, 5956, "610.62")], ["CUDA0"], 2497280256)
    command = llama_command("llama-server", "model", Settings(), placement)
    assert command[command.index("--log-verbosity") + 1] == "4"
    assert command[command.index("--log-colors") + 1] == "off"
    assert not confirmed_backend("verbosity = 3\nmodel loaded", placement)["verified"]
    result = confirmed_backend("\x1b[32mload_tensors: offloaded 37 / 37 layers to GPU\x1b[0m", placement)
    assert result["verified"] and result["offloaded_layers"] == 37
    assert not confirmed_backend("offloaded 37/37 layers to GPU\noffloaded 0/37 layers to GPU", placement)["verified"]


def test_loaded_language_backend_also_runs_an_unspoken_probe(tmp_path, monkeypatch):
    manager = services.Services(tmp_path, {})
    manager.log.parent.mkdir(parents=True)
    manager.log.write_text("load_tensors: offloaded 37/37 layers to GPU")
    commands, requests = [], []
    placement = choose_placement("auto", [GPU(0, "GPU", 8192, 7000, "driver")], ["CUDA0"], 2497280256)
    monkeypatch.setattr(manager, "_spawn_llama", lambda command: commands.append(command) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(manager, "alive", lambda *args: True)
    monkeypatch.setattr(manager, "ensure_language", lambda *args: None)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "はい"}}]})

    original = httpx.Client
    monkeypatch.setattr(services.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(respond)))
    manager._start_llama(Settings(), "llama-server", "model", placement)
    assert manager.backend["device"] == "cuda" and manager.backend["inference_verified"]
    assert len(requests) == 1 and requests[0].url.path == "/v1/chat/completions"
    assert commands


def test_float16_failure_retries_supported_float32_before_cpu(tmp_path, monkeypatch):
    import ctranslate2
    import faster_whisper

    from esi.audio import Perception

    loaded, events = [], []
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda *args: {"int8_float16", "float16", "float32"})

    class Whisper:
        def __init__(self, path, **kwargs):
            self.compute = kwargs["compute_type"]
            loaded.append(self.compute)

        def transcribe(self, audio, **kwargs):
            assert kwargs["beam_size"] == 5 and kwargs["language"] == "ja"
            if self.compute == "float16":
                def failed():
                    raise RuntimeError("cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED")
                    yield
                return failed(), None
            return iter(["segment"]), "info"

    monkeypatch.setattr(faster_whisper, "WhisperModel", Whisper)
    monkeypatch.setattr(Perception, "_create_model", lambda self: pytest.fail("CPU fallback must not happen"))
    perception = AcceleratedPerception(Settings(), tmp_path, events.append)
    perception.model = perception._create_model()
    assert not events[-1]["verified_inference"]
    assert perception._transcribe([.1]) == (["segment"], "info")
    assert loaded == ["float16", "float32"]
    assert events[-1]["device"] == "cuda" and events[-1]["verified_inference"]
    assert events[-1]["attempts"][0]["state"] == "failed"
    assert events[-1]["attempts"][1]["state"] == "verified"


def test_startup_diagnostic_selects_gpu_records_without_conversation(tmp_path):
    from esi02.diagnostics import llama_startup_logs

    logs = tmp_path / "data/esi02/logs"
    logs.mkdir(parents=True)
    (logs / "llama-gpu-failed-1.log").write_text(
        "verbosity = 3\nmodel loaded\nPRIVATE_CONVERSATION\n"
    )
    (logs / "llama.log").write_text(
        "verbosity = 4\nI load_tensors: offloaded 37/37 layers to GPU\nPRIVATE_REPLY\n"
    )
    result = llama_startup_logs(tmp_path)
    assert result["logs"][0]["placement_records_found"]
    assert result["logs"][1]["verbosity"] == 3
    assert not result["logs"][1]["placement_records_found"]
    assert "PRIVATE" not in str(result)


@pytest.mark.parametrize("failure", [ValueError("unsupported CUDA compute format"), RuntimeError("CUDA out of memory")])
def test_lazy_gpu_failure_and_failed_cleanup_redecode_the_same_audio_on_cpu(tmp_path, monkeypatch, failure):
    import ctranslate2
    import faster_whisper

    loaded, events, decoded = [], [], []
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda *args: {"float16", "float32"})

    class Whisper:
        def __init__(self, path, **kwargs):
            self.device = kwargs["device"]
            loaded.append((self.device, kwargs["compute_type"]))
            self.model = SimpleNamespace(unload_model=lambda: (_ for _ in ()).throw(RuntimeError("CUDA context failed")))

        def transcribe(self, audio, **kwargs):
            decoded.append((self.device, audio))
            if self.device == "cuda":
                def failed():
                    raise failure
                    yield
                return failed(), None
            return iter(["decoded same input"]), "info"

    monkeypatch.setattr(faster_whisper, "WhisperModel", Whisper)
    perception = AcceleratedPerception(Settings(), tmp_path, events.append)
    perception.model = perception._create_model()
    audio = [.1, .2, .3]
    assert perception._transcribe(audio) == (["decoded same input"], "info")
    assert all(data is audio for _, data in decoded)
    assert events[-1]["device"] == "cpu" and events[-1]["verified_inference"]
    assert str(failure) in events[-1]["fallback_reason"]
    if "out of memory" in str(failure):
        assert loaded == [("cuda", "float16"), ("cpu", "int8")]
    else:
        assert loaded == [("cuda", "float16"), ("cuda", "float32"), ("cpu", "int8")]


def test_explicit_gpu_requirement_reports_a_lazy_error_without_cpu_fallback(tmp_path, monkeypatch):
    perception = AcceleratedPerception(Settings(asr_device="cuda"), tmp_path)
    perception.actual_device, perception.compute_type = "cuda", "float16"
    monkeypatch.setattr(perception, "_decode", lambda _: (_ for _ in ()).throw(ValueError("unsupported format")))
    monkeypatch.setattr(perception, "_cpu", lambda: pytest.fail("Explicit CUDA must remain explicit"))
    with pytest.raises(ValueError, match="unsupported format"):
        perception._transcribe([.1])
