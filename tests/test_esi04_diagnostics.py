"""ESI-04 diagnostics remain read-only and exclude screen/teaching/conversation contents."""

import json

from esi.models import uid, utc
from esi04.config import Settings
from esi04.session import Session


def test_diagnostics_exclude_screen_explanations_and_keep_the_save_unchanged(tmp_path, monkeypatch):
    from esi02 import diagnostics as base
    from esi04 import diagnostics

    settings = Settings(playback=False)
    path = tmp_path / "save.db"
    session = Session(path, settings)
    session.create_turn("星砂村は極秘の月の国です。")
    session.screen_taught(dict(id=uid(), frame_id=uid(), frame_sha256="a"*64,
                              user_text="このボタンは秘密の送信ボタンです", observed_at=utc()))
    session.close()
    monkeypatch.setattr(base, "audio_check", lambda selections: {"state": "fixture"})
    monkeypatch.setattr(base, "probe_asr", lambda: {"state": "fixture"})
    monkeypatch.setattr(base, "nvidia_devices", lambda: ([], "no GPU in fixture"))
    before = path.read_bytes()
    report = diagnostics.collect_report(tmp_path, settings, path, check_services=False,
                                        vision_backend={"device": "cpu", "inference_verified": True})
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["release"] == "ESI-04"
    assert report["checks"]["save"]["state"] == "valid"
    assert report["observations"]["vision"]["inference_verified"]
    assert "秘密" not in serialized and "極秘" not in serialized
    assert not report["computer"]["grants_restored_on_start"]
    assert path.read_bytes() == before
