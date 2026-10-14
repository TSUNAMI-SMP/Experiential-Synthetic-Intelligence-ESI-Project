"""Verified, pinned Windows inference binaries. No driver installation or old-file overwrite."""

import hashlib
import os
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
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


def size_text(size):
    return f"{size / 1_000_000_000:.2f} GB" if size >= 1_000_000_000 else f"{size / 1_000_000:.1f} MB"


@dataclass(frozen=True)
class DownloadProgress:
    name: str
    phase: str
    received: int = 0
    total: int | None = None
    speed: float = 0

    @property
    def percent(self):
        if self.phase != "download" or not self.total:
            return None
        return min(100, self.received * 100 // self.total)

    def __str__(self):
        if self.phase == "verify":
            return f"{self.name}：ファイルを照合中です…"
        if self.phase == "complete":
            return f"{self.name}：準備できました（照合済み）"
        if self.phase == "connect":
            return f"{self.name}：ダウンロード先へ接続中です…"
        amount = size_text(self.received)
        amount += f" / {size_text(self.total)}（{self.percent}%）" if self.total else " / 総容量不明"
        speed = f"{self.speed / 1_000_000:.2f} MB/秒" if self.speed > 0 else "速度を計測中"
        remaining = "残り時間は計測中"
        if self.total and self.speed > 0:
            seconds = max(0, int((self.total - self.received) / self.speed))
            remaining = f"残り約{seconds // 60}分{seconds % 60:02d}秒"
        elif not self.total:
            remaining = "残り時間は不明"
        return f"{self.name}：ダウンロード中\nこのファイル：{amount} ｜ {speed} ｜ {remaining}"


def verify_file(path, digest, cancel):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("準備を中止しました。")
            checksum.update(block)
    return checksum.hexdigest() == digest


def download(url, path, digest, notify=lambda message: None, cancel=None):
    path = Path(path)
    if path.exists():
        notify(DownloadProgress(path.name, "verify"))
        if not verify_file(path, digest, cancel):
            raise ValueError("既存のダウンロードが指定ハッシュと一致しません。ファイルは保持しました。")
        notify(DownloadProgress(path.name, "complete"))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        notify(DownloadProgress(path.name, "connect"))
        with urllib.request.urlopen(url, timeout=30) as response, partial.open("wb") as stream:
            try:
                total = int(getattr(response, "headers", {}).get("Content-Length", "0"))
            except (TypeError, ValueError):
                total = 0
            total = total if total > 0 else None
            received = 0
            started = reported = time.monotonic()
            notify(DownloadProgress(path.name, "download", total=total))
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("準備を中止しました。")
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                received += len(block)
                now = time.monotonic()
                if now - reported >= 1:
                    notify(
                        DownloadProgress(
                            path.name, "download", received, total, received / max(now - started, 0.001)
                        )
                    )
                    reported = now
            notify(
                DownloadProgress(
                    path.name, "download", received, total, received / max(time.monotonic() - started, 0.001)
                )
            )
        if total is not None and received != total:
            raise ValueError("ダウンロードが途中で終了しました。再試行してください。")
        notify(DownloadProgress(path.name, "verify", received, total))
        if not verify_file(partial, digest, cancel):
            raise ValueError("ダウンロードしたファイルのハッシュが一致しません。")
        partial.replace(path)
        notify(DownloadProgress(path.name, "complete", received, total))
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
