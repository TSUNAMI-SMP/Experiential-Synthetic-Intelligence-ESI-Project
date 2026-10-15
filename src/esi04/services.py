"""On-demand vision process with independent ownership and real inference verification."""

import os
import subprocess
import time

import httpx

from esi02.assets import verify_file
from esi02.dialogue import Cancelled
from esi02.gpu import (
    Placement,
    choose_placement,
    confirmed_backend,
    llama_command,
    llama_devices,
    nvidia_devices,
)
from esi03.services import Services as PreviousServices
from esi04.setup_assets import VISION_FILES
from esi04.vision import VisionClient


class Services(PreviousServices):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = self.root / "data/esi04/logs/llama.log"
        self.vision_log = self.root / "data/esi04/logs/vision.log"
        self.vision_process = None
        self.vision_signature = None
        self.vision_backend = {"device": "unknown", "verified": False, "inference_verified": False}

    @staticmethod
    def vision_key(settings):
        return (settings.vision_url, settings.vision_model_path, settings.vision_projector_path,
                settings.vision_device, settings.vision_context_tokens, settings.cpu_threads)

    def stop_vision(self):
        with self.lock:
            process, self.vision_process = self.vision_process, None
            self.vision_signature = None
            self.vision_backend = {"device": "unknown", "verified": False, "inference_verified": False}
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process in self.owned:
                    self.owned.remove(process)

    def stop_owned(self):
        self.stop_vision()
        super().stop_owned()

    def _vision_alive(self, settings):
        try:
            with httpx.Client(base_url=settings.vision_url, trust_env=False, timeout=1) as client:
                return client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def _start_vision(self, settings, executable, model, projector, placement, cancellation):
        parameters = settings.model_copy(update={"llm_url": settings.vision_url,
                                                  "context_tokens": settings.vision_context_tokens})
        command = llama_command(executable, model, parameters, placement)+[
            "--mmproj", str(projector), "--image-max-tokens", "1024", "--no-context-shift",
            "--cache-ram", "0", "--flash-attn", settings.flash_attention,
            "--cache-type-k", settings.cache_type, "--cache-type-v", settings.cache_type,
        ]
        if not placement.layers:
            command.append("--no-mmproj-offload")
        self.vision_log.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            cancellation.check()
            if self.cancel.is_set():
                raise Cancelled()
            with self.vision_log.open("wb") as stream:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                process = subprocess.Popen(command, cwd=self.root, stdout=stream, stderr=subprocess.STDOUT, **options)
            self.vision_process = process
            self.owned.append(process)
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            cancellation.check()
            if self.cancel.is_set():
                raise Cancelled()
            if process.poll() is not None:
                raise RuntimeError("画面認識ソフトが起動中に終了しました。画面認識ログを確認してください。")
            if self._vision_alive(settings):
                backend = confirmed_backend(self.vision_log.read_text(encoding="utf-8", errors="replace"), placement)
                self.vision_backend = {**backend, "source": "owned_process", "inference_verified": False,
                                       "plan": placement.dump(), "reason": placement.reason}
                if placement.layers and not backend.get("verified"):
                    raise RuntimeError("画面認識モデルのGPU配置を起動ログで確認できません。")
                self.vision_signature = self.vision_key(settings)
                return
            cancellation.event.wait(0.2)
        raise RuntimeError("画面認識ソフトの起動が時間切れになりました。")

    def ensure_vision(self, settings, cancellation, force_cpu=False):
        if self.vision_process and self.vision_process.poll() is None:
            if self.vision_signature == self.vision_key(settings) and not force_cpu:
                return
            self.stop_vision()
        if self._vision_alive(settings):
            if settings.vision_device == "cuda":
                raise RuntimeError("起動済みの外部画面認識ソフトのGPU使用は確認できません。")
            self.vision_backend = {"device": "unknown", "verified": False, "source": "external_process",
                                   "inference_verified": False}
            return
        executable = self._llama_executable()
        model, projector = self.root / settings.vision_model_path, self.root / settings.vision_projector_path
        if not executable or not model.is_file() or not projector.is_file():
            raise FileNotFoundError("画面認識用AIデータか言語ソフトがありません。AIデータの準備を実行してください。")
        for path, (_, _, digest) in zip((model, projector), VISION_FILES, strict=True):
            if not verify_file(path, digest, cancellation.event):
                raise ValueError("画面認識用AIデータが指定版と一致しません。元ファイルは保持しました。")
        gpus, _ = nvidia_devices()
        supported, _ = llama_devices(executable)
        placement = choose_placement("cpu" if force_cpu else settings.vision_device, gpus, supported,
                                     model.stat().st_size+projector.stat().st_size, settings.gpu_reserve_mb,
                                     settings.vision_context_tokens)
        if not placement.layers:
            executable = self._cpu_executable(executable)
        self.notify("画面認識を必要なときだけ起動します。"+placement.reason)
        try:
            self._start_vision(settings, executable, model, projector, placement, cancellation)
        except RuntimeError as exc:
            if placement.layers and settings.vision_device == "auto" and not cancellation.event.is_set():
                self.stop_vision()
                if self.vision_log.exists():
                    self.vision_log.replace(self.vision_log.with_name(f"vision-gpu-failed-{time.time_ns()}.log"))
                self.notify("画面認識のGPU起動を完了できないためCPUで再試行します。")
                try:
                    self._start_vision(settings, self._cpu_executable(executable), model, projector,
                                       Placement("cpu", 0, str(exc)), cancellation)
                except (RuntimeError, Cancelled):
                    self.stop_vision()
                    raise
                return
            self.stop_vision()
            raise
        except Cancelled:
            self.stop_vision()
            raise

    def observe(self, settings, frame, question, cancellation):
        try:
            self.ensure_vision(settings, cancellation)
            result = VisionClient(settings).observe(frame, question, cancellation)
        except (RuntimeError, httpx.HTTPError) as exc:
            if settings.vision_device != "auto" or self.vision_backend.get("device") != "cuda":
                raise
            self.stop_vision()
            if self.vision_log.exists():
                self.vision_log.replace(self.vision_log.with_name(f"vision-gpu-failed-{time.time_ns()}.log"))
            self.notify(f"画面認識のGPU計算を継続できないためCPUで再試行します：{type(exc).__name__}")
            self.ensure_vision(settings, cancellation, force_cpu=True)
            result = VisionClient(settings).observe(frame, question, cancellation)
        self.vision_backend["inference_verified"] = True
        return result
