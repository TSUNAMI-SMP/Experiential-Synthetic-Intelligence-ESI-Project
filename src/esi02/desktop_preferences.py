"""Desktop display preferences are separate from the experiment/save settings."""

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Coordinate = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
Position = Annotated[list[Coordinate], Field(min_length=2, max_length=2)]


class DesktopPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    microphone_visible: bool = False
    avatar_visible: bool = False
    microphone_position: Position | None = None
    avatar_position: Position | None = None
    avatar_width: int = Field(default=300, ge=120, le=900)
    model_path: str | None = Field(default=None, max_length=4096)


def read_preferences(path):
    path = Path(path)
    if not path.exists():
        return DesktopPreferences()
    if path.stat().st_size > 16_000:
        raise ValueError("デスクトップの表示設定が大きすぎます。")
    return DesktopPreferences.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_preferences(path, preferences):
    # Revalidate mutations, and replace atomically without a shared .tmp filename.
    value = DesktopPreferences.model_validate(preferences.model_dump())
    write_json(path, value.model_dump())


def write_json(path, value):
    """Atomic local UI preference storage; values must be validated by their caller."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
