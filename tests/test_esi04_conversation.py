import json

import pytest

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.dialogue import DialogueLLM, ResponsePlan
from esi04.session import Session, inspect_database


def reply(session, text):
    turn = session.create_turn(text)
    chunks = []
    result = DialogueLLM(session.settings).stream(
        text, session.history(turn["id"]), Cancellation(), lambda _: None, chunks.append,
        session.dialogue_context(text),
    )
    assert "".join(chunks) == result["text"]
    session.dialogue_result(turn["id"], result)
    session.update_turn(turn["id"], stage="complete", reply=result["text"])
    return result


def test_reported_conversation_is_not_hijacked_by_body_metrics(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False, natural_dialogue=False))
    try:
        assert "聞いている" in reply(session, "エル・エル、エル・エル、ESI")["text"]
        proposal = reply(session, "音声認識がうまくいっていないみたいです。知識を確かめるにはネットで調べものをするのが良いですよ")
        assert "聞き取り" in proposal["text"] and "提案" in proposal["text"]
        assert "落ち着いて" in reply(session, "仮想の部屋のいごごちはどうですか?")["text"]
        desires = reply(session, "何か欲しいものはある?")
        assert desires["intent"] == "self_desire" and "知らない" not in desires["text"]
        teaching = reply(session, "例えば私は仮想の空間におやつを持っていくことができるよおやつを食べたら幸せな気分になれるよ")
        assert "%" not in teaching["text"] and "疲労" not in teaching["text"]
        assert not any(o.kind == "snack" for o in session.world.objects)  # Description is not placement.
        interest = reply(session, "おやつに興味ある?")
        assert interest["intent"] == "self_interest" and "気になる" in interest["text"]
        assert "覚えているよ" not in interest["text"]
        assert "%" in reply(session, "活動エネルギーと疲労を数値で教えて")["text"]
    finally:
        session.close()


def test_short_answer_is_linked_and_retains_both_sides_of_the_evidence(tmp_path):
    path = tmp_path / "save.db"
    session = Session(path, Settings(playback=False, natural_dialogue=False))
    item = session.experience.interest("光る木", "光る木は夜だけ光るの？", "教わったことを確かめたい", "user", "source:1")
    session.experience.asked(item.id, "action:1")
    result = reply(session, "昼も光るよ")
    assert "答えとして" in result["text"]
    assert session.experience.state.dialogue_questions[-1].stage == "answered"
    assert item.stage == "resolved"
    session.close()
    restored = Session(path)
    try:
        belief = restored.knowledge.entries()[0]
        assert belief["label"] == "光る木" and belief["value"] == "昼も光るよ"
        assert belief["evidence"][0]["question"] == "光る木は夜だけ光るの？"
        assert belief["evidence"][0]["quote"] == "昼も光るよ"
        assert restored.experience.state.dialogue_questions[-1].stage == "answered"
        assert inspect_database(path).release == "ESI-04"
    finally:
        restored.close()


def test_unconfirmed_recognition_cannot_resolve_a_question_or_create_belief(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        item = session.experience.interest("光る木", "光る木は夜だけ光るの？", "確かめたい", "user", "source:1")
        session.experience.asked(item.id, "action:1")
        session.create_turn("昼も光るよ", source="microphone", remember=False)
        assert session.experience.state.dialogue_questions[-1].stage == "pending"
        assert not session.knowledge.entries()
    finally:
        session.close()


def test_no_fixed_question_interval_and_no_repetition_without_new_context(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        state = session.experience.state
        state.availability = "present"
        assert session.experience.question_candidate(session.world)
        first = state.interests[0]
        session.experience.asked(first.id, "action:first")
        session.experience.finish_question("action:first", "unknown", "observation:none")
        assert state.dialogue_questions[-1].stage == "unobserved"
        assert not session.experience.question_candidate(session.world)
        second = session.experience.interest("新しい光", "新しい光はいつ見えたの？", "別の観測から疑問が生まれた", "user", "source:new", 0.9)
        assert session.experience.question_candidate(session.world)["interest"].id == second.id
        assert state.elapsed == 0  # A second decision is possible without waiting 150 seconds.
        session.create_turn("今は忙しい")
        assert not session.experience.question_candidate(session.world)
        session.create_turn("戻ったよ")
        assert session.experience.question_candidate(session.world)
    finally:
        session.close()


def test_failed_state_change_rolls_back_live_state_and_beliefs(tmp_path, monkeypatch):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    before = session.dump()
    real = session._write
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected disk/transaction failure")
    monkeypatch.setattr(session, "_write", fail)
    with pytest.raises(RuntimeError):
        session.create_turn("星砂村は月の国です。")
    assert session.dump() == before
    assert not session.knowledge.entries()
    assert json.loads(session.store.db.execute("SELECT value FROM metadata WHERE key='esi04_live'").fetchone()[0]) == before
    session.store.close()


def test_model_self_intent_cannot_redirect_the_users_story(tmp_path, monkeypatch):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    plan = ResponsePlan(opening="本を読んでいるの？興味があるテーマだったの？", reflection="", refs=[],
                        question="none", interest_id="", intent="self_interest", answer_to="")
    monkeypatch.setattr(DialogueLLM, "_plan", lambda *args: (plan, {"inference_performed": True}))
    try:
        result = reply(session, "私は今日は本を読んでいました")
        assert "本を読んでいました" in result["text"]
        assert "もっと知りたい" not in result["text"] and "疲労" not in result["text"]
        assert result["intent"] == "chat"
    finally:
        session.close()


def test_rejecting_an_ambiguous_answer_does_not_learn_the_wrong_link(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False, natural_dialogue=False))
    try:
        question = session.experience.open_question("月砂って何なの？", "月砂", "説明を知りたい")
        reply(session, "甘いものだよ")
        assert question.stage == "uncertain"
        assert "別の話" in reply(session, "いいえ")["text"]
        assert question.stage == "pending" and question.reply_turn is None
        assert not session.knowledge.entries()
    finally:
        session.close()


def test_an_old_unobserved_question_cannot_capture_an_unrelated_yes(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False, natural_dialogue=False))
    try:
        item = session.experience.interest("光る木", "光る木は夜だけ光るの？", "確かめたい", "user", "source:1")
        session.experience.asked(item.id, "action:1")
        session.experience.finish_question("action:1", "unknown", "timeout:1")
        reply(session, "私は今日は本を読んでいました")
        reply(session, "はい")
        assert session.experience.state.dialogue_questions[-1].stage == "unobserved"
        assert not session.knowledge.entries()
        reply(session, "光る木は昼も光るよ")
        assert session.experience.state.dialogue_questions[-1].stage == "answered"
    finally:
        session.close()


def test_new_topic_does_not_leave_an_unanswered_question_blocking_future_activity(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False, natural_dialogue=False))
    try:
        item = session.experience.interest("光る木", "光る木は夜だけ光るの？", "確かめたい", "user", "source:1")
        session.experience.asked(item.id, "action:1")
        reply(session, "私は今日は本を読んでいました")
        assert session.experience.pending_question() is None
        assert session.experience.state.dialogue_questions[-1].stage == "unobserved"
        assert session.experience.state.availability == "present"
        reply(session, "はい")
        assert not session.knowledge.entries()
        reply(session, "光る木は昼も光るよ")
        assert session.experience.state.dialogue_questions[-1].stage == "answered"
    finally:
        session.close()
