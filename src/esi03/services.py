"""Configured CUDA/KV execution, with the same actual-inference verification as ESI-02."""

from esi02.services import Services as PreviousServices


class Services(PreviousServices):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = self.root / "data/esi03/logs/llama.log"
        self.execution_settings = None

    @staticmethod
    def signature(settings):
        return PreviousServices.signature(settings) + (settings.flash_attention, settings.cache_type)

    def _start_llama(self, settings, *args):
        self.execution_settings = settings
        return super()._start_llama(settings, *args)

    def _spawn_llama(self, command):
        settings = self.execution_settings
        if settings is not None:
            command = list(command) + [
                "--flash-attn",
                settings.flash_attention,
                "--cache-type-k",
                settings.cache_type,
                "--cache-type-v",
                settings.cache_type,
            ]
        return super()._spawn_llama(command)
