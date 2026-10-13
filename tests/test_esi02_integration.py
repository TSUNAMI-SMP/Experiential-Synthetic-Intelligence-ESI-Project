import os
import time
from pathlib import Path

import pytest
import soundfile as sf

from esi.adapters import TTS
from esi.models import Source
from esi02.config import Settings
from esi02.perception import AcceleratedPerception
from esi02.runtime import Runtime
from esi02.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local ASR/Qwen/AivisSpeech required"
    ),
]


def test_real_asr_streamed_dialogue_rion_audio_and_restart(tmp_path):
    settings = Settings(
        playback=False,
        reply_tokens=96,
        asr_model_path=os.environ.get(
            "ESI_ASR_MODEL_DIR", "/workspace/esi-environment/models/faster-whisper-small"
        ),
        llm_url=os.environ.get("ESI_LLAMA_URL", "http://127.0.0.1:8080"),
        aivis_url=os.environ.get("ESI_AIVIS_URL", "http://127.0.0.1:10101"),
    )
    input_voice = TTS(settings.audio_settings(), tmp_path / "fixture")
    wave = input_voice.synthesize("こんにちは。今日はよろしくお願いします。", "controlled-input")
    audio, rate = sf.read(wave["path"], dtype="float32")
    assert rate == 16000
    perception = AcceleratedPerception(settings, tmp_path / "recognized")
    event = perception.recognize(audio, Source.RECORDING, metadata={"controlled_tts_fixture": True})
    assert "こんにちは" in event.text
    session = Session(tmp_path / "esi02.sqlite3", settings)
    agent = session.agent_id
    runtime = Runtime(session, perception=perception)
    runtime.start()
    try:
        runtime.send("observe", event)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            turns = session.view()["turns"]
            if turns and turns[-1]["stage"] in {"complete", "failed"}:
                break
            time.sleep(0.05)
        else:
            pytest.fail("conversation did not complete within the deadline")
        turn = turns[-1]
        assert turn["stage"] == "complete", turn["error"]
        assert turn["reply"] and turn["chunks"]
        assert all(
            c["audio"]["model_name"] == "凛音エル" and Path(c["audio"]["path"]).is_file() and not c["played"]
            for c in turn["chunks"]
        )
        assert session.store.records("llm_io") and not session.store.records("outcome")
        assert session.world.elapsed > 0 and session.world.body.energy < 0.85
        assert session.epoch == 1
    finally:
        runtime.stop()
        session.close()
    restored = Session(tmp_path / "esi02.sqlite3")
    try:
        assert restored.agent_id == agent and restored.view()["turns"][-1]["reply"] == turn["reply"]
    finally:
        restored.close()
