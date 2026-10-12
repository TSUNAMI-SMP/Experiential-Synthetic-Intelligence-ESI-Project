import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from esi.adapters import play_wave
from esi.audio import FRAME, Microphone, Perception
from esi.core import Core
from esi.devices import load_preferences, resolve_device, save_preferences
from esi.models import ActionKind, Observation, Settings, Source
from esi.presentation import describe
from esi.runtime import Runtime


def devices():
    return [
        {"index": 7, "name": "USB microphone", "hostapi": "WASAPI", "inputs": 2, "outputs": 0},
        {"index": 9, "name": "Headphones", "hostapi": "WASAPI", "inputs": 0, "outputs": 2},
    ]


def test_saved_devices_follow_identity_not_old_index_and_missing_does_not_fallback(tmp_path):
    inputs, output = devices()
    path = tmp_path / "audio-devices.json"
    save_preferences(path, inputs, output, True)
    saved = load_preferences(path)
    changed = [{**inputs, "index": 20}, {**output, "index": 25}]
    assert resolve_device(saved["input"], changed, "inputs") == 20
    assert resolve_device(saved["output"], changed, "outputs") == 25
    with pytest.raises(ValueError, match="選び直してください"):
        resolve_device(saved["input"], [output], "inputs")
    assert resolve_device(None, [], "inputs") is None


def mock_native_device(monkeypatch, direction, opened):
    import sounddevice as sd

    def query(device, kind):
        assert device == (7 if direction == "input" else 9) and kind == direction
        return {"name": "Chosen native device", "default_samplerate": 48000, f"max_{direction}_channels": 2}

    def check(**settings):
        if settings["samplerate"] != 48000 or settings["channels"] != 2:
            raise sd.PortAudioError("Device requires 48 kHz stereo")

    class Stream:
        def __init__(self, **kwargs):
            self.settings = kwargs
            opened.append(kwargs)

        def start(self):
            values = (0.08 * np.sin(2 * np.pi * 220 * np.arange(1536) / 48000)).astype(np.float32)
            self.settings["callback"](np.tile(values[:, None], (1, 2)), 1536, None, False)

        def stop(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, samples):
            opened.append(samples.copy())

    monkeypatch.setattr(sd, "query_devices", query)
    monkeypatch.setattr(sd, f"check_{direction}_settings", check)
    monkeypatch.setattr(sd, "InputStream" if direction == "input" else "OutputStream", Stream)


def test_native_stereo_microphone_is_resampled_and_has_meter_before_asr(tmp_path, monkeypatch):
    from esi import audio

    frames = []

    class SilentSegmenter:
        def __init__(self, settings):
            self.frames = []
            self.probability = 0.1

        def feed(self, frame):
            frames.append(frame.copy())
            return None

    opened, statuses = [], []
    mock_native_device(monkeypatch, "input", opened)
    monkeypatch.setattr(audio, "Segmenter", SilentSegmenter)
    perception = Perception(Settings(), tmp_path / "audio")
    mic = Microphone(
        perception,
        lambda e: pytest.fail("No speech was detected"),
        lambda k, v: statuses.append((k, v)),
        device=7,
    )
    try:
        mic.start()
        deadline = time.monotonic() + 2
        while not frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert frames and all(f.shape == (FRAME,) for f in frames)
        assert opened[0]["device"] == 7 and opened[0]["samplerate"] == 48000 and opened[0]["channels"] == 2
        assert mic.telemetry["rms"] > 0.03 and mic.telemetry["packets_received"] == 1
        assert mic.telemetry["speech_intervals"] == 0 and perception.model is None
        mic._report(force=True)
        assert any(k == "audio_input" and v["device_name"] == "Chosen native device" for k, v in statuses)
    finally:
        mic.close()


def test_recognition_busy_is_visible_before_a_slow_model_returns(tmp_path):
    perception = Perception(Settings(), tmp_path / "audio")
    entered, release = threading.Event(), threading.Event()
    events, statuses = [], []

    def recognize(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return Observation(source=Source.MICROPHONE, text="認識完了")

    perception.recognize = recognize
    mic = Microphone(perception, events.append, lambda k, v: statuses.append((k, v)))
    mic.asr_thread = threading.Thread(target=mic._asr_worker)
    mic.asr_thread.start()
    try:
        mic.segments.put((np.zeros(FRAME), {"utterance_id": "test", "continues": False}, 1, "test"))
        assert entered.wait(1)
        assert not events and mic.telemetry["asr_busy"]
        assert any(k == "asr" and v.startswith("transcribing") for k, v in statuses)
        release.set()
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert events and events[0].text == "認識完了"
    finally:
        release.set()
        mic.close()


def test_removed_microphone_still_closes_and_allows_shutdown(tmp_path):
    import sounddevice as sd

    closed, statuses = [], []

    class RemovedDevice:
        def stop(self):
            raise sd.PortAudioError("Device disconnected")

        def close(self):
            closed.append(True)

    mic = Microphone(
        Perception(Settings(), tmp_path / "audio"), lambda e: None, lambda k, v: statuses.append((k, v))
    )
    mic.stream = RemovedDevice()
    mic.close()
    assert closed and mic.stop_event.is_set()
    assert any("disconnected" in message for component, message in statuses)


def test_output_uses_selected_device_native_format_and_cancellation(tmp_path, monkeypatch):
    opened = []
    mock_native_device(monkeypatch, "output", opened)
    path = tmp_path / "tone.wav"
    sf.write(path, np.ones(1600, dtype=np.float32) * 0.03, 16000)
    route = {}
    assert play_wave(path, device=9, routing=route)
    assert opened[0]["device"] == 9 and opened[0]["samplerate"] == 48000 and opened[0]["channels"] == 2
    assert np.concatenate(opened[1:]).shape == (4800, 2)
    assert route["playback_device_name"] == "Chosen native device"
    stop = threading.Event()
    stop.set()
    assert not play_wave(path, stop, device=9)


@pytest.mark.parametrize(
    "audio,title",
    [
        ({"packets_received": 0}, "音声データを待っています"),
        ({"packets_received": 4, "peak": 0}, "ほぼ無音"),
        ({"packets_received": 4, "peak": 0.1, "speaking": True}, "声らしい音"),
        ({"asr_busy": True, "asr_seconds": 8}, "言葉に変換"),
        ({"packets_received": 4, "seconds_since_packet": 3}, "途絶え"),
    ],
)
def test_status_distinguishes_no_data_silence_voice_asr_and_disconnection(audio, title):
    view = {"status": {"microphone": "running", "audio_input": audio}}
    assert title in describe(view, True)["title"]


def test_status_reports_model_failure_instead_of_calling_it_deliberate_silence():
    view = {
        "status": {"llm": "failed: ConnectionError: connection refused", "jobs": []},
        "input_action": {"kind": "wait", "stage": "complete"},
    }
    result = describe(view, True)
    assert result["error"] and "問題" in result["title"] and "connection refused" in result["detail"]
    assert (
        describe({"status": {"jobs": [{"kind": "interpret", "seconds": 9}]}}, True)["phase"] == "interpreting"
    )
    queued = {
        "status": {"jobs": [{"kind": "interpret", "seconds": 9, "stale": True}], "pending_input": "new-input"}
    }
    assert describe(queued, True)["phase"] == "queued"


def test_idle_choices_do_not_hide_last_input_decision(tmp_path):
    core = Core(tmp_path / "session.sqlite3", Settings(llm_mode="off"))
    try:
        event = Observation(text="聞こえていますか？", source=Source.MICROPHONE)
        core.observe(event)
        selected = core.choose(event.event_id, forced=ActionKind.WAIT)
        for _ in range(20):
            core.choose()
        view = core.view()
        assert view["input_action"]["action_id"] == selected.action_id
        assert "待つ" in describe(view, True)["title"]
    finally:
        core.close()


def test_runtime_passes_output_choice_to_playback_without_training_on_test_audio(tmp_path, monkeypatch):
    from esi import runtime as runtime_module

    selected = []

    def playback(path, stop, device, routing):
        selected.append(device)
        return True

    monkeypatch.setattr(runtime_module, "play_wave", playback)
    core = Core(tmp_path / "session.sqlite3", Settings(playback=True, exploration=0))
    runtime = Runtime(core, input_device=7, output_device=9)
    try:
        event = Observation(text="入力")
        core.observe(event)
        action = core.choose(event.event_id, forced=ActionKind.SPEAK)
        future = Future()
        future.set_result(
            {
                "path": "wave.wav",
                "duration": 0.1,
                "model_name": "凛音エル",
                "model_version": "1.0.0",
                "style_name": "ノーマル",
                "played": False,
            }
        )
        runtime.jobs[future] = {
            "kind": "synthesis",
            "action_id": action.action_id,
            "text": "発声",
            "version": core.context_version,
        }
        runtime._complete_jobs()
        for pending in list(runtime.jobs):
            pending.result(timeout=1)
        runtime._complete_jobs()
        assert selected == [9]
        assert core.actions[action.action_id].stage == "complete"
        output = core.view()["latest_output"]
        assert output["metadata"]["played"] and output["metadata"]["playback_device_id"] == 9
        assert core.predictor.counts["speak"][1] == 0
    finally:
        runtime.stop()
        core.close()


def test_stopped_microphone_cannot_overwrite_new_capture_status(tmp_path):
    core = Core(tmp_path / "restart.sqlite3", Settings(llm_mode="off"))
    runtime = Runtime(core)
    try:
        runtime.microphone = SimpleNamespace(capture_id="current")
        runtime._command("microphone_status", ("old", "asr", "failed: stale worker error"))
        assert core.status["asr"] == "not_checked"
        runtime._command("microphone_status", ("current", "audio_input", {"rms": 0.1}))
        assert core.status["audio_input"] == {"rms": 0.1}
        runtime.microphone = None
        runtime._command("microphone_status", ("current", "audio_input", {"rms": 0.8}))
        assert core.status["audio_input"] == {"rms": 0.1}
    finally:
        runtime.microphone = None
        runtime.stop()
        core.close()
