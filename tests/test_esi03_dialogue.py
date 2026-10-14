import pytest

from esi02.dialogue import Cancellation
from esi03.config import Settings
from esi03.dialogue import DialogueLLM, Plan, discourse
from esi03.session import Session


class PlannedDialogue(DialogueLLM):
    def __init__(self, plan):
        super().__init__(Settings())
        self.plan = plan

    def _plan(self, *args):
        return self.plan, {"inference_performed": True}


def reply(llm, session, text):
    session.create_turn(text)
    spoken = []
    result = llm.stream(
        text, [], Cancellation(), lambda text: None, spoken.append, session.dialogue_context(text)
    )
    assert "".join(spoken) == result["text"]
    return result


def test_unknown_location_cannot_leak_pretrained_knowledge(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        malicious = Plan(
            opening="ニューヨークはアメリカの都市です。",
            reflection="",
            refs=[],
            question="none",
            interest_id="",
        )
        result = reply(PlannedDialogue(malicious), session, "ニューヨークとは何ですか？")
        assert "アメリカ" not in result["text"]
        assert result["unknown_topic"] == "ニューヨーク"
        session.dialogue_result(next(reversed(session.turns)), result)
        assert any(item.topic == "ニューヨーク" for item in session.experience.state.interests)
    finally:
        session.close()


def test_model_cannot_insert_factual_content_or_assistant_sales_pitch(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        plan = Plan(
            opening="散歩は健康によいです。",
            reflection="何かお手伝いできますか？",
            refs=[],
            question="none",
            interest_id="",
        )
        result = reply(PlannedDialogue(plan), session, "今日は散歩した")
        assert "健康" not in result["text"]
        assert "お手伝い" not in result["text"]
        assert set(result["rejected_discourse"]) == {"opening", "reflection"}
    finally:
        session.close()


def test_natural_discourse_weaves_literal_evidence(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        session.create_turn("星砂村は月の国です。")
        identifier = session.knowledge.entries()[0]["id"]
        plan = Plan(
            opening="うん。",
            reflection="私はもっと知りたい。自分でも確かめたい。",
            refs=[identifier],
            question="none",
            interest_id="",
        )
        result = reply(PlannedDialogue(plan), session, "星砂村について教えて")
        assert "月の国" in result["text"]
        assert "私はもっと知りたい" in result["text"]
        assert result["grounding"]["refs"] == [identifier]
        assert not result["output_is_external_evidence"]
    finally:
        session.close()


def test_initial_personality_and_goals_are_actual_saved_state(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        llm = DialogueLLM(Settings(natural_dialogue=False))
        result = reply(llm, session, "あなたの目標は何ですか？")
        assert "自分を大切" in result["text"]
        assert session.experience.state.interests[0].topic in result["text"]
    finally:
        session.close()


def test_followup_interest_obeys_interval_in_ordinary_conversation(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        session.advance_world(30)
        interest = session.experience.state.interests[0]
        plan = Plan(opening="うん。", reflection="", refs=[], question="interest", interest_id=interest.id)
        result = reply(PlannedDialogue(plan), session, "今日は外を歩いてきた")
        assert result["asked_interest"] == interest.id
        session.dialogue_result(next(reversed(session.turns)), result)
        repeated = reply(PlannedDialogue(plan), session, "それから帰ってきた")
        assert repeated["asked_interest"] is None
        assert interest.question not in repeated["text"]
    finally:
        session.close()


def test_self_goal_rest_and_basic_goodbye_do_not_become_external_topics(tmp_path):
    session = Session(tmp_path / "save.db")
    try:
        llm = DialogueLLM(Settings(natural_dialogue=False))
        session.world.body.fatigue = 0.8
        session.experience.advance(session.world, 0)
        assert "休んで" in reply(llm, session, "何がしたい？")["text"]
        assert "おやすみ" in reply(llm, session, "おやすみ")["text"]
    finally:
        session.close()


@pytest.mark.parametrize(
    "text",
    [
        "東京は日本の首都です。",
        "私は何でも知っています。",
        "https://example.com を開きました。",
        "ツールを実行しよう。",
    ],
)
def test_discourse_channel_rejects_ungrounded_assertions(text):
    assert discourse(text) == ""


def test_many_short_sentences_fit_audio_backpressure_without_losing_text():
    settings = Settings(speech_capacity=2)
    llm = DialogueLLM(settings)
    spoken, shown = [], []
    answer = "うん。" * 15
    result = llm._emit_answer(answer, Cancellation(), shown.append, spoken.append)
    assert len(spoken) <= 2
    assert "".join(spoken) == answer
    assert result["text"] == answer
