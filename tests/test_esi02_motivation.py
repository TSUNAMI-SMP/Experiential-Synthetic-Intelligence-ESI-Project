import copy
import math
import threading
import time

import pytest

from esi02.config import Settings
from esi02.motivation import REQUESTS, Brain, Estimate, State
from esi02.runtime import Runtime
from esi02.session import Session
from esi02.world import World


def step(world, brain, seconds, act=True, requests=True):
    events = []
    while seconds > 1e-8:
        dt = min(0.5, seconds)
        world.advance(dt)
        events.extend(brain.tick(world, dt, act, requests))
        seconds -= dt
    return events


def hot_request():
    world = World()
    world.change_environment({"temperature": 39.0})
    brain = Brain("test", world)
    step(world, brain, 30)
    assert brain.active.kind == "ask_cool"
    return world, brain


def until(condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class Replies:
    def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
        cancellation.check()
        assert context["motivation"]["simulation_only"]
        on_delta("わかりました。")
        on_sentence("わかりました。")
        return {"text": "わかりました。", "finish_reason": "stop"}


class Memory:
    def extract(self, turn, existing, cancellation):
        cancellation.check()
        return {"items": [], "rejected": []}


class Speech:
    def __init__(self):
        self.texts = []

    def synthesize(self, text, action):
        self.texts.append(text)
        return {"path": action + ".wav", "duration": 0.1, "played": False}


def test_needs_follow_body_and_modulators_are_bounded_without_fictitious_social_contact():
    world = World()
    brain = Brain("test", world)
    assert not brain.state.has_contact
    step(world, brain, 30, act=False)
    assert brain.state.modulators.oxytocin < 0.2
    world.body.fatigue, world.body.energy, world.body.hydration = 0.8, 0.2, 0.3
    world.change_environment({"temperature": 40.0, "noise_db": 90.0})
    step(world, brain, 30, act=False)
    assert brain.state.needs.maintenance >= 0.8
    assert brain.state.needs.rest >= 0.8
    assert brain.state.needs.comfort > 0.7
    assert brain.state.modulators.noradrenaline > 0.6
    before = brain.state.modulators.oxytocin
    brain.user_turn(world, {"id": "real", "epoch": 1, "input": "こんにちは", "source": "text"})
    step(world, brain, 20, act=False)
    assert brain.state.modulators.oxytocin > before
    assert all(0 <= v <= 1 for v in brain.state.modulators.model_dump().values())


def test_rest_is_chosen_for_fatigue_and_learns_only_after_observed_recovery():
    world = World()
    world.body.fatigue, world.body.energy = 0.8, 0.4
    brain = Brain("test", world)
    brain.state.next_decision = 0
    brain.tick(world, 0, True, True)
    assert brain.active.kind == "rest" and world.body.activity == "moving"
    before = brain.active.before
    assert brain.state.estimates["rest"].count == 0
    events = step(world, brain, 30, act=False)
    outcome = next(e["payload"]["action"] for e in events if e["kind"] == "motivation_outcome")
    assert outcome["learned"] and outcome["reward"] > 0
    assert world.body.fatigue < before.fatigue and world.body.energy > before.energy
    assert brain.state.estimates["rest"].count == 1
    assert outcome["contributions"]["fatigue"] > 0
    step(world, brain, 3, act=False)
    assert brain.state.modulators.dopamine > 0.25


def test_critical_maintenance_overrides_exploration_and_requests():
    world = World()
    world.body.hydration, world.body.fatigue = 0.1, 0.85
    world.change_environment({"temperature": 39.0})
    brain = Brain("test", world)
    brain.state.next_decision = 0
    brain.tick(world, 0, True, True)
    assert brain.active.kind == "drink"


def test_drink_arrival_is_required_and_full_hydration_cannot_farm_reward(tmp_path):
    world = World()
    world.body.hydration = 0.20
    brain = Brain("test", world)
    brain.state.next_decision = 0
    brain.tick(world, 0, True, True)
    assert brain.active.kind == "drink" and not world.near("water")
    assert brain.state.estimates["drink"].count == 0
    step(world, brain, 10, act=False)
    assert world.near("water") and brain.state.estimates["drink"].count == 1
    assert brain.state.actions[-1].evidence["hydration_added"] == pytest.approx(0.3)
    for _ in range(6):
        world.drink()
    assert world.body.hydration == 1
    brain.state.estimates["drink"].weights = [1.0] + [0.0] * 9
    assert not any(c["kind"] == "drink" for c in brain.candidates(world))
    step(world, brain, 60, act=True, requests=False)
    assert brain.state.estimates["drink"].count == 1


def test_unanswered_request_never_trains_zero_and_same_condition_does_not_repeat():
    world, brain = hot_request()
    identifier = brain.active.id
    for stage in ("synthesizing", "playing", "played", "played"):
        brain.delivery(identifier, stage)
    assert brain.state.estimates["ask_cool"].count == 0
    step(world, brain, 120, act=False)
    action = brain.state.actions[-1]
    assert action.id == identifier and action.stage == "unknown"
    assert action.reward is None and not action.learned
    step(world, brain, 600, act=True, requests=True)
    assert len([a for a in brain.state.actions if a.kind == "ask_cool"]) == 1
    assert brain.state.estimates["ask_cool"].count == 0


def test_agreement_and_assistant_words_do_not_prove_environment_changed():
    world, brain = hot_request()
    identifier = brain.active.id
    brain.respond(world, identifier, "accept")
    brain.user_turn(world, {"id": "self", "epoch": 1, "input": "はい", "source": "self_output"})
    step(world, brain, 30, act=False)
    assert brain.active.kind == "ask_cool" and brain.state.estimates["ask_cool"].count == 0
    assert world.room.temperature == 39
    events = brain.intervention(world, "environment", {"temperature": 23.0})
    assert not events
    world.change_environment({"temperature": 23.0})
    step(world, brain, 0.5, act=False)
    action = brain.state.actions[-1]
    assert action.learned and action.reward > 0.2
    assert action.evidence["type"] == "environment_controls"
    assert brain.state.estimates["ask_cool"].count == 1
    with pytest.raises(ValueError, match="終了"):
        brain.respond(world, identifier, "accept")
    assert brain.state.estimates["ask_cool"].count == 1


def test_decline_is_observed_negative_feedback_and_holds_off_even_if_conditions_worsen():
    world, brain = hot_request()
    identifier = brain.active.id
    brain.respond(world, identifier, "decline")
    assert brain.state.actions[-1].reward < 0 and brain.state.actions[-1].learned
    assert brain.state.cooldowns["ask_cool"] == world.elapsed + 900
    world.change_environment({"temperature": 45.0})
    step(world, brain, 200, act=True, requests=True)
    assert len([a for a in brain.state.actions if a.kind == "ask_cool"]) == 1
    assert brain.state.estimates["ask_cool"].count == 1


def test_social_request_acceptance_is_counted_once_and_self_output_cannot_satisfy_it():
    world = World()
    brain = Brain("test", world)
    step(world, brain, 200, act=False)
    brain.state.next_decision = 0
    brain.state.cooldowns["explore"] = 1000
    brain.tick(world, 0, True, True)
    assert brain.active.kind == "ask_talk"
    turn = {"id": "user", "epoch": 1, "input": "はい", "source": "self_output"}
    brain.user_turn(world, turn)
    assert brain.active is not None and not brain.state.has_contact
    turn["source"] = "text"
    brain.user_turn(world, turn)
    assert brain.active is None and brain.state.estimates["ask_talk"].count == 1
    before = brain.state.model_dump()
    brain.user_turn(world, turn)
    assert brain.state.model_dump() == before


@pytest.mark.parametrize("text", ["お願いはやめて。", "今は話しかけないで。", "ごめんね、やめてください。"])
def test_general_please_stop_response_holds_all_request_kinds(text):
    world, brain = hot_request()
    brain.user_turn(world, {"id": "user", "epoch": 1, "input": text, "source": "text"})
    assert all(brain.state.cooldowns[kind] >= world.elapsed + 900 for kind in REQUESTS)
    assert brain.active is None


def test_same_condition_can_be_requested_again_only_after_resolution_and_new_discomfort():
    world, brain = hot_request()
    brain.intervention(world, "environment", {"temperature": 23.0})
    world.change_environment({"temperature": 23.0})
    step(world, brain, 15, act=False)
    assert "ask_cool" not in brain.state.request_latches
    step(world, brain, 300, act=False)
    world.change_environment({"temperature": 39.0})
    brain.tick(world, 0, True, True)
    assert brain.active.kind == "ask_cool"
    assert len([a for a in brain.state.actions if a.kind == "ask_cool"]) == 2


def test_learning_rejects_imagined_llm_and_self_output_labels_and_nonfinite_values():
    estimate = Estimate()
    features = [1.0] + [0.0] * 9
    for provenance in ("llm", "imagined", "self_output"):
        with pytest.raises(ValueError):
            estimate.learn(features, 0.9, provenance)
    for reward in (math.nan, math.inf, 2, True):
        with pytest.raises(ValueError):
            estimate.learn(features, reward, "virtual_world_observation")
    assert estimate.count == 0
    for _ in range(20):
        estimate.learn(features, 0.2, "virtual_world_observation")
    assert estimate.predict(features) == pytest.approx(0.2, abs=0.02)
    for _ in range(40):
        estimate.learn(features, -0.3, "virtual_world_observation")
    assert estimate.predict(features) < 0


def test_invalid_control_never_cancels_active_request_or_awards_an_unprovided_reward(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    session.world_operation("environment", {"temperature": 39.0})
    session.advance_world(30, autonomy=True, allow_requests=True)
    before = session.brain.state.model_dump()
    try:
        with pytest.raises(ValueError):
            session.world_operation("environment", {"temperature": math.nan})
        assert session.brain.state.model_dump() == before
        with pytest.raises(ValueError):
            session.motivation_operation(
                "response", {"id": session.brain.active.id, "answer": "accept", "reward": 1.0}
            )
        assert session.brain.state.model_dump() == before
    finally:
        session.close()


def test_user_intervention_and_change_in_conversation_load_are_not_misattributed(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    session.world.body.fatigue = 0.8
    session.brain.state.next_decision = 0
    session.advance_world(0.5, autonomy=True)
    assert session.brain.active.kind == "rest"
    session.world_operation("place", "desk")
    try:
        assert session.brain.active is None
        assert session.brain.state.actions[-1].stage == "cancelled"
        assert session.brain.state.actions[-1].reward is None
        assert session.brain.state.estimates["rest"].count == 0
    finally:
        session.close()
    world = World()
    world.body.fatigue = 0.8
    brain = Brain("test", world)
    brain.state.next_decision = 0
    brain.tick(world, 0, True, False)
    world.conversation_active = True
    step(world, brain, 30, act=False)
    assert brain.state.actions[-1].stage == "unknown"
    assert brain.state.estimates["rest"].count == 0


def test_request_recovery_never_revoices_old_request_and_does_not_learn_unknown(tmp_path):
    path = tmp_path / "s.sqlite3"
    session = Session(path)
    session.world_operation("environment", {"temperature": 39.0})
    session.advance_world(30, autonomy=True, allow_requests=True)
    identifier = session.brain.active.id
    session.store.close()  # crash: replay both world operation and autonomous action
    resumed = Session(path)
    try:
        assert resumed.brain.active is None
        action = resumed.brain.state.actions[-1]
        assert action.id == identifier and action.stage == "cancelled"
        assert action.reward is None and resumed.brain.state.estimates["ask_cool"].count == 0
        assert resumed.epoch == 0 and not resumed.turns
    finally:
        resumed.close()


def test_body_action_and_estimate_recover_exactly_after_uncheckpointed_crash(tmp_path):
    path = tmp_path / "s.sqlite3"
    session = Session(path)
    session.world.body.fatigue, session.world.body.energy = 0.8, 0.4
    session.brain.state.next_decision = 0
    session.checkpoint()
    session.advance_world(0.5, autonomy=True)
    for _ in range(60):
        session.advance_world(0.5, autonomy=False)
    before, world = session.brain.state.model_dump(), session.world.model_dump()
    session.store.close()
    resumed = Session(path)
    try:
        assert resumed.brain.state.model_dump() == before
        assert resumed.world.model_dump() == world
        assert resumed.brain.state.estimates["rest"].count == 1
    finally:
        resumed.close()


def test_long_simulation_is_bounded_and_cannot_generate_unlimited_requests():
    world, brain = hot_request()
    step(world, brain, 3600, act=True, requests=True)
    assert len(brain.state.actions) <= 64 and len(brain.state.visits) <= 30
    assert len([a for a in brain.state.actions if a.kind == "ask_cool"]) <= 1
    assert brain.state.estimates["ask_cool"].count == 0
    assert all(0 <= v <= 1 for v in brain.state.needs.model_dump().values())
    State.model_validate(brain.state.model_dump())


def test_spontaneous_request_is_interrupted_by_user_input_without_becoming_a_user_turn(tmp_path):
    session = Session(tmp_path / "s.sqlite3", Settings(tick_seconds=0.01))
    session.world_operation("environment", {"temperature": 39.0})
    session.advance_world(30, autonomy=True, allow_requests=True)
    request_id = session.brain.active.id
    playing, stopped = threading.Event(), threading.Event()
    plays = []

    def player(path, stop, device, routing):
        plays.append(path)
        if len(plays) == 1:
            playing.set()
            assert stop.wait(3)
            stopped.set()
            return False
        return True

    runtime = Runtime(session, llm=Replies(), tts=Speech(), memory_llm=Memory(), playback=player)
    runtime.start()
    try:
        assert playing.wait(2)
        assert session.epoch == 0 and not session.turns
        runtime.send("text", "こんにちは")
        assert stopped.wait(2)
        until(lambda: session.view()["turns"] and session.view()["turns"][-1]["stage"] == "complete")
        assert session.epoch == 1
        request = next(a for a in session.brain.state.actions if a.id == request_id)
        assert request.delivered == "interrupted"
        assert session.brain.state.estimates["ask_cool"].count == 0
        assert session.view()["turns"][-1]["reply"] == "わかりました。"
        runtime.stop()
        state = copy.deepcopy(session.brain.state.model_dump())
        time.sleep(0.05)
        assert session.brain.state.model_dump() == state
    finally:
        if not runtime.halt.is_set():
            runtime.stop()
        session.close()


def test_autonomy_off_retains_basic_conversation_and_requests_do_not_cancel_dialogue(tmp_path):
    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    session.motivation_operation("enabled", False)
    runtime = Runtime(session, llm=Replies(), tts=Speech(), memory_llm=Memory())
    runtime.start()
    try:
        runtime.send("text", "こんにちは")
        until(lambda: session.view()["turns"] and session.view()["turns"][-1]["stage"] == "complete")
        assert not session.brain.state.enabled and session.view()["turns"][-1]["reply"]
        assert not session.brain.state.actions
    finally:
        runtime.stop()
        session.close()


def test_adjustment_values_affect_action_priority_without_bypassing_body_constraints():
    world = World()
    world.body.fatigue = 0.5
    brain = Brain("test", world)
    brain.refresh(world, 0)
    low = next(c["score"] for c in brain.candidates(world) if c["kind"] == "rest")
    brain.state.modulators.serotonin, brain.state.modulators.noradrenaline = 0.0, 1.0
    high = next(c["score"] for c in brain.candidates(world) if c["kind"] == "rest")
    assert high > low
    world.body.fatigue = 0.1
    brain.state.needs.curiosity = 1.0
    brain.state.modulators.dopamine = 0.0
    low = next(c["score"] for c in brain.candidates(world) if c["kind"] == "explore")
    brain.state.modulators.dopamine = 1.0
    high = next(c["score"] for c in brain.candidates(world) if c["kind"] == "explore")
    assert high > low
    world.body.energy = 0.2
    assert not any(c["kind"] == "explore" for c in brain.candidates(world))


def test_recent_request_and_user_decline_are_available_to_basic_dialogue_context(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    try:
        session.world_operation("environment", {"temperature": 39.0})
        session.advance_world(30, autonomy=True, allow_requests=True)
        original = session.brain.active.text
        session.create_turn("今は無理です。", "text")
        context = session.dialogue_context("今は無理です。")
        recent = context["motivation"]["recent_requests"]
        assert recent[-1]["request"] == original and recent[-1]["stage"] == "declined"
        assert recent[-1]["delivery"] == "not_sent"
        assert not context["motivation"].get("active_action")
    finally:
        session.close()
