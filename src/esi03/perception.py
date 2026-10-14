"""A preferred ASR compute format is tried first; actual decoding still verifies support."""

from esi02.perception import AcceleratedPerception as PreviousPerception


class AcceleratedPerception(PreviousPerception):
    def __init__(self, settings, *args, **kwargs):
        self.preferred_compute = settings.asr_compute_type
        self.cpu_threads = settings.cpu_threads
        super().__init__(settings, *args, **kwargs)

    def _cuda(self):
        if self.preferred_compute in self.cuda_types:
            self.cuda_types.remove(self.preferred_compute)
            self.cuda_types.insert(0, self.preferred_compute)
        return super()._cuda()

    def _cpu(self):
        if self.preferred_compute != "float32":
            return super()._cpu()
        from faster_whisper import WhisperModel

        model = WhisperModel(
            self.settings.asr_model_path,
            device="cpu",
            compute_type="float32",
            local_files_only=True,
            cpu_threads=self.cpu_threads,
            num_workers=1,
        )
        self.actual_device, self.compute_type = "cpu", "float32"
        self.notify(self._state())
        return model
