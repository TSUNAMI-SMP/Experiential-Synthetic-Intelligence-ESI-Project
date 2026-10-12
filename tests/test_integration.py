import os
import time

import pytest

from esi.adapters import LLM, TTS
from esi.audio import FRAME, Perception, Segmenter
from esi.core import Core
from esi.models import ActionKind, Observation, Settings, Source
from esi.runtime import Runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ESI_RUN_INTEGRATION") != "1",
        reason="Set ESI_RUN_INTEGRATION=1 with actual local models and engines",
    ),
]


def configuration():
    return Settings(
        asr_model_path=os.environ.get(
            "ESI_ASR_MODEL_DIR", "/workspace/esi-environment/models/faster-whisper-small"
        ),
        llm_url=os.environ.get("ESI_LLAMA_URL", "http://127.0.0.1:8080"),
        aivis_url=os.environ.get("ESI_AIVIS_URL", "http://127.0.0.1:10101"),
        exploration=0,
        outcome_deadline=120,
        llm_timeout=120,
        llm_tokens=160,
    )


def test_real_tts_asr_streaming_vad_llm_and_speech_plan(tmp_path):
    import numpy as np
    import soundfile as sf

    settings = configuration()
    tts = TTS(settings, tmp_path / "tts")
    wave = tts.synthesize("こんにちは。音声認識の動作を確認します。", "integration-input")
    assert wave["engine"] == "AivisSpeech"
    assert wave["model_uuid"] == settings.tts_model_uuid and wave["model_name"] == "凛音エル"
    assert wave["style_name"] == "ノーマル"
    assert not wave["played"]
    audio, sr = sf.read(wave["path"], dtype="float32")
    segmenter = Segmenter(settings)
    detected = []
    samples = np.pad(audio, (0, 16000))
    for i in range(0, len(samples) - FRAME, FRAME):
        part = segmenter.feed(samples[i : i + FRAME])
        if part:
            detected.append(part)
    assert detected
    perception = Perception(settings, tmp_path / "recording")
    events = list(perception.recording(wave["path"]))
    assert events and "こんにちは" in "".join(e.text for e in events)
    assert all(e.source == Source.RECORDING and e.metadata["confidence_calibrated"] is False for e in events)
    core = Core(tmp_path / "pipeline.sqlite3", settings)
    try:
        event = events[0]
        core.observe(event)
        llm = LLM(settings)
        proposal = llm.interpret(core.context(event.event_id))
        assert core.interpret(event.event_id, proposal, core.context_version)
        action = core.choose(event.event_id, forced=ActionKind.INQUIRE)
        speech = llm.verbalize(core.context(event.event_id), action)
        output = tts.synthesize(speech.text, action.action_id)
        assert output["duration"] > 0
        assert core.predictor.counts["inquire"][1] == 0  # Generated speech is not external evidence.
    finally:
        core.close()


def test_real_autonomous_runtime_selects_and_generates_wave(tmp_path):
    settings = configuration()
    core = Core(tmp_path / "runtime.sqlite3", settings)
    runtime = Runtime(core)
    try:
        # Explicit test state: high unresolvedness makes inquiry useful, rather than forcing a reply.
        core.state.questions = {f"unresolved-{i}": "unresolved test question" for i in range(3)}
        core.state.unresolved = 0.75
        runtime.start()
        event = Observation(text="この未知の音について、何を確認したらよい？", category="question")
        runtime.send("observe", event)
        deadline = time.monotonic() + 160
        completed = []
        while time.monotonic() < deadline:
            completed = [a for a in core.view()["actions"] if a["audio_ref"] and a["stage"] == "complete"]
            if completed:
                break
            if any(str(core.status[k]).startswith("failed") for k in ("core", "llm", "tts")):
                pytest.fail(str(core.status))
            time.sleep(0.1)
        assert completed, core.view()
        assert core.store.records("speech_plan")
        assert any(e.source == Source.SELF_OUTPUT for e in core.events.values())
        assert core.predictor.counts[completed[-1]["kind"]][1] == 0
    finally:
        runtime.stop()
        core.close()
