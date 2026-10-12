"""Verified model download; uses official pinned revisions, never disables TLS."""

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = [
    (
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "model.bin",
        "faster-whisper-small",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    ),
    (
        "Qwen/Qwen3-4B-GGUF",
        "bc640142c66e1fdd12af0bd68f40445458f3869b",
        "Qwen3-4B-Q4_K_M.gguf",
        "qwen3-4b",
        "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
    ),
]


def download(url, path, expected):
    def digest(p):
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    if path.exists():
        if digest(path) != expected:
            raise ValueError(f"Existing checksum mismatch: {path}; file preserved")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".download")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        if digest(partial) != expected:
            raise ValueError(f"Checksum mismatch: {path}")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def main():
    # Small ASR files are fixed by both the revision and Git blob IDs in the official Hub metadata.
    repo, revision, _, folder, _ = ASSETS[0]
    with urllib.request.urlopen(
        f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", timeout=30
    ) as response:
        metadata = json.load(response)
    if metadata["sha"] != revision:
        raise ValueError("ASR revision mismatch")
    for asset in metadata["siblings"]:
        name = asset["rfilename"]
        if name not in {"config.json", "tokenizer.json", "vocabulary.txt"}:
            continue
        path = ROOT / "models" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(
            f"https://huggingface.co/{repo}/resolve/{revision}/{name}", timeout=30
        ) as response:
            content = response.read()
        if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != asset["blobId"]:
            raise ValueError("Small ASR artifact verification failed")
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"Existing file differs: {path}")
        if not path.exists():
            path.write_bytes(content)
    for repo, revision, name, folder, digest in ASSETS:
        path = ROOT / "models" / folder / name
        download(f"https://huggingface.co/{repo}/resolve/{revision}/{name}", path, digest)
        print(f"Verified {path}")
    voice_path = ROOT / "models/aivis/f5017410-fbb5-49e1-97cb-e785f42e15f5.aivmx"
    download(
        "https://api.aivis-project.com/v1/aivm-models/f5017410-fbb5-49e1-97cb-e785f42e15f5/download?model_type=AIVMX",
        voice_path,
        "e7ac9e2636f59d6f6512016d0752f9e9f89c2b3ffd2012a6a1b92791330e23bc",
    )
    print(f"Verified 凛音エル v1.0.0 {voice_path}")


if __name__ == "__main__":
    main()
