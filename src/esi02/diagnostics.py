"""Read-only readiness checks; reports exclude conversations, recordings and saves."""

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import sqlite3
import struct
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from esi.adapters import TTS
from esi.devices import audio_devices, identity, load_preferences, resolve_device, stream_format
from esi02 import __version__
from esi02.avatar_assets import model_file
from esi02.config import Settings
from esi02.desktop_preferences import read_preferences, write_json
from esi02.gpu import llama_devices, nvidia_devices
from esi02.session import inspect_database
from esi02.setup_assets import ASR_FILES, MODELS

PACKAGES = (
    "numpy",
    "sounddevice",
    "soundfile",
    "PySide6",
    "faster-whisper",
    "ctranslate2",
    "onnxruntime",
    "live2d-py",
    "nvidia-cublas-cu12",
    "nvidia-cudnn-cu12",
    "nvidia-cuda-runtime-cu12",
    "nvidia-cuda-nvrtc-cu12",
)


def problem(exc):
    message = (
        "設定またはセーブの形式が正しくありません。" if isinstance(exc, ValidationError) else str(exc)[:1000]
    )
    return {"state": "error", "error_type": type(exc).__name__, "message": message}


def sanitize(value, root):
    # Diagnostic errors can contain native DLL paths. Keep device names, hide account paths.
    replacements = [(str(Path(root).resolve()), "<ESI>"), (str(Path.home()), "<USER>")]
    if isinstance(value, str):
        for original, replacement in replacements:
            if original not in {"/", "\\"}:
                value = value.replace(original, replacement).replace(original.replace("\\", "/"), replacement)
        return value
    if isinstance(value, dict):
        return {key: sanitize(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, root) for item in value]
    return value


def local_url(value):
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or url.username or url.password or url.query or url.fragment:
        return False
    if url.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        return False


def service_check(settings, kind):
    url = settings.llm_url if kind == "llm" else settings.aivis_url
    if not local_url(url):
        return {"state": "not_checked", "message": "ローカル以外の接続先は、この診断では問い合わせません。"}
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=3, follow_redirects=False) as client:
            if kind == "llm":
                health = client.get("/health")
                health.raise_for_status()
                if health.json().get("status") != "ok":
                    raise ValueError("言語ソフトが準備完了を返していません。")
                models = client.get("/v1/models")
                models.raise_for_status()
                matches = any(
                    settings.llm_model.lower() in str(m.get("id", "")).lower()
                    for m in models.json().get("data", [])
                )
                if not matches:
                    raise ValueError("言語ソフトのモデルが保存設定と一致しません。")
                return {"state": "reachable", "model_matches": True, "inference_tested": False}
            version = client.get("/version")
            version.raise_for_status()
            voice = TTS(settings.audio_settings()).resolve_voice(client)
            return {
                "state": "reachable",
                "engine_version": str(version.json())[:100],
                "voice": voice,
                "synthesis_tested": False,
                "physical_playback_tested": False,
            }
    except Exception as exc:
        return problem(exc)


def asr_capability():
    """Run in a child process: a missing native DLL must not kill the GUI."""
    from esi02.cuda_libraries import initialize

    initialize()
    import ctranslate2

    count = ctranslate2.get_cuda_device_count()
    return {
        "state": "detected",
        "cuda_devices": count,
        "cuda_compute_types": sorted(ctranslate2.get_supported_compute_types("cuda", 0)) if count else [],
        "verified_inference": False,
    }


def probe_asr():
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            [str(executable), "-m", "esi02.diagnostics", "probe-asr"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **options,
        )
        if result.returncode:
            return {
                "state": "error",
                "message": "音声認識ライブラリの確認用プロセスが終了しました。",
                "returncode": result.returncode,
                "verified_inference": False,
            }
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or "state" not in value:
            raise ValueError("音声認識の診断結果を読み込めません。")
        return value
    except Exception as exc:
        return {**problem(exc), "verified_inference": False}


def audio_check(selections):
    try:
        devices = audio_devices()
    except Exception as exc:
        return problem(exc)
    formats = {}
    for name, direction in (("input", "inputs"), ("output", "outputs")):
        try:
            selected = selections.get(name)
            device = resolve_device(selected, devices, direction)
            rate, channels, label = stream_format(device, name, 16000)
            formats[name] = {
                "state": "compatible",
                "selected": identity(selected),
                "device_name": label,
                "sample_rate": rate,
                "channels": channels,
                "stream_opened": False,
            }
        except Exception as exc:
            formats[name] = problem(exc)
    return {
        "state": "checked",
        "devices": devices,
        "formats": formats,
        "recording_tested": False,
        "physical_playback_tested": False,
    }


def save_check(path):
    if path is None or not Path(path).exists():
        return {"state": "not_created", "message": "セーブはまだありません。診断では作成しません。"}
    try:
        inspect_database(path)
        with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
            return {
                "state": "valid",
                "records": db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                "read_only": True,
            }
    except Exception as exc:
        return problem(exc)


def asset_check(root, settings, verify=False):
    entries = []
    for _, relative, digest in MODELS[:2]:
        path = (
            Path(settings.asr_model_path) / "model.bin"
            if relative.endswith("model.bin")
            else Path(root) / relative
        )
        if not path.is_absolute():
            path = Path(root) / path
        try:
            entry = {"name": relative, "present": path.is_file(), "checksum_verified": False}
            if entry["present"]:
                entry["bytes"] = path.stat().st_size
                if verify:
                    with path.open("rb") as file:
                        entry["checksum_verified"] = hashlib.file_digest(file, "sha256").hexdigest() == digest
            entries.append(entry)
        except Exception as exc:
            entries.append({"name": relative, **problem(exc)})
    asr = Path(settings.asr_model_path)
    if not asr.is_absolute():
        asr = Path(root) / asr
    missing = [name for name in sorted(ASR_FILES) if not (asr / name).is_file()]
    return {"state": "checked", "files": entries, "missing_asr_config": missing}


def runtime_observation(status=None, backend=None, turns=None):
    status, backend = status or {}, backend or {}
    asr = status.get("asr_device", {})
    audio = status.get("audio_input", {})
    chunks = [c for turn in (turns or []) for c in turn.get("chunks", [])]
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "running": bool(status.get("running")),
        "llm": {
            k: backend[k]
            for k in ("device", "verified", "source", "reason", "fallback_reason", "offloaded_layers", "inference_verified")
            if k in backend
        },
        "asr": {
            k: asr[k]
            for k in ("device", "compute_type", "verified_inference", "fallback_reason", "attempts", "loaded_cuda_libraries")
            if k in asr
        },
        "audio_input": {
            k: audio[k]
            for k in ("rms", "device_name", "recognized_intervals", "seconds_since_packet")
            if k in audio
        },
        "generated_chunks": len(chunks),
        "completed_playback_chunks": sum(bool(c.get("played")) for c in chunks),
        "physical_sound_confirmed": False,
    }


def llama_startup_logs(root):
    directory = Path(root) / "data/esi02/logs"
    candidates = list(directory.glob("llama-gpu-failed-*.log"))
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    current = directory / "llama.log"
    if current.is_file():
        candidates.insert(0, current)
    reports = []
    for path in candidates[:3]:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 128_000))
            content = stream.read(128_000).decode("utf-8", errors="replace")
        # Startup placement/errors only: do not export requests, prompts or replies.
        lines = [line[:500] for line in content.splitlines() if re.search(
            r"load_tensors:.*(?:offload|buffer)|ggml_cuda_init:|CUDA error:|cuBLAS failed|cuDNN failed", line
        )]
        verbosity = re.search(r"verbosity\s*=\s*(\d+)", content)
        reports.append({
            "file": path.name, "verbosity": int(verbosity[1]) if verbosity else None,
            "placement_records_found": any("offloaded" in line for line in lines),
            "startup_records": lines[-12:],
        })
    return {"state": "checked" if reports else "not_found", "logs": reports}


def collect_report(
    root,
    settings,
    database=None,
    selections=None,
    observation=None,
    executable=None,
    verify_models=False,
    check_services=True,
):
    root = Path(root).resolve()
    report = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "release": "ESI-02",
        "version": __version__,
        "development": True,
        "git_commit": repository_revision(root),
        "requested": {
            key: getattr(settings, key)
            for key in ("llm_device", "asr_device", "context_tokens", "playback", "gpu_reserve_mb")
        },
        "system": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_bits": struct.calcsize("P") * 8,
        },
        "checks": {},
        "observations": observation or runtime_observation(),
    }
    checks = report["checks"]
    try:
        checks["llama_startup_logs"] = llama_startup_logs(root)
    except OSError as exc:
        checks["llama_startup_logs"] = problem(exc)
    try:
        if selections is None:
            selections = load_preferences(Path(database).parent / "audio-devices.json") if database else {}
        checks["audio"] = audio_check(selections)
    except Exception as exc:
        checks["audio"] = problem(exc)
    checks["save"] = save_check(database)
    checks["assets"] = asset_check(root, settings, verify_models)
    checks["packages"] = {}
    for name in PACKAGES:
        try:
            checks["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            checks["packages"][name] = None
    devices, error = nvidia_devices()
    checks["gpu_hardware"] = {
        "state": "detected" if devices else "unavailable",
        "devices": [asdict(d) for d in devices],
        "message": error,
        "inference_tested": False,
    }
    checks["asr_capability"] = probe_asr()
    checks["llama_binaries"] = []
    candidates = (
        [Path(executable)]
        if executable
        else [root / name / "llama-server.exe" for name in ("llama-cuda", "llama-esi02-cpu", "llama")]
    )
    for candidate in candidates:
        if candidate.is_file():
            supported, error = llama_devices(candidate)
            checks["llama_binaries"].append(
                {"name": candidate.parent.name, "present": True, "cuda_devices": supported, "message": error}
            )
    for kind in ("llm", "aivis"):
        checks[kind] = (
            service_check(settings, kind)
            if check_services
            else {"state": "not_checked", "message": "設定を読めないため、接続確認は行っていません。"}
        )
    try:
        display = read_preferences(Path(database).parent / "esi02-desktop.json") if database else None
        checks["avatar"] = {
            "state": "selected" if display and display.model_path else "still_draft",
            "model_file_present": bool(
                display and display.model_path and model_file(display.model_path).is_file()
            ),
            "render_tested": False,
        }
    except Exception as exc:
        checks["avatar"] = problem(exc)
    report["manual_checks"] = [
        "マイクへ話したときに音量が動くか",
        "返答の音が実際に聞こえるか",
        "新しい発話で古い返答が中断されるか",
        "口の動きが聞こえる音に合うか",
    ]
    return sanitize(report, root)


def collect_from_paths(root, database, config=None, **kwargs):
    try:
        settings = (
            Settings.read(config)
            if config
            else inspect_database(database)
            if Path(database).exists()
            else Settings()
        )
        configuration = {
            "state": "valid",
            "source": "config" if config else "save" if Path(database).exists() else "default",
        }
    except Exception as exc:
        settings, configuration = Settings(), problem(exc)
    report = collect_report(
        root, settings, database, check_services=configuration["state"] == "valid", **kwargs
    )
    report["checks"]["configuration"] = sanitize(configuration, root)
    return report


def repository_revision(root):
    if not (Path(root) / ".git").exists():
        return None
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            **options,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def save_report(root, report, destination=None):
    path = (
        Path(destination)
        if destination
        else Path(root)
        / "data/esi02/diagnostics"
        / (datetime.now(UTC).strftime("diagnostic-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    )
    write_json(path, report)
    return path.resolve()


if __name__ == "__main__":
    if sys.argv[1:] != ["probe-asr"]:
        raise SystemExit("Use python -m esi02 doctor")
    try:
        result = asr_capability()
    except Exception as exc:
        result = {**problem(exc), "verified_inference": False}
    print(json.dumps(result, ensure_ascii=True))
