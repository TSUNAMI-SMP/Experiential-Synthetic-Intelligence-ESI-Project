import pytest

from esi04.config import Settings
from esi04.session import Session
from esi04.world import World, scene_command


def test_snack_is_consumed_once_with_actual_observed_effect_and_persisted(tmp_path):
    path = tmp_path / "save.db"
    session = Session(path, Settings(playback=False))
    position = {"x": 3.7, "y": 0.0, "z": 2.5}
    placed = session.scene_operation("place", {"kind": "snack", "position": position, "quantity": 2}, "ui:snack")
    identifier = placed["object_id"]
    energy = session.world.body.energy
    session.scene_operation("eat", {"id": identifier}, "eat:1")
    assert session.world.body.energy > energy
    assert session.world.get(identifier).quantity == 1
    assert session.scene_operation("eat", {"id": identifier}, "eat:1") is None
    assert session.world.get(identifier).quantity == 1
    assert session.experience.state.habits["play"].trials == 1
    session.close()
    restored = Session(path)
    try:
        assert restored.world.get(identifier).quantity == 1
        assert restored.cognition.summaries[-1].certainty == "observed"
    finally:
        restored.close()


def test_path_avoids_furniture_and_reaches_relocated_rest(tmp_path):
    world = World()
    world.object_operation("place", {"kind": "desk", "position": {"x": 3.0, "y": 0.0, "z": 1.5}}, "desk:new")
    world.move_to({"x": 3.0, "y": 0.0, "z": 0.5})
    assert all(world.free(p.x, p.z) for p in world.path)
    for _ in range(40):
        world.advance(0.5)
        assert world.free(world.position.x, world.position.z)
    assert world.destination is None and world.position.z == pytest.approx(0.5)
    world.object_operation("move", {"id": "rest", "position": {"x": 1.2, "y": 0.0, "z": 2.5}}, "move:rest")
    world.set_activity("resting")
    world.advance(30)
    assert world.near("rest") and world.body.activity == "resting"


def test_table_contents_move_together_and_failed_placement_does_not_mutate(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        event = session.scene_operation("place", {"kind": "computer", "parent_id": "desk",
                                                  "position": {"x": 4.5, "y": 0.0, "z": 1.0}}, "computer:1")
        session.scene_operation("move", {"id": "desk", "position": {"x": 4.0, "y": 0.0, "z": 0.8}}, "desk:move")
        computer = session.world.get(event["object_id"])
        assert computer.position.x == 4.0 and computer.position.y == 0.75
        before = session.dump()
        with pytest.raises(ValueError):
            session.scene_operation("place", {"kind": "desk", "position": session.world.position.model_dump()}, "bad:1")
        assert session.dump() == before
        assert not scene_command("例えばおやつを置いたら嬉しいよ", session.world)
        assert not scene_command("おやつを置くことができるよ", session.world)
        assert scene_command("机の上におやつを2個置いて", session.world)["values"]["parent_id"] == "desk"
    finally:
        session.close()


def test_autonomous_object_inspection_is_an_observed_activity_not_self_generated_reward(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        placed = session.scene_operation("place", {"kind": "snack", "position": {"x": 3.6, "y": 0.0, "z": 2.5}}, "user:1")
        for _ in range(50):
            session.advance_world(0.5, autonomy=True, allow_requests=False)
        obj = session.world.get(placed["object_id"])
        assert obj.discovered
        assert session.experience.state.habits["explore"].trials > 0
        assert any(s.certainty == "observed" for s in session.cognition.summaries)
    finally:
        session.close()


def test_removed_and_replaced_body_resources_use_actual_room_objects(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        session.scene_operation("remove", {"id": "rest"}, "remove:bed")
        session.scene_operation("remove", {"id": "water"}, "remove:water")
        session.world.body.fatigue = 0.95
        session.world.body.hydration = 0.1
        assert not any(c["kind"] in {"rest", "drink"} for c in session.brain.candidates(session.world, False))
        session.advance_world(1, autonomy=True, allow_requests=False)
        water = session.scene_operation("place", {"kind": "water", "position": {"x": 3.5, "y": 0.0, "z": 2.5}}, "new:water")
        assert session.world.place("water").id == water["object_id"]
        before = session.world.body.hydration
        session.world.drink()
        assert session.world.body.hydration > before
        session.scene_operation("place", {"kind": "bed", "position": {"x": 1.1, "y": 0.0, "z": 1.1}}, "new:bed")
        session.world.set_activity("resting")
        session.advance_world(30)
        assert session.world.near("rest") and session.world.body.activity == "resting"
    finally:
        session.close()


def test_changing_the_scene_does_not_reward_an_interrupted_body_action(tmp_path):
    session = Session(tmp_path / "save.db", Settings(playback=False))
    try:
        session.brain._begin(session.world, "rest", False)
        old = session.brain.active
        session.scene_operation("remove", {"id": "rest"}, "remove:rest")
        assert session.brain.active is None
        assert next(a for a in session.brain.state.actions if a.id == old.id).stage == "cancelled"
        assert session.experience.state.habits["rest"].trials == 0
    finally:
        session.close()
