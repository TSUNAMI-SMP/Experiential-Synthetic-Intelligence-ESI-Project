"""GPU failure paths preserve logs and retry CPU only in automatic mode."""

import hashlib

import pytest

from esi02.dialogue import Cancellation
from esi02.gpu import Placement
from esi04 import services as module
from esi04.config import Settings


def test_vision_gpu_start_failure_retries_cpu_and_keeps_language_owner(tmp_path, monkeypatch):
    files = []
    for name in ("model.gguf", "projector.gguf"):
        (tmp_path / name).write_bytes(name.encode())
        files.append((name, len(name), hashlib.sha256(name.encode()).hexdigest()))
    executable = tmp_path / "llama-server"
    executable.touch()
    monkeypatch.setattr(module, "VISION_FILES", files)
    monkeypatch.setattr(module, "nvidia_devices", lambda: ([], "fixture"))
    monkeypatch.setattr(module, "llama_devices", lambda path: ([], "fixture"))
    monkeypatch.setattr(module, "choose_placement", lambda *args: Placement("cuda", 99, "GPU fixture"))
    service = module.Services(tmp_path, {}, lambda _: None, executable=executable)
    monkeypatch.setattr(service, "_vision_alive", lambda settings: False)
    monkeypatch.setattr(service, "_cpu_executable", lambda executable: executable)
    language_owner = object()
    service.language_process = language_owner
    attempts = []
    def start(settings, executable, model, projector, placement, cancellation):
        attempts.append(placement.device)
        if placement.layers:
            service.vision_log.parent.mkdir(parents=True, exist_ok=True)
            service.vision_log.write_text("intentional CUDA kernel failure")
            raise RuntimeError("intentional CUDA kernel failure")
        service.vision_backend = {"device": "cpu", "verified": True, "inference_verified": False}
    monkeypatch.setattr(service, "_start_vision", start)
    settings = Settings(vision_model_path="model.gguf", vision_projector_path="projector.gguf")
    service.ensure_vision(settings, Cancellation())
    assert attempts == ["cuda", "cpu"]
    assert service.language_process is language_owner
    assert not service.vision_backend["inference_verified"]  # Starting is not image inference.
    assert len(list(service.vision_log.parent.glob("vision-gpu-failed-*.log"))) == 1
    attempts.clear()
    with pytest.raises(RuntimeError):
        service.ensure_vision(settings.model_copy(update={"vision_device": "cuda"}), Cancellation())
    assert attempts == ["cuda"]


def test_vision_actual_inference_failure_retries_cpu_without_claiming_gpu_success(tmp_path, monkeypatch):
    service = module.Services(tmp_path, {}, lambda _: None)
    attempts = []
    def ensure(settings, cancellation, force_cpu=False):
        attempts.append(force_cpu)
        service.vision_backend = {"device": "cpu" if force_cpu else "cuda", "inference_verified": False}
    monkeypatch.setattr(service, "ensure_vision", ensure)
    class Client:
        def __init__(self, settings):
            pass

        def observe(self, *args):
            if service.vision_backend["device"] == "cuda":
                raise RuntimeError("intentional unsupported CUDA operation")
            return "actual CPU result fixture"
    monkeypatch.setattr(module, "VisionClient", Client)
    assert service.observe(Settings(), None, "画面", Cancellation()) == "actual CPU result fixture"
    assert attempts == [False, True]
    assert service.vision_backend == {"device": "cpu", "inference_verified": True}
