"""Japanese ASR with verified CUDA precision retries and explicit input uncertainty."""

from esi.audio import Perception


class AcceleratedPerception(Perception):
    def __init__(self, settings, directory, notify=lambda state: None):
        super().__init__(settings.audio_settings(), directory)
        self.device_mode = settings.asr_device
        self.notify = notify
        self.actual_device = None
        self.compute_type = None
        self.fallback_reason = ""
        self.cuda_types = []
        self.attempts = []
        self.cpu_for_speech = False

    def _state(self, verified=False):
        from esi02.cuda_libraries import loaded_paths

        return {
            "device": self.actual_device,
            "compute_type": self.compute_type,
            "verified_inference": verified,
            "fallback_reason": self.fallback_reason,
            "attempts": [dict(item) for item in self.attempts],
            "loaded_cuda_libraries": loaded_paths(),
        }

    def _cpu(self):
        model = super()._create_model()
        self.actual_device, self.compute_type = "cpu", "int8"
        self.notify(self._state())
        return model

    def _cuda(self):
        from faster_whisper import WhisperModel

        last = None
        while self.cuda_types:
            compute = self.cuda_types.pop(0)
            attempt = {"device": "cuda", "compute_type": compute, "state": "loading"}
            self.attempts.append(attempt)
            try:
                model = WhisperModel(
                    self.settings.asr_model_path, device="cuda", device_index=0,
                    compute_type=compute, num_workers=1, local_files_only=True,
                )
            except (RuntimeError, OSError, ValueError) as exc:
                attempt.update(state="failed", error=str(exc)[:500])
                last = exc
                continue
            attempt["state"] = "loaded_unverified"
            self.actual_device, self.compute_type = "cuda", compute
            self.notify(self._state())
            return model
        raise RuntimeError(str(last) if last else "利用できるCUDA計算形式がありません。")

    def _create_model(self):
        if self.device_mode == "cpu" or self.cpu_for_speech:
            return self._cpu()
        try:
            from esi02.cuda_libraries import initialize

            initialize()
            import ctranslate2

            if ctranslate2.get_cuda_device_count() < 1:
                raise RuntimeError("CUDAで使用できるGPUが見つかりません。")
            supported = ctranslate2.get_supported_compute_types("cuda", 0)
            # A reported capability is only a candidate. In particular, mixed
            # int8/float16 may fail on a GPU even when device enumeration succeeds.
            self.cuda_types = [t for t in ("float16", "float32", "int8_float16") if t in supported]
            return self._cuda()
        except (RuntimeError, OSError, ValueError, ImportError) as exc:
            if self.device_mode == "cuda":
                raise RuntimeError(f"GPU音声認識を開始できません：{exc}") from exc
            self.fallback_reason = str(exc)
            return self._cpu()

    def _decode(self, audio):
        segments, info = self.model.transcribe(
            audio, language="ja", beam_size=5, condition_on_previous_text=False,
            vad_filter=False, hotwords="凛音エル、ESI",
        )
        # Native errors often happen only when the segment iterator is consumed.
        return list(segments), info

    def _release_failed_cuda(self):
        native = getattr(self.model, "model", None)
        unload = getattr(native, "unload_model", None)
        if unload:
            try:
                unload()
            except (RuntimeError, OSError, ValueError):
                # A failed CUDA context may also reject cleanup. Still release
                # the Python owner and try the remaining precision/CPU options.
                pass
        self.model = None

    def release_gpu_for_speech(self, stop):
        """Free automatic ASR placement after a reported synthesis allocation failure."""
        if self.device_mode != "auto":
            return False
        while not self.lock.acquire(timeout=0.1):
            if stop.is_set():
                return False
        try:
            if stop.is_set() or self.actual_device != "cuda":
                return False
            self._release_failed_cuda()
            self.cpu_for_speech = True
            self.cuda_types.clear()
            self.actual_device, self.compute_type = None, None
            self.fallback_reason = "音声合成でGPUメモリ不足が発生したため、聞き取りをCPUへ切り替えます。"
            self.notify(self._state())
            return True
        finally:
            self.lock.release()

    def _transcribe(self, audio):
        while True:
            try:
                result = self._decode(audio)
                break
            except (RuntimeError, OSError, ValueError) as exc:
                if self.actual_device != "cuda":
                    raise
                if self.attempts:
                    self.attempts[-1].update(state="failed", error=str(exc)[:500])
                if self.device_mode == "cuda":
                    self.notify(self._state())
                    raise
                self.fallback_reason = str(exc)
                self._release_failed_cuda()
                if any(term in str(exc).lower() for term in ("out of memory", "failed to allocate", "cuda_error_memory_allocation")):
                    # Trying float32 after a float16 allocation failure doubles
                    # memory pressure while language/vision models share VRAM.
                    self.cuda_types.clear()
                if self.cuda_types:
                    try:
                        self.model = self._cuda()
                        continue
                    except (RuntimeError, OSError, ValueError) as next_error:
                        self.fallback_reason = str(next_error)
                self.model = self._cpu()
        if self.actual_device == "cuda" and self.attempts:
            self.attempts[-1]["state"] = "verified"
            failed = [a["compute_type"] for a in self.attempts if a["state"] == "failed"]
            if failed:
                self.fallback_reason = f"CUDA {', '.join(failed)}で失敗したため{self.compute_type}を使用しています。"
        self.notify(self._state(verified=True))
        return result

    def recognize(self, *args, **kwargs):
        event = super().recognize(*args, **kwargs)
        segments = event.metadata.get("asr_segments", [])
        uncertain = not event.text.strip() or not segments or all(
            segment.get("avg_logprob", 0) < -1.0 or segment.get("no_speech_prob", 0) > 0.6
            for segment in segments
        )
        event.metadata.update(
            asr_device=self.actual_device,
            asr_compute_type=self.compute_type,
            asr_fallback_reason=self.fallback_reason,
            asr_attempts=[dict(item) for item in self.attempts],
            recognition_needs_confirmation=uncertain,
            recognition_quality_reason="認識の根拠が弱いか、発話を検出できませんでした。" if uncertain else "",
            recognition_parameters={
                "language": "ja", "beam_size": 5, "condition_on_previous_text": False,
                "hotwords": "凛音エル、ESI", "external_silero_vad": True,
            },
        )
        return event
