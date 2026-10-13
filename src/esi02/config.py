import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from esi.models import Settings as AudioSettings


class Settings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False
    )
    release: Literal["ESI-02"] = "ESI-02"
    llm_url: str = "http://127.0.0.1:8080"
    llm_model: str = "Qwen3-4B"
    llm_timeout: float = Field(default=120, gt=0, le=600)
    reply_tokens: int = Field(default=256, ge=32, le=512)
    context_tokens: int = Field(default=4096, ge=2048, le=8192)
    llm_device: Literal["auto", "cpu", "cuda"] = "auto"
    asr_device: Literal["auto", "cpu", "cuda"] = "auto"
    asr_model_path: str = "models/faster-whisper-small"
    aivis_url: str = "http://127.0.0.1:10101"
    playback: bool = True
    cpu_threads: int = Field(default=2, ge=1, le=16)
    gpu_reserve_mb: int = Field(default=1536, ge=512, le=8192)
    tick_seconds: float = Field(default=0.05, ge=0.01, le=1)
    checkpoint_seconds: float = Field(default=15, ge=1, le=120)
    history_pairs: int = Field(default=6, ge=1, le=12)
    retained_turns: int = Field(default=128, ge=16, le=512)
    max_records: int = Field(default=200_000, ge=1000)
    command_capacity: int = Field(default=256, ge=32, le=1024)
    speech_capacity: int = Field(default=8, ge=2, le=32)

    def audio_settings(self):
        # Reuse the verified audio library, not the ESI-01 action selector.
        return AudioSettings(
            asr_model_path=self.asr_model_path,
            aivis_url=self.aivis_url,
            cpu_threads=self.cpu_threads,
            playback=self.playback,
        )

    @property
    def fingerprint(self):
        content = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode()).hexdigest()

    @classmethod
    def read(cls, path):
        with Path(path).open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))
