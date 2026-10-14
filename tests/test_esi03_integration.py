import os
import threading
import time
from pathlib import Path

import pytest
import soundfile as sf

from esi.adapters import TTS
from esi.models import Source
from esi03.config import Settings
from esi03.perception import AcceleratedPerception
from esi03.runtime import Runtime
from esi03.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local ASR/Qwen/AivisSpeech required"
    ),
]


def completed_turn(session, previous, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turns = session.view()["turns"]
        if len(turns) > previous and turns[-1]["stage"] in {"complete", "failed"}:
            assert turns[-1]["stage"] == "complete", turns[-1]["error"]
            return turns[-1]
        threading.Event().wait(0.05)
    pytest.fail("ESI-03 audio conversation did not complete within the deadline")


def test_real_esi03_audio_owner_grounded_learning_unknowns_and_restart(tmp_path):
    settings = Settings(
        playback=False, llm_device="cpu", asr_device="cpu",
        asr_model_path=os.environ.get("ESI_ASR_MODEL_DIR", "/workspace/esi-environment/models/faster-whisper-small"),
        llm_url=os.environ.get("ESI_LLAMA_URL", "http://127.0.0.1:8080"),
        aivis_url=os.environ.get("ESI_AIVIS_URL", "http://127.0.0.1:10101"),
    )
    voice = TTS(settings.audio_settings(), tmp_path / "controlled-fixture")
    wave = voice.synthesize("こんにちは。", "controlled-input")
    audio, rate = sf.read(wave["path"], dtype="float32")
    assert rate == 16000
    perception = AcceleratedPerception(settings, tmp_path / "recognized")
    event = perception.recognize(audio, Source.RECORDING, metadata={"controlled_tts_fixture": True})
    assert "こんにちは" in event.text
    assert not event.metadata["recognition_needs_confirmation"]
    path = tmp_path / "esi03.sqlite3"
    session = Session(path, settings)
    session.character_operation("preferences", {"proactive": False, "exploration": False})
    agent_id = session.agent_id
    runtime = Runtime(session, perception=perception)
    runtime.start(microphone=False)
    try:
        assert runtime.send("observe", event)
        greeting = completed_turn(session, 0)
        assert "こんにちは" in greeting["reply"]
        assert runtime.send("text", "星砂村は月の国です。")
        teaching = completed_turn(session, 1)
        assert "月の国" in teaching["reply"]
        assert runtime.send("text", "ニューヨークとは何ですか？")
        unknown = completed_turn(session, 2)
        assert "アメリカ" not in unknown["reply"]
        assert "知らない" in unknown["reply"] or "教わって" in unknown["reply"]
        for turn in (greeting, teaching, unknown):
            assert turn["chunks"]
            assert all(
                chunk["audio"]["model_name"] == "凛音エル"
                and Path(chunk["audio"]["path"]).is_file()
                and not chunk["played"]
                for chunk in turn["chunks"]
            )
        assert len(session.knowledge.entries()) == 1
        assert session.experience.state.source_documents == 0
        assert runtime.playback_meter.level() == 0  # A generated WAV alone is not physical playback.
        assert session.store.records("llm_io")
    finally:
        runtime.stop()
        session.close()
    restored = Session(path)
    try:
        assert restored.agent_id == agent_id
        assert restored.knowledge.entries()[0]["value"] == "月の国"
        assert restored.view()["turns"][-1]["reply"] == unknown["reply"]
        assert not restored.experience.state.preferences.proactive
    finally:
        restored.close()
