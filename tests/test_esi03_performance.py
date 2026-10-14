import json

import httpx
import pytest

from esi02.dialogue import Cancellation
from esi03.config import Settings
from esi03.performance import benchmark_language
from esi03.services import Services


def test_benchmark_measures_stream_and_never_becomes_character_evidence(monkeypatch):
    from esi03 import performance

    monkeypatch.setattr(performance, "nvidia_devices", lambda: ([], "no GPU on this test host"))
    chunks = [
        {"choices": [{"delta": {"content": "計測"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 4}},
    ]
    body = "\n\n".join("data: " + json.dumps(item) for item in chunks) + "\n\ndata: [DONE]\n\n"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    report = benchmark_language(
        Settings(),
        Cancellation(),
        runs=1,
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
    )
    assert report["runs"][0]["completion_tokens"] == 4
    assert report["median_first_token_seconds"] >= 0
    assert not report["used_for_character_learning"]
    assert not report["nvfp4"]
    assert not report["vram_peak_measured"]


def test_execution_signature_changes_when_flash_or_cache_changes():
    original = Settings()
    changed = Settings(cache_type="f16", flash_attention="off")
    assert Services.signature(original) != Services.signature(changed)


def test_empty_model_stream_is_not_reported_as_working(monkeypatch):
    from esi03 import performance

    monkeypatch.setattr(performance, "nvidia_devices", lambda: ([], ""))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="data: [DONE]\n"))
    with pytest.raises(ValueError):
        benchmark_language(
            Settings(),
            Cancellation(),
            runs=1,
            client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
        )
