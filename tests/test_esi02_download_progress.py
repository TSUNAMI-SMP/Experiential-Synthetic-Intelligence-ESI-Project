import hashlib
import io

import pytest

from esi02 import assets


@pytest.mark.parametrize("length", ["2097152", None, "invalid"])
def test_download_progress_reports_transfer_then_verification_without_false_total(
    tmp_path, monkeypatch, length
):
    content = b"x" * (2 * 1024 * 1024)

    class Response(io.BytesIO):
        headers = {} if length is None else {"Content-Length": length}

    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *a, **kw: Response(content))
    clock = iter(range(100))
    monkeypatch.setattr(assets.time, "monotonic", lambda: next(clock))
    events = []
    target = tmp_path / "model.gguf"
    assets.download("https://test.invalid/model", target, hashlib.sha256(content).hexdigest(), events.append)
    transfers = [e for e in events if e.phase == "download"]
    assert target.read_bytes() == content
    assert transfers[-1].received == len(content) and transfers[-1].speed > 0
    assert [e.phase for e in events][-2:] == ["verify", "complete"]
    if length == "2097152":
        assert transfers[1].percent == 50 and transfers[-1].percent == 100
        assert "残り約" in str(transfers[1])
    else:
        assert all(e.total is None and e.percent is None for e in transfers)
        assert "総容量不明" in str(transfers[-1])


def test_transfer_eof_and_bad_hash_never_report_verified_completion(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        headers = {"Content-Length": "100"}

    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *a, **kw: Response(b"short"))
    events = []
    target = tmp_path / "model.gguf"
    with pytest.raises(ValueError, match="途中で終了"):
        assets.download(
            "https://test.invalid/model", target, hashlib.sha256(b"short").hexdigest(), events.append
        )
    assert not target.exists() and not target.with_suffix(".gguf.partial").exists()
    assert all(e.phase != "complete" for e in events)
    Response.headers = {}
    with pytest.raises(ValueError, match="ハッシュ"):
        assets.download("https://test.invalid/model", target, "0" * 64, events.append)
    assert all(e.phase != "complete" for e in events)


def test_existing_model_is_verified_and_reused_without_network(tmp_path, monkeypatch):
    target = tmp_path / "model.gguf"
    target.write_bytes(b"existing")
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("redownload"))
    events = []
    assets.download(
        "https://test.invalid/model", target, hashlib.sha256(b"existing").hexdigest(), events.append
    )
    assert [e.phase for e in events] == ["verify", "complete"]
