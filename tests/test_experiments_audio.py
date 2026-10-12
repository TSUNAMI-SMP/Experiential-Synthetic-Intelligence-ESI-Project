import copy
import json

import numpy as np
import pytest
import soundfile as sf

from esi.audio import FRAME, Microphone, Perception, acoustics, mono16
from esi.core import Core
from esi.experiments import compare, evaluate, measure
from esi.models import Settings


def test_holdout_improves_and_frozen_condition_is_unchanged(tmp_path):
    report = compare(tmp_path / "comparison", trials=80)
    learned, frozen = (report["conditions"][k] for k in ("learning", "frozen"))
    assert learned["heldout"]["mse"] < learned["baseline"]["mse"] * 0.1
    assert learned["heldout"]["choice_accuracy"] > frozen["heldout"]["choice_accuracy"]
    assert frozen["heldout"]["mse"] == frozen["baseline"]["mse"]
    assert learned["weights_changed"] and not frozen["weights_changed"]
    assert set(report["training_cues"]).isdisjoint(report["heldout_cues"])
    saved = json.loads((tmp_path / "comparison/comparison.json").read_text())
    assert saved == report


def test_evaluation_does_not_update_live_model_memory_or_state(tmp_path):
    core = Core(tmp_path / "live.sqlite3", Settings(llm_mode="off"))
    try:
        before = copy.deepcopy(core.dump())
        count = len(core.store.records())
        result = measure(core)
        assert core.dump() == before
        assert len(core.store.records()) == count
        core.checkpoint()
        measured = evaluate(core.store.path, tmp_path / "isolated-evaluation")
        assert measured == result
        assert core.dump() == before
        assert len(core.store.records()) == count
    finally:
        core.close()


def test_resampling_pitch_and_missing_values():
    sr = 48000
    stereo = np.tile((0.1 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr))[:, None], (1, 2))
    audio = mono16(stereo, sr)
    assert audio.shape == (16000,)
    frames, summary = acoustics(audio)
    assert 210 < summary["f0_hz"] < 230
    assert len(frames) > 30 and frames[-1].seconds > 0.9
    silent, missing = acoustics(np.zeros(16000, dtype=np.float32))
    assert missing["f0_hz"] is None
    assert all(f.f0_hz is None for f in silent)
    with pytest.raises(ValueError):
        mono16(np.array([float("nan")]), 16000)


def test_audio_callback_only_enqueues_and_overflow_is_bounded(tmp_path):
    perception = Perception(Settings(audio_queue_capacity=16), tmp_path / "audio")
    mic = Microphone(perception, lambda e: pytest.fail("callback ran ASR"), lambda k, v: None)
    for _ in range(24):
        mic._callback(np.zeros((FRAME, 1), dtype=np.float32), FRAME, None, False)
    assert mic.queue.qsize() == 16
    assert mic.dropped == 8
    assert perception.model is None


def test_recording_limit_is_enforced_before_loading_asr(tmp_path):
    wave = tmp_path / "oversized.wav"
    sf.write(wave, np.zeros(32000, dtype=np.float32), 16000)
    perception = Perception(Settings(recording_max_seconds=1), tmp_path / "input")
    with pytest.raises(ValueError, match="duration limit"):
        list(perception.recording(wave))
    assert perception.model is None
