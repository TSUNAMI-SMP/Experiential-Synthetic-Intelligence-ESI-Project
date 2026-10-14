"""Measure real decoding without putting benchmark output into character memory."""

import json
import statistics
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import soundfile as sf

from esi.models import utc
from esi02.gpu import nvidia_devices
from esi03.perception import AcceleratedPerception


def benchmark_language(settings, cancellation, backend=None, runs=3, client_factory=httpx.Client):
    if runs not in {1, 3, 5}:
        raise ValueError("計測回数は1・3・5回から選んでください。")
    before, hardware_error = nvidia_devices()
    measurements = []
    with client_factory(
        base_url=settings.llm_url, timeout=min(60, settings.llm_timeout), trust_env=False
    ) as client:
        cancellation.attach(client.close)
        try:
            for _ in range(runs):
                cancellation.check()
                started, first = time.monotonic(), None
                completion_tokens, finish, chunks = None, None, 0
                payload = {
                    "model": settings.llm_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "次の文を一度だけ繰り返してください。これは計測用の短い文章です。",
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": 48,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                with client.stream("POST", "/v1/chat/completions", json=payload) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        cancellation.check()
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        event = json.loads(line[5:])
                        if event.get("error"):
                            raise RuntimeError(str(event["error"]))
                        if event.get("usage"):
                            completion_tokens = event["usage"].get("completion_tokens")
                        for choice in event.get("choices", []):
                            if choice.get("delta", {}).get("content"):
                                chunks += 1
                                if first is None:
                                    first = time.monotonic() - started
                            finish = choice.get("finish_reason") or finish
                duration = time.monotonic() - started
                if first is None or finish not in {"stop", "length"}:
                    raise ValueError("試験推論から計測できる応答を得られませんでした。")
                measurements.append(
                    {
                        "seconds": duration,
                        "first_token_seconds": first,
                        "completion_tokens": completion_tokens,
                        "stream_chunks": chunks,
                        "tokens_per_second": completion_tokens / duration if completion_tokens else None,
                    }
                )
        finally:
            cancellation.detach()
    after, after_error = nvidia_devices()
    return {
        "release": "ESI-03",
        "measured_at": utc(),
        "kind": "language",
        "runs": measurements,
        "median_first_token_seconds": statistics.median(item["first_token_seconds"] for item in measurements),
        "median_total_seconds": statistics.median(item["seconds"] for item in measurements),
        "hardware_before": [asdict(gpu) for gpu in before],
        "hardware_after": [asdict(gpu) for gpu in after],
        "hardware_error": hardware_error or after_error,
        "backend": backend or {"device": "unknown"},
        "settings": {
            key: getattr(settings, key)
            for key in ("context_tokens", "flash_attention", "cache_type", "llm_device")
        },
        "weights_format": "Q4_K_M",
        "nvfp4": False,
        "vram_peak_measured": False,
        "used_for_character_learning": False,
    }


def benchmark_asr(settings, path, directory, cancellation):
    path = Path(path)
    info = sf.info(path)
    if info.duration > 30 or info.duration <= 0:
        raise ValueError("音声の計測には30秒以内のサンプルを指定してください。")
    perception = AcceleratedPerception(settings, directory)
    cancellation.check()
    started = time.monotonic()
    events = list(perception.recording(path))
    cancellation.check()
    elapsed = time.monotonic() - started
    return {
        "release": "ESI-03",
        "measured_at": utc(),
        "kind": "asr",
        "seconds": elapsed,
        "sample_seconds": info.duration,
        "real_time_factor": elapsed / info.duration,
        "observations": len(events),
        "recognized_characters": sum(len(event.text) for event in events),
        "device": perception._state(verified=bool(events)),
        "used_for_character_learning": False,
    }
