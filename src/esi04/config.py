"""ESI-04 runtime configuration; access grants are deliberately not persisted here."""

from typing import Literal

from pydantic import Field

from esi03.config import Settings as PreviousSettings


class Settings(PreviousSettings):
    release: Literal["ESI-04"] = "ESI-04"
    reply_tokens: int = Field(default=512, ge=128, le=1024)
    vision_url: str = "http://127.0.0.1:8081"
    vision_model_path: str = "models/qwen3-vl-2b/Qwen3VL-2B-Instruct-Q4_K_M.gguf"
    vision_projector_path: str = "models/qwen3-vl-2b/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
    vision_device: Literal["auto", "gpu", "cpu"] = "auto"
    vision_timeout: float = Field(default=180, ge=10, le=600)
    vision_context_tokens: int = Field(default=4096, ge=2048, le=8192)
    idle_deliberation: bool = True
    screen_history_mb: int = Field(default=512, ge=32, le=2048)
