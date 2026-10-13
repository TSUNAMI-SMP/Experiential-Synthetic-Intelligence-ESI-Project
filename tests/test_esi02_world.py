import copy
import math
import threading
import time

import pytest
from pydantic import ValidationError

from esi02.config import Settings
from esi02.runtime import Runtime
from esi02.session import Session
from esi02.world import PLACES, Point, World


def advance(world, seconds):
    while seconds > 0:
        step = min(60, seconds)
        world.advance(step)
        seconds -= step


def test_stimuli_change_with_time_and_position_and_body_cost_is_observed():
    world = World()
    start = world.observe()
    energy, hydration = world.body.energy, world.body.hydration
    advance(world, 60)
    assert world.observe().temperature != start.temperature
    assert world.observe().light_lux != start.light_lux
    assert world.observe().noise_db != start.noise_db
    assert world.body.energy < energy and world.body.hydration < hydration
    assert world.body.fatigue > 0.1
    world.go_to("window")
    assert world.destination is not None and world.body.activity == "moving"
    before = world.position.model_copy()
    world.advance(0.5)
    assert 0 < before.distance(world.position) <= 0.35 + 1e-9
    world.advance(10)
    assert world.position == Point.model_validate(PLACES["window"]["position"])
    assert world.destination is None and world.distance_travelled > 0


def test_rest_requires_travel_rest_recovers_and_water_requires_actual_location():
    world = World()
    world.body.energy, world.body.fatigue, world.body.hydration = 0.3, 0.7, 0.2
    with pytest.raises(ValueError, match="水飲み場"):
        world.drink()
    assert world.body.hydration == 0.2
    world.set_activity("resting")
    assert world.body.activity == "moving"
    world.advance(10)
    assert world.body.activity == "resting" and world.observe().near_rest
    energy, fatigue = world.body.energy, world.body.fatigue
    advance(world, 300)
    assert world.body.energy > energy and world.body.fatigue < fatigue
    world.go_to("water")
    world.advance(10)
    before = world.body.hydration
    world.drink()
    assert world.body.hydration == pytest.approx(before + 0.3)
    for _ in range(10):
        world.drink()
    assert world.body.hydration == 1 and world.last_intervention["hydration_added"] == 0


def test_extreme_environment_causes_strain_and_values_remain_bounded():
    comfortable, strained = World(), World()
    strained.change_environment({"temperature": 43, "noise_db": 90, "lamp": False})
    advance(comfortable, 3600)
    advance(strained, 3600)
    assert strained.body.energy < comfortable.body.energy
    assert strained.body.hydration < comfortable.body.hydration
    assert strained.body.condition < comfortable.body.condition
    assert strained.observe().thermal_discomfort > 0.5
    assert strained.observe().noise_discomfort > 0.5
    assert 30 <= strained.body.temperature <= 43
    for key in ("energy", "fatigue", "hydration", "condition"):
        assert 0 <= getattr(strained.body, key) <= 1


def test_timestep_is_independent_of_display_frame_rate():
    left, right = World(), World()
    for world in (left, right):
        world.go_to("rest")
        world.change_environment({"temperature": 30, "noise_db": 60})
    left.advance(30)
    for _ in range(60):
        right.advance(0.5)
    assert left.model_dump() == right.model_dump()


@pytest.mark.parametrize("seconds", [-1, math.nan, math.inf, 61, "1"])
def test_invalid_timestep_does_not_mutate_world(seconds):
    world = World()
    before = world.model_dump()
    with pytest.raises(ValueError):
        world.advance(seconds)
    assert world.model_dump() == before


def test_invalid_environment_or_outside_move_never_changes_body():
    world = World()
    before = world.model_dump()
    for value in (
        {"temperature": math.nan},
        {"temperature": 70},
        {"noise_db": 120},
        {"lamp": "false"},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            world.change_environment(value)
        assert world.model_dump() == before
    for point in ({"x": 7, "z": 1}, {"x": 1, "y": 1, "z": 1}, {"x": math.nan, "z": 1}):
        with pytest.raises(ValueError):
            world.move_to(point)
        assert world.model_dump() == before
    with pytest.raises(ValueError, match="休む場所"):
        world.move_to({"x": 5, "z": 4}, after="resting")
    assert world.model_dump() == before


def test_world_replays_uncheckpointed_changes_exactly_and_remains_paused_while_closed(tmp_path):
    path = tmp_path / "s.sqlite3"
    session = Session(path)
    session.world_operation("environment", {"temperature": 35, "noise_db": 65})
    session.world_operation("place", "water")
    session.advance_world(10)
    session.world_operation("drink")
    session.advance_world(30)
    before = session.world.model_dump()
    session.store.close()
    restored = Session(path)
    try:
        assert restored.world.model_dump() == before
        assert restored.epoch == 0 and not restored.turns
        restored.close()
        restored = Session(path)
        assert restored.world.model_dump() == before
    finally:
        restored.close()


def test_world_model_refuses_corrupt_body():
    body = World().model_dump()
    body["body"]["energy"] = 1.5
    with pytest.raises(ValidationError):
        World.model_validate(body)


def test_runtime_body_ticks_and_environment_changes_do_not_cancel_slow_conversation(tmp_path):
    began, finish = threading.Event(), threading.Event()

    class Reply:
        def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
            assert context["virtual_world"]["simulation_only"]
            began.set()
            assert finish.wait(3)
            cancellation.check()
            on_delta("仮想の部屋で話しています。")
            on_sentence("仮想の部屋で話しています。")
            return {"text": "仮想の部屋で話しています。", "finish_reason": "stop"}

    class Speech:
        def synthesize(self, text, action):
            return {"path": "unused.wav", "duration": 1, "played": False}

    class Memory:
        def extract(self, turn, existing, cancellation):
            cancellation.check()
            return {"items": [], "rejected": []}

    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    runtime = Runtime(session, llm=Reply(), tts=Speech(), memory_llm=Memory())
    runtime.start()
    try:
        runtime.send("text", "会話を続けながら部屋を変えてください")
        assert began.wait(1)
        runtime.send("environment", {"temperature": 28})
        runtime.send("world", ("place", "rest"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and session.world.elapsed < 0.5:
            time.sleep(0.01)
        assert session.world.elapsed >= 0.5
        assert session.world.room.temperature == 28 and session.world.destination is not None
        assert session.epoch == 1 and session.view()["turns"][-1]["stage"] != "cancelled"
        finish.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and session.view()["turns"][-1]["stage"] != "complete":
            time.sleep(0.01)
        assert session.view()["turns"][-1]["stage"] == "complete"
    finally:
        finish.set()
        runtime.stop()
        after = copy.deepcopy(session.world.model_dump())
        time.sleep(0.03)
        assert session.world.model_dump() == after
        session.close()
