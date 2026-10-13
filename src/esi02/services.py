"""GPU-aware local startup. Keep external services and ESI-01 assets intact."""

import hashlib
import os
import subprocess
import time
from pathlib import Path

from esi.desktop import Services as OriginalServices
from esi02.gpu import Placement, confirmed_backend, llama_command, plan_llama

MODEL_SHA = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"


class Services(OriginalServices):
    def __init__(self, root, preferences, notify=lambda message: None, timeout=120, executable=None):
        super().__init__(root, preferences, notify, timeout)
        self.backend = {"device": "unknown", "verified": False}
        self.executable_override = executable
        self.log = self.root / "data/esi02/logs/llama.log"
        self.language_signature = None
        self.language_process = None

    def current_backend(self, settings):
        backend = dict(self.backend)
        if backend.get("source") == "owned_process" and (
            self.language_process is None
            or self.language_process.poll() is not None
            or self.language_signature != self.signature(settings)
        ):
            backend.update(
                device="unknown",
                verified=False,
                reason="このセーブの言語ソフトの動作を現在確認できません。接続を確認し直してください。",
            )
        return backend

    @staticmethod
    def signature(settings):
        return (
            settings.llm_url,
            settings.llm_model,
            settings.llm_device,
            settings.context_tokens,
            settings.cpu_threads,
            settings.gpu_reserve_mb,
        )

    def stop_owned(self):
        had_owned = bool(self.owned)
        super().stop_owned()
        self.language_signature = None
        self.language_process = None
        if had_owned:
            self.backend = {"device": "unknown", "verified": False, "reason": "言語ソフトを停止しました。"}

    def _llama_executable(self):
        if self.executable_override:
            return Path(self.executable_override)
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        choices = [
            self.root / "llama-cuda" / name,
            self.root / "llama-esi02-cpu" / name,
            self.root / "llama" / name,
        ]
        return next((p for p in choices if p.is_file()), None)

    def _cpu_executable(self, fallback):
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        return next(
            (p for p in [self.root / "llama-esi02-cpu" / name, self.root / "llama" / name] if p.is_file()),
            fallback,
        )

    def _spawn_llama(self, command):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with self.lock:
            if self.cancel.is_set():
                raise RuntimeError("起動を中止しました。")
            # Each attempt has its own fresh log, so old GPU messages cannot prove new offloading.
            with self.log.open("wb") as stream:
                process = subprocess.Popen(
                    command, cwd=self.root, stdout=stream, stderr=subprocess.STDOUT, **options
                )
            self.owned.append(process)
            self.language_process = process
        return process

    def _start_llama(self, settings, executable, model, placement):
        process = self._spawn_llama(llama_command(executable, model, settings, placement))
        deadline = time.monotonic() + self.timeout
        while not self.cancel.is_set() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"言語ソフトが終了しました（{process.returncode}）。起動ログを確認してください。"
                )
            if self.alive(settings, "llama"):
                self.ensure_language(settings)
                log = self.log.read_text(encoding="utf-8", errors="replace")
                self.backend = {
                    **confirmed_backend(log, placement),
                    "plan": placement.dump(),
                    "source": "owned_process",
                    "log": str(self.log),
                    "reason": placement.reason,
                }
                if placement.layers and not self.backend["verified"]:
                    raise RuntimeError("GPUへの読み込みを確認できませんでした。")
                self.language_signature = self.signature(settings)
                return
            self.cancel.wait(0.25)
        raise RuntimeError(
            "起動を中止しました。" if self.cancel.is_set() else "言語ソフトの起動が時間切れになりました。"
        )

    def ensure_engine(self, settings, kind):
        if kind != "llama":
            return super().ensure_engine(settings, kind)
        owns_language = bool(self.language_process and self.language_process.poll() is None)
        if owns_language and self.language_signature != self.signature(settings):
            # A changed saved context/device must not silently reuse the old process settings.
            self.stop_owned()
            owns_language = False
        if self.alive(settings, "llama"):
            self.ensure_language(settings)
            if owns_language and self.backend.get("source") == "owned_process":
                self.notify("ESIが起動した言語ソフトを引き続き利用します。")
                return
            if settings.llm_device == "cuda":
                raise RuntimeError(
                    "起動済みの言語ソフトのGPU使用を確認できません。一度そのソフトを閉じ、ESIから起動してください。"
                )
            self.backend = {
                "device": "unknown",
                "verified": False,
                "source": "external_process",
                "reason": "起動済みの言語ソフトを利用しています。GPU使用は未確認です。",
            }
            self.notify(self.backend["reason"])
            return
        executable = self._llama_executable()
        model = self.root / "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf"
        if not executable or not model.is_file():
            raise FileNotFoundError("言語ソフトかQwen3-4Bのデータがありません。")
        self.notify("AIデータを照合し、GPUの対応と空き容量を確認しています…")
        with model.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != MODEL_SHA:
                raise ValueError("Qwen3-4Bのデータが指定版と一致しません。元のファイルは保持しました。")
        placement, diagnosis = plan_llama(executable, model, settings)
        if placement.layers == 0 and diagnosis.get("binary"):
            executable = self._cpu_executable(executable)
        self.notify(placement.reason)
        try:
            self._start_llama(settings, executable, model, placement)
        except RuntimeError as exc:
            if placement.layers == 0 or settings.llm_device == "cuda" or self.cancel.is_set():
                raise
            self.stop_owned()
            fallback_reason = str(exc)
            self.notify(f"GPUで開始できません：{fallback_reason} CPUで起動します。")
            # Preserve the failed GPU attempt for diagnosis before replacing its live log.
            if self.log.exists():
                self.log.replace(self.log.with_name(f"llama-gpu-failed-{time.time_ns()}.log"))
            self._start_llama(
                settings, self._cpu_executable(executable), model, Placement("cpu", 0, fallback_reason)
            )
            self.backend["fallback_reason"] = fallback_reason
        self.backend["probe"] = diagnosis

    def prepare(self, settings):
        if not (self.root / settings.asr_model_path / "model.bin").is_file():
            raise FileNotFoundError("音声認識のAIデータがありません。")
        self.ensure_engine(settings, "llama")
        self.ensure_engine(settings, "aivis")
        self.ensure_voice(settings.audio_settings())
        if self.cancel.is_set():
            raise RuntimeError("起動を中止しました。")
