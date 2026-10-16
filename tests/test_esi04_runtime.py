"""Owner integration: observations arrive before answers and revoked work cannot return later."""

import threading
import time
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from esi.models import uid, utc
from esi04.computer import ComputerHub, ComputerMemory
from esi04.computer_models import Foreground, Rect
from esi04.config import Settings
from esi04.runtime import Runtime, requested_skill
from esi04.session import Session
from esi04.vision import Observation, VisualDescription


def wait_for(predicate, timeout=4):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("ESI-04 owner did not reach the expected state")


def test_successful_recognition_clears_its_old_error_but_keeps_unrelated_errors(tmp_path):
    session = Session(tmp_path / "save.db", Settings(idle_deliberation=False))
    runtime = Runtime(session)
    try:
        runtime._status("asr", "failed: GPU decode failed")
        assert session.status["error"] == "asr：GPU decode failed"
        runtime._status("asr", "transcribing")
        runtime._status("asr", "ready: microphone interval recognized")
        assert not session.status["error"]
        runtime._status("asr", "failed: another decode failure")
        runtime._status("error", "voice：speaker disconnected")
        runtime._status("asr", "ready: microphone interval recognized")
        assert session.status["error"] == "voice：speaker disconnected"
        assert not session.status["audio_errors"]
        assert session.store.db.execute("SELECT COUNT(*) FROM records WHERE kind='audio_processing_error'").fetchone()[0] == 2
    finally:
        runtime.stop()
        session.close()


class NoMemoryModel:
    def extract(self, *args):
        return {"items": [], "rejected": []}


class FileOnlyVoice:
    def synthesize(self, text, identifier):
        return {"path": "fixture.wav", "duration": 0.01, "played": False, "model_name": "test fixture"}


class Screens:
    def __init__(self):
        self.vision_backend = {"device": "fixture", "inference_verified": True}
        self.started = threading.Event()
        self.release = None

    def observe(self, settings, frame, question, cancellation):
        self.started.set()
        if self.release:
            self.release.wait(3)
        return Observation(id=uid(), frame_id=frame.id, frame_sha256=frame.sha256,
                           observed_at=utc(), question=question,
                           result=VisualDescription(description="数字42とContinueボタンが見える。",
                                                    visible_text=["42", "Continue"], uncertain=True))


def owner(tmp_path):
    native = SimpleNamespace(foreground=lambda: Foreground(handle=1, application="fixture"))
    hub = ComputerHub(ComputerMemory(tmp_path / "screen"), native)
    hub.configure("screen", observe=True, control=True)
    stream = BytesIO()
    Image.new("RGB", (80, 60), "navy").save(stream, format="PNG")
    frame = hub.memory.save_frame(stream.getvalue(), sequence=1, generation=hub.grant.generation,
        captured_at=time.monotonic(), observed_at=utc(), screen="screen", image_width=80, image_height=60,
        physical=Rect(x=0, y=0, width=80, height=60), logical=Rect(x=0, y=0, width=80, height=60),
        foreground=native.foreground())
    hub.publish(frame)
    session = Session(tmp_path / "save.db", Settings(playback=False, natural_dialogue=False, idle_deliberation=False))
    session.character_operation("preferences", {"proactive": False, "exploration": False})
    services = Screens()
    runtime = Runtime(session, computer=hub, services=services, tts=FileOnlyVoice(), memory_llm=NoMemoryModel())
    return session, hub, services, runtime


def test_natural_screen_question_uses_the_current_observation_before_reply(tmp_path):
    session, hub, services, runtime = owner(tmp_path)
    try:
        runtime.start()
        assert runtime.send("text", "この画面に何が見える？")
        wait_for(lambda: bool(session.turns) and list(session.turns.values())[-1]["stage"] == "complete")
        turn = list(session.turns.values())[-1]
        assert services.started.is_set()
        assert "42" in turn["reply"] and "Continue" in turn["reply"]
        assert turn["screen_observation"]["frame_id"] == hub.latest.id
        assert not session.knowledge.entries()  # An inferred caption is not a taught fact.
    finally:
        runtime.stop()
        assert not hub.grant.observe and not hub.grant.control
        session.close()


def test_new_conversation_cancels_old_screen_answer_and_drops_late_result(tmp_path):
    session, hub, services, runtime = owner(tmp_path)
    services.release = threading.Event()
    try:
        runtime.start()
        runtime.send("text", "この画面に何が見える？")
        assert services.started.wait(3)
        runtime.send("text", "こんにちは")
        wait_for(lambda: len(session.turns) == 2)
        services.release.set()
        wait_for(lambda: list(session.turns.values())[-1]["stage"] == "complete" and runtime.computer_task is None)
        turns = list(session.turns.values())
        assert turns[0]["stage"] == "cancelled"
        assert "こんにちは" in turns[1]["reply"] and "42" not in turns[1]["reply"]
        assert session.cognition.last_screen is None
    finally:
        services.release.set()
        runtime.stop()
        session.close()


def test_named_demonstrations_use_the_taught_skill_and_do_not_match_hypotheticals():
    short = SimpleNamespace(id="short", name="メモ")
    full = SimpleNamespace(id="full", name="メモを書く")
    assert requested_skill("覚えた「メモを書く」を再現して", [short, full]) is full
    assert requested_skill("メモを書くのを再現できますか", [full]) is None
    assert requested_skill("例えばメモを書くのをやってみたら", [full]) is None
    assert requested_skill("メモを書くってどういう意味？", [full]) is None


def test_missing_demonstration_does_not_leave_the_owner_stuck(tmp_path):
    import pytest

    session, hub, services, runtime = owner(tmp_path)
    try:
        with pytest.raises(ValueError, match="見つかりません"):
            runtime._start_computer("replay", {"id": "missing"})
        assert runtime.computer_task is None and not runtime.jobs
    finally:
        runtime.stop()
        session.close()


def test_room_commands_and_stories_do_not_become_real_pc_operations(tmp_path):
    session, hub, services, runtime = owner(tmp_path)
    try:
        runtime._new_turn("机の上におやつを置いて")
        runtime._new_turn("おやつを右に動かして")
        assert list(session.turns.values())[-1]["scene_result"]["operation"] == "move"
        assert not session.cognition.pending_computer_task
        runtime._new_turn("私はメモ帳を開いています")
        assert not session.cognition.pending_computer_task
        runtime._new_turn("このおやつは何？")
        assert not list(session.turns.values())[-1].get("screen_query")
        runtime._new_turn("メモ帳を開いて")
        assert session.cognition.pending_computer_task == "メモ帳を開いて"
    finally:
        runtime.stop()
        session.close()


def test_natural_screen_teaching_retains_the_user_quote_and_actual_frame(tmp_path):
    session, hub, services, runtime = owner(tmp_path)
    try:
        text = "このボタンは下書きを保存するボタンです"
        runtime._new_turn(text)
        teaching = session.cognition.screen_teachings[-1]
        assert teaching.user_text == text and teaching.frame_id == hub.latest.id
        assert teaching.basis == "user_explanation_of_screen"
        runtime._new_turn("このボタンは送信するボタンですか？")
        assert len(session.cognition.screen_teachings) == 1
    finally:
        runtime.stop()
        session.close()


def test_restart_discards_interrupted_real_computer_request(tmp_path):
    path = tmp_path / "save.db"
    session = Session(path, Settings(playback=False))
    session.cognition.pending_computer_task = "メモ帳を開いて"
    session.close()
    restored = Session(path)
    try:
        assert restored.cognition.pending_computer_task == ""
    finally:
        restored.close()


def test_spoken_stop_revokes_access_and_cannot_leave_an_autonomous_task(tmp_path):
    session, hub, services, runtime = owner(tmp_path)
    try:
        runtime._new_turn("PCの操作を止めて")
        assert not hub.grant.observe and not hub.grant.control
        assert not session.cognition.pending_computer_task
        assert session.dialogue_context("PCの操作を止めて")["dialogue"]["computer_stopped"]
    finally:
        runtime.stop()
        session.close()


def test_idle_research_reads_a_real_allowed_folder_and_keeps_its_sources(tmp_path):
    from esi03.tools import Policy

    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "source.md").write_text("星砂村は月の国です。", encoding="utf-8")
    session = Session(tmp_path / "save.db", Settings(playback=False, idle_deliberation=False))
    session.research.configure(Policy(read_roots=[str(folder)]).model_dump())
    session.character_operation("preferences", {"exploration": True, "proactive": False})
    session.character_operation("interest", {"topic": "星砂村", "question": "星砂村とは何ですか？"})
    runtime = Runtime(session, tts=FileOnlyVoice(), memory_llm=NoMemoryModel())
    try:
        runtime.start()
        wait_for(lambda: bool(session.knowledge.entries())
                 and any(g.stage == "complete" and g.evidence_ids for g in session.experience.state.goals))
        assert session.knowledge.entries()[0]["value"] == "月の国"
        assert any(entry.stage == "complete" for entry in session.research.entries())
        assert any(g.stage == "complete" and g.evidence_ids for g in session.experience.state.goals)
    finally:
        runtime.stop()
        session.close()


def test_full_owner_links_a_normal_spoken_answer_to_its_generated_question(tmp_path):
    from test_esi04_conversation import completion, model

    from esi04.dialogue import DialogueLLM

    session=Session(tmp_path/'dialogue.db',Settings(playback=False,idle_deliberation=False))
    session.character_operation('preferences',{'proactive':True,'exploration':False,'idle_activity':False})
    session.brain.state.enabled=False
    factory, requests=model(
        completion('囲碁がお好きなのですね。',followup='どの一手が印象的でしたか？'),
        completion('中央の切り込みが印象に残ったのですね。相手の応手を読む場面には面白さがあります。',reply_relation='answered'))
    runtime=Runtime(session,llm=DialogueLLM(session.settings,factory),tts=FileOnlyVoice(),memory_llm=NoMemoryModel())
    try:
        runtime.start()
        runtime.send('text','今日は友人と囲碁をしました')
        wait_for(lambda: len(session.turns)==1 and next(reversed(session.turns.values()))['stage']=='complete')
        question=session.experience.pending_question()
        assert question and question.text=='どの一手が印象的でしたか？'
        runtime.send('text','中央の切り込みです')
        wait_for(lambda: len(session.turns)==2 and next(reversed(session.turns.values()))['stage']=='complete')
        assert question.stage=='answered' and question.reply_quote=='中央の切り込みです'
        turn=next(reversed(session.turns.values()))
        assert '切り込み' in turn['reply'] and turn['chunks']
        assert len(requests)==2 and not session.status.get('error')
    finally:
        runtime.stop()
        session.close()
