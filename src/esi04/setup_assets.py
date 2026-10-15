"""Pinned visual model, separate from the reusable conversation/audio assets."""

from pathlib import Path

from esi02.assets import download
from esi02.setup_assets import prepare_assets as prepare_base_assets

REVISION = "52d6c8ffea26cc873ac5ad116f8631268d7eb503"
VISION_FILES = (
    ("Qwen3VL-2B-Instruct-Q4_K_M.gguf", 1107409952,
     "089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae"),
    ("mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf", 445053216,
     "f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82"),
)


def vision_missing(root):
    return any(not (Path(root) / "models/qwen3-vl-2b" / name).is_file() for name, _, _ in VISION_FILES)


def prepare_vision(root, notify=print, cancel=None):
    notify("画面認識用AIデータを準備します（追加 約1.55 GB）。会話用データは再利用します。")
    for name, _, digest in VISION_FILES:
        download(f"https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/{REVISION}/{name}",
                 Path(root) / "models/qwen3-vl-2b" / name, digest, notify, cancel)


def prepare_assets(root, settings, notify=print, cancel=None, include_vision=True):
    prepare_base_assets(root, settings, notify, cancel)
    if include_vision:
        prepare_vision(root, notify, cancel)
