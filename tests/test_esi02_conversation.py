import json
import threading
import time
from concurrent.futures import Future

import httpx
import pytest

from esi.core import Core
from esi.models import Settings as OldSettings
from esi02.config import Settings
from esi02.dialogue import Cancellation, Cancelled, DialogueLLM, Sentences
from esi02.gpu import GPU, choose_placement, confirmed_backend, llama_command
from esi02.runtime import Runtime
from esi02.session import Session


def until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class Speech:
    def __init__(self):
        self.texts = []

    def synthesize(self, text, action):
        self.texts.append(text)
        return {"path": action + ".wav", "duration": 0.1, "played": False}


class Replies:
    def stream(self, text, history, cancellation, on_delta, on_sentence):
        cancellation.check()
        reply = "こんにちは。お話しできます。"
        on_delta(reply)
        on_sentence(reply)
        return {"text": reply, "finish_reason": "stop", "request": {"history": history}}


def test_normal_conversation_replies_without_an_action_selector_and_survives_environment_ticks(tmp_path):
    session = Session(tmp_path / "esi02.sqlite3", Settings(playback=False, tick_seconds=0.01))
    speech = Speech()
    runtime = Runtime(session, llm=Replies(), tts=speech)
    runtime.start()
    try:
        assert runtime.send("text", "こんにちは。会話できますか？")
        for _ in range(20):
            assert runtime.send("environment", {"temperature": 23})
        until(lambda: session.view()["turns"] and session.view()["turns"][-1]["stage"] == "complete")
        turn = session.view()["turns"][-1]
        assert turn["reply"] == "こんにちは。お話しできます。"
        assert turn["chunks"] and not turn["chunks"][0]["external_evidence"]
        assert speech.texts == [turn["reply"]]
        assert session.epoch == 1
        assert not session.store.records("outcome")
    finally:
        runtime.stop()
        session.close()
    reopened = Session(tmp_path / "esi02.sqlite3")
    try:
        assert reopened.view()["turns"][-1]["reply"] == turn["reply"]
        assert reopened.elapsed > 0
    finally:
        reopened.close()


def test_new_turn_cancels_stale_generation_and_never_voices_old_answer(tmp_path):
    first_started, release = threading.Event(), threading.Event()

    class Blocking:
        calls = 0

        def stream(self, text, history, cancellation, on_delta, on_sentence):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                release.wait(2)
                # Fault injection: an adapter ignores cancellation and produces a stale result.
                on_delta("古い答え。")
                on_sentence("古い答え。")
                return {"text": "古い答え。", "finish_reason": "stop"}
            return Replies().stream(text, history, cancellation, on_delta, on_sentence)

    session = Session(tmp_path / "esi02.sqlite3", Settings(playback=False, tick_seconds=0.01))
    speech = Speech()
    runtime = Runtime(session, llm=Blocking(), tts=speech)
    runtime.start()
    try:
        runtime.send("text", "前の質問")
        assert first_started.wait(1)
        runtime.send("text", "新しい質問")
        until(lambda: len(session.turns) == 2)
        release.set()
        until(lambda: session.view()["turns"][-1]["stage"] == "complete")
        assert session.view()["turns"][0]["stage"] == "cancelled"
        assert "古い答え。" not in speech.texts
        assert speech.texts == ["こんにちは。お話しできます。"]
    finally:
        release.set()
        runtime.stop()
        session.close()


def test_barge_in_cancels_playback_and_stop_checkpoints_without_late_writes(tmp_path):
    playing, cancelled = threading.Event(), threading.Event()
    calls = []

    def player(path, stop, device, routing):
        calls.append(path)
        if len(calls) == 1:
            playing.set()
            assert stop.wait(2)
            cancelled.set()
            return False
        return True

    session = Session(tmp_path / "esi02.sqlite3", Settings(tick_seconds=0.01))
    runtime = Runtime(session, llm=Replies(), tts=Speech(), playback=player)
    runtime.start()
    try:
        runtime.send("text", "質問1")
        assert playing.wait(1)
        runtime.send("text", "質問2")
        assert cancelled.wait(1)
        until(lambda: session.view()["turns"][-1]["stage"] == "complete")
        assert session.view()["turns"][0]["chunks"] == []
        assert session.view()["turns"][1]["chunks"][0]["played"]
        runtime.stop()
        assert not runtime.send("text", "停止後")
        before = session.store.records()
        time.sleep(0.05)
        assert session.store.records() == before
    finally:
        if not runtime.halt.is_set():
            runtime.stop()
        session.close()


def test_llm_failure_is_reported_without_fabricating_a_response(tmp_path):
    class Broken:
        def stream(self, *args):
            raise ConnectionError("server unavailable")

    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    speech = Speech()
    runtime = Runtime(session, llm=Broken(), tts=speech)
    runtime.start()
    try:
        runtime.send("text", "こんにちは")
        until(lambda: session.view()["turns"] and session.view()["turns"][-1]["stage"] == "failed")
        assert session.view()["turns"][-1]["reply"] == ""
        assert not speech.texts
        before = session.elapsed
        until(lambda: session.elapsed > before)
    finally:
        runtime.stop()
        session.close()


def test_old_session_is_rejected_without_writing_and_uncheckpointed_turn_is_recovered(tmp_path):
    old_path = tmp_path / "old.sqlite3"
    old = Core(old_path, OldSettings(llm_mode="off"))
    old.close()
    before = old_path.read_bytes()
    with pytest.raises(ValueError, match="ESI-02"):
        Session(old_path)
    assert old_path.read_bytes() == before
    path = tmp_path / "new.sqlite3"
    first = Session(path)
    turn = first.create_turn("まだ終わっていない質問")
    first.update_turn(turn["id"], stage="generating")
    first.store.close()  # intentional simulated crash: no checkpoint
    resumed = Session(path)
    try:
        assert resumed.view()["turns"][-1]["input"] == turn["input"]
        assert resumed.view()["turns"][-1]["stage"] == "cancelled"
    finally:
        resumed.close()


def test_streaming_adapter_speaks_complete_sentences_and_prunes_history_to_budget():
    sent, deltas, requests = [], [], []
    budget_calls = 0

    def response(request):
        nonlocal budget_calls
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(payload["messages"])})
        if request.url.path == "/tokenize":
            budget_calls += 1
            return httpx.Response(200, json={"tokens": list(range(2100 if budget_calls == 1 else 100))})
        chunks = ["こんにちは", "。", "お話し", "できます。"]
        events = [
            "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
            for chunk in chunks
        ]
        events += ['data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}', "data: [DONE]"]
        return httpx.Response(200, text="\n\n".join(events))

    llm = DialogueLLM(
        Settings(context_tokens=2048),
        lambda **kw: httpx.Client(**kw, transport=httpx.MockTransport(response)),
    )
    result = llm.stream(
        "今の質問",
        [{"role": "user", "content": "古い質問"}, {"role": "assistant", "content": "古い返事"}],
        Cancellation(),
        deltas.append,
        sent.append,
    )
    assert result["text"] == "こんにちは。お話しできます。"
    assert sent == ["こんにちは。", "お話しできます。"]
    final = requests[-1][1]
    assert len(final["messages"]) == 2 and final["stream"]
    assert final["chat_template_kwargs"]["enable_thinking"] is False


def test_stream_cancellation_aborts_before_any_speech_and_closes_transport():
    cancellation = Cancellation()
    closed = []
    cancellation.attach(lambda: closed.append(True))
    cancellation.set()
    assert closed == [True]
    with pytest.raises(Cancelled):
        cancellation.check()


def test_gpu_selection_requires_binary_support_and_memory_and_never_calls_a_plan_verified():
    gpu = GPU(0, "RTX 5060", 8192, 7000, "driver-test")
    assert choose_placement("auto", [gpu], [], 2_400_000_000).device == "cpu"
    placement = choose_placement("auto", [gpu], ["CUDA0"], 2_400_000_000)
    assert placement.device == "CUDA0" and placement.layers == 99
    assert not confirmed_backend("model ready", placement)["verified"]
    assert confirmed_backend("offloaded 37/37 layers to GPU", placement)["device"] == "cuda"
    low = GPU(0, "RTX 5060", 8192, 2000, "driver-test")
    assert choose_placement("auto", [low], ["CUDA0"], 2_400_000_000).device == "cpu"
    with pytest.raises(RuntimeError):
        choose_placement("cuda", [low], ["CUDA0"], 2_400_000_000)
    command = llama_command("llama-server.exe", "model.gguf", Settings(), placement)
    assert command[-2:] == ["--device", "CUDA0"]
    assert "99" in command


def test_stale_worker_failure_cannot_override_current_status(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    runtime = Runtime(session, llm=Replies(), tts=Speech())
    try:
        runtime._new_turn("first")
        old = runtime.current["turn_id"]
        failed = Future()
        failed.set_exception(ConnectionError("old error"))
        runtime.jobs[failed] = {"kind": "reply", "turn_id": old}
        runtime._new_turn("second")
        runtime._finish_jobs()
        assert not session.status.get("error")
        assert session.store.records("stale_worker_error")
    finally:
        runtime.stop()
        session.close()


def test_sentence_chunks_are_bounded_without_dropping_unpunctuated_text():
    splitter = Sentences(limit=20)
    text = "あ" * 65 + "。終わり"
    chunks = splitter.feed(text) + splitter.finish()
    assert "".join(chunks) == text
    assert all(len(s) <= 20 for s in chunks)


def test_echo_matching_uses_capture_time_instead_of_delayed_asr_completion(tmp_path):
    from esi.models import Observation, Source

    session = Session(tmp_path / "s.sqlite3")
    runtime = Runtime(session, llm=Replies(), tts=Speech())
    try:
        runtime.outgoing.append({"text": "こんにちは。お話しできます。", "start": 100.0, "end": 103.0})
        echo = Observation(
            text="こんにちは。お話しできます。",
            source=Source.MICROPHONE,
            mono_start=100.5,
            mono_end=102.5,
            duration=2,
            metadata={"tts_contamination_possible": True},
        )
        runtime._new_turn(echo.text, "microphone", echo)
        assert not session.turns and session.store.records("echo_suppressed")
        interrupt = echo.model_copy(update={"text": "その話はやめてください"})
        runtime._new_turn(interrupt.text, "microphone", interrupt)
        assert session.view()["turns"][-1]["input"] == interrupt.text
        runtime._status("asr", "failed: 音声認識のデータが見つかりません")
        assert "音声認識のデータ" in session.view()["status"]["error"]
    finally:
        runtime.stop()
        session.close()
