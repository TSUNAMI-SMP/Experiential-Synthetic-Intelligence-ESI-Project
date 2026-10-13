"""Verified, pinned Windows inference binaries. No driver installation or old-file overwrite."""

import hashlib
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PureWindowsPath

BINARIES = {
    "cuda": [
        (
            "llama-b11429-bin-win-cuda-13.4-x64.zip",
            "76ddc6eff2389570789ed608881efc6977751a722015d8e8c94f302224ff1a3a",
        ),
        (
            "cudart-llama-bin-win-cuda-13.4-x64.zip",
            "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668",
        ),
    ],
    "cpu": [
        (
            "llama-b11429-bin-win-cpu-x64.zip",
            "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe",
        )
    ],
}


def download(url, path, digest, notify=lambda message: None, cancel=None):
    path = Path(path)
    if path.exists():
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("既存のダウンロードが指定ハッシュと一致しません。ファイルは保持しました。")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=30) as response, partial.open("wb") as stream:
            received, reported = 0, 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("準備を中止しました。")
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                received += len(block)
                if received - reported >= 32 * 1024 * 1024:
                    notify(f"{path.name}：{received / 1024**2:.0f} MBを受信しました…")
                    reported = received
        with partial.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("言語ソフトのハッシュが一致しません。")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def unpack(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if sum(m.file_size for m in members) > 4_000_000_000:
            raise ValueError("展開サイズが大きすぎます。")
        for member in members:
            relative = member.filename.replace("\\", "/")
            target = (destination / relative).resolve()
            if (
                not target.is_relative_to(destination)
                or PureWindowsPath(relative).is_absolute()
                or ":" in relative
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("圧縮ファイルの展開先が正しくありません。")
        source.extractall(destination)


def install_llama(root, device="cuda", notify=print, cancel=None):
    if os.name != "nt":
        raise RuntimeError("この自動導入はWindows x64用です。")
    if device not in BINARIES:
        raise ValueError("cpuまたはcudaを指定してください。")
    root = Path(root)
    destination = root / ("llama-cuda" if device == "cuda" else "llama-esi02-cpu")
    if destination.exists():
        raise FileExistsError("言語ソフトの導入先がすでにあります。既存ファイルは保持しました。")
    with tempfile.TemporaryDirectory(prefix="esi02-download-", dir=root) as temporary:
        staging = Path(temporary) / "unpacked"
        staging.mkdir()
        for name, digest in BINARIES[device]:
            notify(f"{name} を公式配布から取得しています…")
            archive = Path(temporary) / name
            download(
                f"https://github.com/ggml-org/llama.cpp/releases/download/b11429/{name}",
                archive,
                digest,
                notify=notify,
                cancel=cancel,
            )
            if cancel is not None and cancel.is_set():
                raise RuntimeError("準備を中止しました。")
            unpack(archive, staging)
        if not (staging / "llama-server.exe").is_file():
            raise ValueError("言語ソフトの実行ファイルが見つかりません。")
        staging.replace(destination)
    return destination
