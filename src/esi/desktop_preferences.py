"""Display/launcher preferences, deliberately outside the experiment configuration."""

import json
from pathlib import Path


def read_preferences(path):
    path = Path(path)
    if not path.exists():
        return {}
    if path.stat().st_size > 64_000:
        raise ValueError("画面設定のファイルが大きすぎます。")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("画面設定の形式が正しくありません。")
    return value


def write_preferences(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
