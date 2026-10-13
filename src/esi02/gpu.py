"""Select CUDA only when hardware AND the inference binary support it."""

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GPU:
    index: int
    name: str
    total_mb: int
    free_mb: int
    driver: str


@dataclass(frozen=True)
class Placement:
    device: str
    layers: int
    reason: str
    gpu: GPU | None = None

    def dump(self):
        return asdict(self)


def nvidia_devices():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode:
            return [], "NVIDIAドライバーからGPU情報を取得できません。"
        devices = []
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 5:
                continue
            index, name, total, free, driver = parts
            devices.append(GPU(int(index), name, int(total), int(free), driver))
        return devices, "" if devices else "NVIDIA GPUが見つかりません。"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [], f"GPUを確認できません：{type(exc).__name__}"


def llama_devices(executable):
    try:
        result = subprocess.run(
            [str(executable), "--list-devices"], capture_output=True, text=True, errors="replace", timeout=10
        )
        if result.returncode:
            return [], "言語ソフトのGPU対応を確認できません。"
        output = result.stdout + "\n" + result.stderr
        return re.findall(r"\b(CUDA\d+):", output), ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"言語ソフトのGPU確認に失敗：{type(exc).__name__}"


def choose_placement(mode, gpus, supported, model_bytes, reserve_mb=1536, context_tokens=4096):
    if mode == "cpu":
        return Placement("cpu", 0, "設定によりCPUを使用します。")
    candidates = [g for g in gpus if f"CUDA{g.index}" in supported]
    if not candidates:
        reason = "GPUかCUDA対応の言語ソフトが見つからないためCPUを使用します。"
        if mode == "cuda":
            raise RuntimeError(reason)
        return Placement("cpu", 0, reason)
    gpu = max(candidates, key=lambda g: g.free_mb)
    # Includes working buffers, KV cache, UI and ASR headroom; this is an estimate, not measured usage.
    estimated_mb = model_bytes / 1024**2 * 1.2 + context_tokens / 4096 * 512
    available = gpu.free_mb - reserve_mb
    if available < estimated_mb:
        reason = "GPUの空きメモリーが安全余裕を含む推定必要量に足りないためCPUを使用します。"
        if mode == "cuda":
            raise RuntimeError(reason)
        return Placement("cpu", 0, reason, gpu)
    return Placement(
        f"CUDA{gpu.index}", 99, "CUDAでの読み込みを試します。実際のGPU使用は起動ログで確認します。", gpu
    )


def plan_llama(executable, model, settings):
    gpus, hardware_error = nvidia_devices()
    supported, binary_error = llama_devices(executable)
    placement = choose_placement(
        settings.llm_device,
        gpus,
        supported,
        Path(model).stat().st_size,
        settings.gpu_reserve_mb,
        settings.context_tokens,
    )
    return placement, {"hardware": hardware_error, "binary": binary_error}


def llama_command(executable, model, settings, placement):
    from urllib.parse import urlparse

    url = urlparse(settings.llm_url)
    if url.hostname not in {"127.0.0.1", "localhost"} or not url.port:
        raise ValueError("ローカル起動にはlocalhostのポート付きURLが必要です。")
    command = [
        str(executable),
        "--model",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        str(url.port),
        "--ctx-size",
        str(settings.context_tokens),
        "--parallel",
        "1",
        "--threads",
        str(settings.cpu_threads),
        "--gpu-layers",
        str(placement.layers),
        "--reasoning",
        "off",
        "--jinja",
    ]
    if placement.layers:
        command.extend(["--device", placement.device])
    return command


def confirmed_backend(log, placement):
    match = re.search(r"offloaded\s+(\d+)/(\d+)\s+layers\s+to\s+GPU", log, re.IGNORECASE)
    if match and int(match.group(1)) > 0:
        return {"device": "cuda", "offloaded_layers": int(match.group(1)), "verified": True}
    if placement.layers == 0:
        return {"device": "cpu", "offloaded_layers": 0, "verified": True}
    return {"device": "unknown", "offloaded_layers": 0, "verified": False}
