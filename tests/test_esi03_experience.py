import math

import pytest

from esi02.world import World
from esi03.experience import Experience
from esi03.personality import Character, State, Traits


def advance(experience, world, seconds):
    for _ in range(int(seconds / 30)):
        world.advance(30)
        experience.advance(world, 30)


def test_curiosity_has_a_persistent_reason_and_respects_rest():
    world, experience = World(), Experience()
    advance(experience, world, 180)
    question = experience.question_candidate(world)
    assert question["interest"].origin == "growth"
    assert question["reason"] == "自分の学び方を育てたい"
    world.body.fatigue = 0.85
    assert experience.question_candidate(world) is None


def test_approval_social_and_pleasure_needs_change_from_experience():
    world, experience = World(), Experience()
    advance(experience, world, 900)
    before = experience.state.needs.model_copy()
    experience.user_turn({"id": "thanks", "epoch": 1, "source": "text", "input": "ありがとう"})
    experience.advance(world, 0)
    assert experience.state.needs.social < before.social
    assert experience.state.needs.approval < before.approval
    assert experience.state.needs.pleasure > 0
    assert experience.state.relationship <= experience.state.preferences.max_attachment


def test_outcomes_need_external_evidence_and_are_idempotent():
    experience = Experience()
    values = experience.state.character.values.model_copy(deep=True)
    with pytest.raises(ValueError, match="生成した文章"):
        experience.outcome("self", "question", 1, "model_response", "answer")
    assert experience.outcome("observation", "question", 0.8, "explicit_user_response", "reply")
    assert not experience.outcome("observation", "question", 0.8, "explicit_user_response", "reply")
    assert experience.state.habits["question"].trials == 1
    assert experience.state.character.values == values
    assert experience.state.character.traits.sociability > 0.65
    with pytest.raises(ValueError):
        experience.outcome("nan", "question", math.nan, "explicit_user_response", "reply")


def test_tolerance_satiation_and_recovery_bound_dependency():
    world, experience = World(), Experience()
    for number in range(100):
        experience.outcome(str(number), "conversation", 1, "explicit_user_response", str(number))
    habit = experience.state.habits["conversation"]
    assert habit.strength <= 0.65
    assert habit.tolerance <= 0.8
    assert habit.satiation <= 1
    tolerance, satiation = habit.tolerance, habit.satiation
    advance(experience, world, 600)
    assert habit.tolerance < tolerance
    assert habit.satiation < satiation
    assert experience.state.character.traits.trust <= 0.8


def test_refusal_pauses_specific_question_and_stops_proactivity():
    world, experience = World(), Experience()
    advance(experience, world, 180)
    topic = experience.question_candidate(world)["interest"]
    experience.asked(topic.id, "request")
    experience.user_turn({"id": "stop", "epoch": 1, "source": "text", "input": "話しかけないで"})
    assert not experience.state.preferences.proactive
    assert topic.stage == "paused"
    assert experience.question_candidate(world) is None


def test_character_traits_affect_actual_question_selection():
    world = World()
    curious = Experience()
    cautious = Experience(
        State(character=Character(traits=Traits(curiosity=0.1, sociability=0.1))).model_dump()
    )
    advance(curious, world, 180)
    cautious.advance(world, 0)
    assert curious.question_candidate(world)["score"] > cautious.question_candidate(world)["score"]


def test_recovery_does_not_replay_a_request_or_operation():
    world, experience = World(), Experience()
    advance(experience, world, 180)
    goal = experience.plan_research()
    goal.stage = "running"
    experience.asked(experience.state.interests[0].id, "request")
    recovered = Experience(experience.state.model_dump())
    recovered.recover()
    assert recovered.state.goals[0].stage == "cancelled"
    assert recovered.state.active_interest is None
    assert recovered.state.interests[0].stage == "paused"


def test_uncertain_audio_and_self_output_do_not_change_social_learning():
    experience = Experience()
    for source, uncertain in [("self_output", False), ("microphone", True)]:
        experience.user_turn(
            {
                "id": source,
                "epoch": 1,
                "source": source,
                "input": "ありがとう",
                "recognition_needs_confirmation": uncertain,
            }
        )
    assert experience.state.last_user_epoch == 0
    assert experience.state.habits["conversation"].trials == 0


def test_quoted_praise_is_language_data_not_social_approval():
    experience = Experience()
    experience.user_turn(
        {"id": "example", "epoch": 1, "source": "text", "input": "ありがとうは丁寧な言葉です。"}
    )
    assert experience.state.habits["conversation"].trials == 0
    assert experience.state.approval_balance == 0.45


def test_pleasure_drive_can_choose_play_and_learn_from_world_outcome():
    from esi03.motivation import Brain

    world, experience = World(), Experience()
    experience.state.needs.pleasure = 1
    experience.state.needs.curiosity = 0.05
    brain = Brain("character", world, experience)
    choices = brain.candidates(world, allow_requests=False)
    assert any(choice["kind"] == "explore" for choice in choices)
    brain._begin(world, "explore", False)
    assert brain.active.label == "部屋で気分転換を楽しむ"
    for _ in range(16):
        world.advance(1)
        experience.advance(world, 1)
        brain.tick(world, 1, False, False)
    assert experience.state.habits["play"].trials == 1
