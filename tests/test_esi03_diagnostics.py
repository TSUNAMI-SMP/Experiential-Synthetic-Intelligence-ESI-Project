import json

from esi03.config import Settings
from esi03.session import Session


def test_diagnostics_accept_v3_save_without_exporting_words_or_documents(tmp_path, monkeypatch):
    from esi02 import diagnostics as base
    from esi03 import diagnostics

    path = tmp_path / "session.db"
    session = Session(path, Settings(playback=False))
    session.create_turn("星砂村は極秘の月の国です。")
    session.close()
    monkeypatch.setattr(base, "audio_check", lambda selections: {"state": "test"})
    monkeypatch.setattr(base, "probe_asr", lambda: {"state": "test"})
    monkeypatch.setattr(base, "nvidia_devices", lambda: ([], "no GPU in fixture"))
    before = path.read_bytes()
    report = diagnostics.collect_report(tmp_path, Settings(playback=False), path, check_services=False)
    assert report["release"] == "ESI-03"
    assert report["checks"]["save"]["state"] == "valid"
    assert "極秘" not in json.dumps(report, ensure_ascii=False)
    assert not report["model_format"]["nvfp4"]
    assert path.read_bytes() == before
