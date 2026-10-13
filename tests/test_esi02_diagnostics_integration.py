import json
import os

import pytest

from esi02.config import Settings
from esi02.diagnostics import collect_report, save_report
from esi02.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local services required"),
]


def test_read_only_report_checks_real_native_libraries_services_and_pinned_models(tmp_path):
    (tmp_path / "models").symlink_to("/workspace/esi-environment/models", target_is_directory=True)
    database = tmp_path / "save.sqlite3"
    settings = Settings(playback=False)
    Session(database, settings).close()
    before = database.read_bytes()
    report = collect_report(
        tmp_path,
        settings,
        database,
        executable="/workspace/esi-environment/llama/llama-b11429/llama-server",
        verify_models=True,
    )
    checks = report["checks"]
    assert checks["llm"]["state"] == "reachable"
    assert checks["aivis"]["state"] == "reachable"
    assert checks["aivis"]["voice"]["speaker_name"] == "凛音エル"
    assert not checks["llm"]["inference_tested"] and not checks["aivis"]["synthesis_tested"]
    assert checks["asr_capability"]["state"] == "detected"
    assert not checks["asr_capability"]["verified_inference"]
    assert checks["save"]["state"] == "valid"
    assert all(item["checksum_verified"] for item in checks["assets"]["files"])
    assert checks["llama_binaries"][0]["present"]
    assert database.read_bytes() == before
    saved = save_report(tmp_path, report)
    assert json.loads(saved.read_text(encoding="utf-8"))["development"]
    assert not list(tmp_path.rglob("*.wav"))
    assert not (tmp_path / "audio").exists()
