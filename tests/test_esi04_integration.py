"""Actual local ASR -> ESI-04 owner -> Qwen -> the specified Rion voice -> save/restart."""

import os
import threading
import time
from pathlib import Path

import pytest
import soundfile as sf

from esi.adapters import TTS
from esi.models import Source
from esi03.perception import AcceleratedPerception
from esi04.config import Settings
from esi04.runtime import Runtime
from esi04.session import Session

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local ASR/Qwen/AivisSpeech required")]


def completed(session, count, timeout=220):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        turns = session.view()["turns"]
        if len(turns) > count and turns[-1]["stage"] in {"complete", "failed"}:
            assert turns[-1]["stage"] == "complete", turns[-1]["error"]
            return turns[-1]
        threading.Event().wait(.05)
    pytest.fail("ESI-04 real audio conversation did not finish")


def test_real_audio_dialogue_scene_unknowns_and_saved_experience(tmp_path):
    settings = Settings(playback=False, llm_device="cpu", asr_device="cpu", llm_timeout=180,
                        idle_deliberation=False,
                        asr_model_path=os.environ.get("ESI_ASR_MODEL_DIR", "/workspace/esi-environment/models/faster-whisper-small"),
                        llm_url=os.environ.get("ESI_LLAMA_URL", "http://127.0.0.1:8080"),
                        aivis_url=os.environ.get("ESI_AIVIS_URL", "http://127.0.0.1:10101"))
    voice = TTS(settings.audio_settings(), tmp_path / "fixture")
    wave = voice.synthesize("こんにちは。", "controlled-input")
    audio, rate = sf.read(wave["path"], dtype="float32")
    assert rate == 16000
    perception = AcceleratedPerception(settings, tmp_path / "recognized")
    event = perception.recognize(audio, Source.RECORDING, metadata={"controlled_tts_fixture": True})
    assert "こんにちは" in event.text and not event.metadata["recognition_needs_confirmation"]
    path = tmp_path / "save.sqlite3"
    session = Session(path, settings)
    session.character_operation("preferences", {"proactive": False, "exploration": False, "idle_activity": False})
    agent = session.agent_id
    runtime = Runtime(session, perception=perception)
    try:
        runtime.start()
        runtime.send("observe", event)
        greeting = completed(session, 0)
        assert "こんにちは" in greeting["reply"]
        runtime.send("text", "星砂村は月の国です。")
        teaching = completed(session, 1)
        assert teaching["reply"] and "何かお手伝い" not in teaching["reply"]
        assert session.knowledge.entries()[0]["value"] == "月の国"
        runtime.send("text", "ニューヨークとは何ですか？")
        unknown = completed(session, 2)
        assert "アメリカ" not in unknown["reply"] and "知らない" in unknown["reply"]
        runtime.send("text", "机の上におやつを置いて")
        placed = completed(session, 3)
        assert placed["scene_result"]["operation"] == "place"
        assert "疲労" not in placed["reply"] and "%" not in placed["reply"]
        for turn in (greeting, teaching, unknown, placed):
            assert turn["chunks"]
            assert all(c["audio"]["model_name"] == "凛音エル" and Path(c["audio"]["path"]).is_file()
                       and not c["played"] for c in turn["chunks"])
        assert session.store.records("llm_io")
        assert runtime.playback_meter.level() == 0
    finally:
        runtime.stop()
        session.close()
    restored = Session(path)
    try:
        assert restored.agent_id == agent
        assert restored.knowledge.entries()[0]["value"] == "月の国"
        assert any(o.kind == "snack" and o.parent_id for o in restored.world.objects)
        assert restored.view()["turns"][-1]["reply"] == placed["reply"]
    finally:
        restored.close()
