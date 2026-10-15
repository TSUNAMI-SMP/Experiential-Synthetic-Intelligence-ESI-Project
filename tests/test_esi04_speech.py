"""Speech failures must not discard text, repeat scene effects or leak request text."""

import threading
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

from esi02.diagnostics import runtime_observation
from esi02.speech import SpeechFailure
from esi03.perception import AcceleratedPerception
from esi04.config import Settings
from esi04.runtime import Runtime
from esi04.session import Session


def until(condition):
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        if condition():
            return
        threading.Event().wait(.02)
    raise AssertionError("Speech recovery did not finish")


class Voice:
    def __init__(self, root):
        self.root, self.calls = root, 0

    def synthesize(self, text, identifier):
        self.calls += 1
        path = self.root / (identifier+".wav")
        sf.write(path, np.full(1600, .15), 16000)
        return {"path": str(path), "duration": .1, "played": False, "model_name": "凛音エル"}


class Memory:
    def extract(self, *args):
        return {"items": [], "rejected": []}


def setup(tmp_path, **kwargs):
    session = Session(tmp_path / "save.sqlite3", Settings(playback=True, natural_dialogue=False, idle_deliberation=False))
    session.character_operation("preferences", {"proactive": False, "idle_activity": False, "exploration": False})
    voice = Voice(tmp_path)
    runtime = Runtime(session, tts=voice, memory_llm=Memory(), **kwargs)
    return session, runtime, voice


def test_failed_speaker_keeps_the_reply_and_retry_does_not_repeat_a_scene_command(tmp_path):
    broken = True

    def output(*args):
        if broken:
            raise OSError("test speaker disconnected")
        return True

    session, runtime, voice = setup(tmp_path, playback=output)
    runtime.start()
    try:
        runtime.send("text", "机の上におやつを置いて")
        until(lambda: session.view()["turns"] and session.view()["turns"][-1]["stage"] == "failed")
        turn = session.view()["turns"][-1]
        assert turn["reply"] and turn["speech_error"]["stage"] == "playback"
        assert runtime.thread.is_alive() and not runtime.current["cancellation"].event.is_set()
        assert runtime.playback_meter.level() == 0
        assert turn["chunks"][0]["audio"]["path"] and not turn["chunks"][0]["played"]
        objects, epoch, calls = session.world.model_dump()["objects"], session.epoch, voice.calls
        broken = False
        assert runtime.send("retry_voice", turn["id"])
        until(lambda: session.turns[turn["id"]]["stage"] == "complete")
        recovered = session.turns[turn["id"]]
        assert recovered["speech_status"] == "played" and not recovered["speech_error"]
        assert recovered["chunks"][0]["played"] and voice.calls == calls
        assert session.epoch == epoch and session.world.model_dump()["objects"] == objects
        assert not session.status["speech_problem"]
    finally:
        runtime.stop()
        session.close()


def test_audio_failure_does_not_cancel_the_rest_of_a_streamed_reply(tmp_path):
    continue_reply = threading.Event()

    class SlowReply:
        def stream(self, text, history, cancellation, on_delta, on_sentence, **kwargs):
            on_delta("前半。")
            on_sentence("前半。")
            assert continue_reply.wait(4)
            cancellation.check()
            on_delta("後半も残す。")
            on_sentence("後半も残す。")
            return {"text": "前半。後半も残す。", "finish_reason": "stop"}

    session, runtime, voice = setup(tmp_path, llm=SlowReply(), playback=lambda *args: (_ for _ in ()).throw(OSError("speaker failed")))
    runtime.start()
    try:
        runtime.send("text", "続きを話して")
        until(lambda: bool(session.status.get("speech_problem")))
        assert not runtime.current["cancellation"].event.is_set()
        continue_reply.set()
        until(lambda: session.view()["turns"][-1]["stage"] == "failed")
        turn = session.view()["turns"][-1]
        assert turn["reply"] == "前半。後半も残す。"
        assert len(turn["speech_plan"]) == 2 and voice.calls == 1
        assert turn["metrics"]["first_token_seconds"] is not None
    finally:
        continue_reply.set()
        runtime.stop()
        session.close()


@pytest.mark.parametrize("failure", [503, "memory"])
def test_synthesis_retries_only_a_known_recoverable_failure_and_diagnostics_exclude_text(tmp_path, failure):
    session, runtime, voice = setup(tmp_path, playback=lambda *args: True)
    actual = voice.synthesize
    calls, released = [], []
    request = httpx.Request("POST", "http://127.0.0.1:10101/synthesis?text=PRIVATE_TEXT")
    response = httpx.Response(500 if failure == "memory" else failure,
                              text="CUDA out of memory PRIVATE_BODY" if failure == "memory" else "PRIVATE_BODY", request=request)

    def synthesize(*args):
        calls.append(args)
        if len(calls) == 1:
            raise httpx.HTTPStatusError("request failed PRIVATE_TEXT", request=request, response=response)
        return actual(*args)

    voice.synthesize = synthesize
    runtime.perception.release_gpu_for_speech = lambda stop: released.append(True) or True
    try:
        result = runtime._speak({"turn_id": "test", "index": 0, "text": "本文", "stop": threading.Event()})
        assert result["played"] and len(calls) == 2 and calls[0] == calls[1]
        assert bool(released) == (failure == "memory")
        from esi02.speech import issue
        problem = issue(httpx.HTTPStatusError("PRIVATE_TEXT", request=request, response=response), "synthesis")
        report = runtime_observation({"speech_problem": problem})
        assert "PRIVATE" not in str(report)
        assert report["speech_problem"]["endpoint"] == "/synthesis"
    finally:
        runtime.stop()
        session.close()


def test_gpu_memory_recovery_frees_only_automatic_asr_and_is_lazy_until_the_next_input(tmp_path):
    perception = AcceleratedPerception(Settings(asr_device="auto"), tmp_path)
    unloaded = []
    class Native:
        def unload_model(self):
            unloaded.append(True)
    class Model:
        model = Native()
    perception.model, perception.actual_device = Model(), "cuda"
    assert perception.release_gpu_for_speech(threading.Event())
    assert unloaded and perception.model is None and perception.cpu_for_speech
    assert perception.actual_device is None  # CPU inference has not yet happened.
    required = AcceleratedPerception(Settings(asr_device="cuda"), tmp_path / "required")
    required.model, required.actual_device = Model(), "cuda"
    assert not required.release_gpu_for_speech(threading.Event()) and len(unloaded) == 1


def test_failed_synthesis_is_reported_separately_from_playback(tmp_path):
    session, runtime, voice = setup(tmp_path, playback=lambda *args: pytest.fail("No WAV was synthesized"))
    voice.synthesize = lambda *args: (_ for _ in ()).throw(ValueError("invalid voice result"))
    try:
        with pytest.raises(SpeechFailure) as failure:
            runtime._speak({"turn_id": "test", "index": 0, "text": "本文", "stop": threading.Event()})
        assert failure.value.problem["stage"] == "synthesis" and failure.value.audio is None
    finally:
        runtime.stop()
        session.close()


def test_cpu_memory_failure_cannot_disable_gpu_recognition(tmp_path):
    session, runtime, voice = setup(tmp_path, playback=lambda *args: True)
    request = httpx.Request("POST", "http://127.0.0.1:10101/synthesis?text=GPU")
    response = httpx.Response(500, text="CPU allocator out of memory", request=request)
    voice.synthesize = lambda *args: (_ for _ in ()).throw(httpx.HTTPStatusError(f"Error for {request.url}", request=request, response=response))
    runtime.perception.release_gpu_for_speech = lambda stop: pytest.fail("No GPU allocation failure was reported")
    try:
        with pytest.raises(SpeechFailure) as failure:
            runtime._speak({"turn_id": "test", "index": 0, "text": "本文", "stop": threading.Event()})
        assert failure.value.problem["kind"] == "memory" and not failure.value.problem["gpu_memory"]
    finally:
        runtime.stop()
        session.close()


def test_visible_error_and_audio_retry_button_recover_the_reported_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from esi02 import gui as base_gui
    from esi02.desktop_preferences import DesktopPreferences, write_preferences
    from esi04.gui import Window

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(base_gui, "audio_devices", lambda: [])
    broken = True

    def playback(*args):
        if broken:
            raise OSError("speaker unavailable in regression test")
        return True

    session, runtime, voice = setup(tmp_path, playback=playback)
    write_preferences(tmp_path / "esi04-desktop.json", DesktopPreferences(model_path=None))
    window = Window(session, {"device": "cuda", "verified": True, "inference_verified": True})
    monkeypatch.setattr(window, "new_runtime", lambda *args: runtime)
    window.on_start.setChecked(False)
    window.show()

    def gui_until(condition):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            app.processEvents()
            if condition():
                return
            threading.Event().wait(.02)
        raise AssertionError("GUI did not reflect speech recovery")

    try:
        window.toggle(microphone=False)
        window.text.setText("聴いてるよ")
        window.submit_text()
        gui_until(lambda: bool(session.view()["turns"]) and session.view()["turns"][-1]["stage"] == "failed")
        window.refresh()
        assert "スピーカーへの再生に失敗" in window.conversation.toPlainText()
        assert "speaker unavailable" in window.error.text()
        assert window.error.isVisible() and window.error.y() < window.meter.y()
        assert window.repeat_voice_button.isEnabled()
        assert "GPU推論を確認" in window.hardware.text()
        calls = voice.calls
        broken = False
        QTest.mouseClick(window.repeat_voice_button, Qt.LeftButton)
        gui_until(lambda: session.view()["turns"][-1]["stage"] == "complete")
        window.refresh()
        assert not window.error.isVisible() and voice.calls == calls
        assert window.grab().save(str(tmp_path / "recovered-voice.png"))
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
