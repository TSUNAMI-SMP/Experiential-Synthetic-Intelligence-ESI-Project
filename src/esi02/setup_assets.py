"""Pinned first-run assets, shared by the GUI launcher and Windows preparation."""

import hashlib
import json
import os
import urllib.request
from pathlib import Path

from esi02.assets import download, install_llama
from esi02.gpu import nvidia_devices

ASR_REPO = "Systran/faster-whisper-small"
ASR_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
ASR_FILES = {"config.json", "tokenizer.json", "vocabulary.txt"}
MODELS = (
    (
        "https://huggingface.co/Systran/faster-whisper-small/resolve/" + ASR_REVISION + "/model.bin",
        "models/faster-whisper-small/model.bin",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    ),
    (
        "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/"
        "bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf",
        "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf",
        "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
    ),
    (
        "https://api.aivis-project.com/v1/aivm-models/"
        "f5017410-fbb5-49e1-97cb-e785f42e15f5/download?model_type=AIVMX",
        "models/aivis/f5017410-fbb5-49e1-97cb-e785f42e15f5.aivmx",
        "e7ac9e2636f59d6f6512016d0752f9e9f89c2b3ffd2012a6a1b92791330e23bc",
    ),
)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise RuntimeError("準備を中止しました。")


def models_missing(root, include_voice=False):
    root = Path(root)
    required = MODELS if include_voice else MODELS[:2]
    return any(not (root / name).is_file() for _, name, _ in required) or any(
        not (root / "models/faster-whisper-small" / name).is_file() for name in ASR_FILES
    )


def prepare_models(root, notify=print, cancel=None):
    root = Path(root)
    check_cancel(cancel)
    notify("聞き取り用のAIデータの指定版を公式配布と照合しています…")
    with urllib.request.urlopen(
        f"https://huggingface.co/api/models/{ASR_REPO}/revision/{ASR_REVISION}?blobs=true", timeout=30
    ) as response:
        metadata = json.load(response)
    if metadata.get("sha") != ASR_REVISION:
        raise ValueError("聞き取りのデータの指定版が一致しません。")
    siblings = {item["rfilename"]: item for item in metadata["siblings"]}
    if not ASR_FILES.issubset(siblings):
        raise ValueError("聞き取りの設定ファイルが揃っていません。")
    for name in sorted(ASR_FILES):
        check_cancel(cancel)
        with urllib.request.urlopen(
            f"https://huggingface.co/{ASR_REPO}/resolve/{ASR_REVISION}/{name}", timeout=30
        ) as response:
            content = response.read(4_000_001)
        if (
            len(content) > 4_000_000
            or hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            != siblings[name]["blobId"]
        ):
            raise ValueError("聞き取りの設定データを照合できませんでした。")
        path = root / "models/faster-whisper-small" / name
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError(f"既存のファイルが指定版と異なります。元ファイルは保持しました：{path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    for url, name, digest in MODELS:
        check_cancel(cancel)
        notify(f"{Path(name).name} を準備しています（初回は数GBのダウンロードがあります）…")
        download(url, root / name, digest, notify=notify, cancel=cancel)
    notify("聞き取り・会話・凛音エルのデータの照合が終わりました。")


def prepare_binaries(root, settings, notify=print, cancel=None, windows=None):
    root = Path(root)
    if not (os.name == "nt" if windows is None else windows):
        notify("Windowsの言語ソフト自動導入はこのOSでは行いません。起動済みのソフトを利用します。")
        return
    # Always retain an independent CPU binary for a failed GPU start.
    check_cancel(cancel)
    if not (root / "llama-esi02-cpu/llama-server.exe").is_file():
        install_llama(root, "cpu", notify, cancel=cancel)
    devices, reason = nvidia_devices()
    if settings.llm_device == "cpu" or not devices:
        if settings.llm_device == "cuda":
            raise RuntimeError(reason or "NVIDIA GPUを確認できません。")
        notify("CPU用の言語ソフトを準備しました。" + reason)
        return
    check_cancel(cancel)
    if not (root / "llama-cuda/llama-server.exe").is_file():
        install_llama(root, "cuda", notify, cancel=cancel)
    notify("CUDA版の準備が終わりました。GPUでの実計算は起動・聞き取り後に確認します。")


def prepare_assets(root, settings, notify=print, cancel=None):
    prepare_models(root, notify, cancel)
    prepare_binaries(root, settings, notify, cancel)
