import os
import time
from pathlib import Path

import pytest
import soundfile as sf

from esi02.config import Settings
from esi02.runtime import Runtime
from esi02.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local Qwen/AivisSpeech required"
    ),
]


def until(condition, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("actual local operation did not complete within deadline")


class Memory:
    def extract(self, turn, existing, cancellation):
        cancellation.check()
        return {"items": [], "rejected": []}


class Speech:
    def synthesize(self, text, action):
        return {"path": action + ".wav", "duration": 0.1, "played": False}


def test_actual_rion_request_audio_alone_is_not_a_reward_and_actual_environment_change_is_observed(tmp_path):
    session = Session(tmp_path / "request.sqlite3", Settings(playback=False))
    session.world_operation("environment", {"temperature": 39.0})
    # Controlled virtual-time acceleration only; audio synthesis uses the real Rion model.
    session.advance_world(30, autonomy=True, allow_requests=True)
    identifier = session.brain.active.id
    runtime = Runtime(session, memory_llm=Memory())
    runtime.start()
    try:
        until(lambda: session.brain.state.actions[-1].delivered in {"file_only", "failed"}, 45)
        action = next(a for a in session.brain.state.actions if a.id == identifier)
        assert action.delivered == "file_only", session.status
        audio = action.audio["audio"]
        assert Path(audio["path"]).is_file() and audio["model_name"] == "凛音エル"
        wave, rate = sf.read(audio["path"], dtype="float32")
        assert rate == 16000 and len(wave) > 1600
        assert session.epoch == 0 and not session.turns
        assert session.brain.state.estimates["ask_cool"].count == 0
        runtime.send("environment", {"temperature": 23.0})
        until(lambda: session.brain.state.estimates["ask_cool"].count == 1, 35)
        learned = next(a for a in session.brain.state.actions if a.id == identifier)
        assert learned.reward > 0.2 and learned.evidence["type"] == "environment_controls"
    finally:
        runtime.stop()
        session.close()
    reopened = Session(tmp_path / "request.sqlite3")
    try:
        assert reopened.brain.state.estimates["ask_cool"].count == 1
        assert (
            next(a for a in reopened.brain.state.actions if a.id == identifier).audio["audio"]["model_name"]
            == "凛音エル"
        )
    finally:
        reopened.close()


def test_actual_memory_stream_yields_to_new_dialogue_while_virtual_body_continues(tmp_path):
    session = Session(tmp_path / "priority.sqlite3", Settings(playback=False, reply_tokens=96))
    runtime = Runtime(session, tts=Speech())  # real Qwen for both conversation and memory extraction
    runtime.start()
    try:
        runtime.send("text", "私の名前は太郎です。短く挨拶をしてください。")
        until(lambda: session.status.get("memory_stream_started"), 90)
        first = session.view()["turns"][-1]
        assert first["stage"] == "complete" and first["reply"]
        pending_id = session.status["memory_stream_started"]
        old_world = session.world.elapsed
        accepted_at = time.monotonic()
        runtime.send("text", "前の話はやめます。2足す3はいくつですか？短く答えてください。")
        until(
            lambda: (
                len(session.turns) == 2 and session.view()["turns"][-1]["stage"] in {"complete", "failed"}
            ),
            65,
        )
        turn = session.view()["turns"][-1]
        assert turn["stage"] == "complete", turn["error"]
        assert "5" in turn["reply"] or "五" in turn["reply"]
        assert time.monotonic() - accepted_at < 65
        assert session.world.elapsed > old_world and session.epoch == 2
        # The interrupted extraction remains eligible, without a false completion or a failure label.
        extraction = [
            r for r in session.store.records("memory_extraction") if r["payload"]["turn_id"] == pending_id
        ]
        assert not extraction
        assert any(m["id"] == pending_id and m["attempts"] == 0 for m in session.memory.pending())
        print("Actual priority reply:", turn["reply"])
    finally:
        runtime.stop()
        session.close()
