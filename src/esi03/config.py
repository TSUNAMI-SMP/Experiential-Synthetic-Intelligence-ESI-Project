"""Versioned runtime settings; personality and live preferences belong to the character."""

from typing import Literal

from pydantic import Field

from esi02.config import Settings as PreviousSettings


class Settings(PreviousSettings):
    release: Literal["ESI-03"] = "ESI-03"
    reply_tokens: int = Field(default=384, ge=128, le=768)
    context_tokens: int = Field(default=4096, ge=2048, le=8192)
    llm_temperature: float = Field(default=0.55, ge=0, le=1.2)
    flash_attention: Literal["auto", "on", "off"] = "auto"
    cache_type: Literal["f16", "q8_0", "q4_0"] = "q8_0"
    asr_compute_type: Literal["auto", "float16", "float32", "int8_float16", "int8"] = "auto"
    natural_dialogue: bool = True
