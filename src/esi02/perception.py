"""CUDA ASR with explicit, recorded CPU fallback, including lazy CUDA failures."""

from esi.audio import Perception


class AcceleratedPerception(Perception):
    def __init__(self, settings, directory, notify=lambda state: None):
        super().__init__(settings.audio_settings(), directory)
        self.device_mode = settings.asr_device
        self.notify = notify
        self.actual_device = None
        self.compute_type = None
        self.fallback_reason = ""

    def _cpu(self):
        model = super()._create_model()
        self.actual_device, self.compute_type = "cpu", "int8"
        self.notify({"device": "cpu", "compute_type": "int8", "fallback_reason": self.fallback_reason})
        return model

    def _create_model(self):
        if self.device_mode == "cpu":
            return self._cpu()
        try:
            from esi02.cuda_libraries import initialize

            initialize()
            import ctranslate2
            from faster_whisper import WhisperModel

            if ctranslate2.get_cuda_device_count() < 1:
                raise RuntimeError("CUDAで使用できるGPUが見つかりません。")
            supported = ctranslate2.get_supported_compute_types("cuda", 0)
            compute = "int8_float16" if "int8_float16" in supported else "float16"
            model = WhisperModel(
                self.settings.asr_model_path,
                device="cuda",
                device_index=0,
                compute_type=compute,
                num_workers=1,
                local_files_only=True,
            )
            self.actual_device, self.compute_type = "cuda", compute
            self.notify({"device": "cuda", "compute_type": compute, "verified_inference": False})
            return model
        except (RuntimeError, OSError, ImportError) as exc:
            if self.device_mode == "cuda":
                raise RuntimeError(f"GPU音声認識を開始できません：{exc}") from exc
            self.fallback_reason = str(exc)
            return self._cpu()

    def _transcribe(self, audio):
        try:
            result = super()._transcribe(audio)
        except (RuntimeError, OSError) as exc:
            if self.actual_device != "cuda" or self.device_mode == "cuda":
                raise
            # cuDNN/cuBLAS can fail only while consuming the native segment iterator.
            self.model = None
            self.fallback_reason = str(exc)
            self.model = self._cpu()
            result = super()._transcribe(audio)
        self.notify(
            {
                "device": self.actual_device,
                "compute_type": self.compute_type,
                "verified_inference": True,
                "fallback_reason": self.fallback_reason,
            }
        )
        return result

    def recognize(self, *args, **kwargs):
        event = super().recognize(*args, **kwargs)
        event.metadata.update(
            asr_device=self.actual_device,
            asr_compute_type=self.compute_type,
            asr_fallback_reason=self.fallback_reason,
        )
        return event
