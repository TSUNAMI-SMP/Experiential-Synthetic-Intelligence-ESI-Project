"""A broken block is not an acquired item, and a proposed plan cannot reinforce itself."""

from concurrent.futures import Future
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_esi04_plugins import connected, observation

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.minecraft import action_menu, answer, current_action, decide
from esi04.plugins import EnvironmentObservation, PluginCatalog, PluginState
from esi04.runtime import Runtime
from esi04.session import Session


def frame(seen, **changes):
    actor = seen.actor
    return {"actor_id": actor.id, "dimension": actor.dimension, "tick": seen.tick,
            "inventory": [i.model_dump() for i in seen.inventory], "held": "empty hand", "selected_item": "minecraft:air",
            "food": actor.food, "saturation": actor.saturation, "health": actor.health,
            "x": actor.x, "y": actor.y, "z": actor.z, "yaw": actor.yaw, "pitch": actor.pitch,
            "target_block": "minecraft:stone", "container_id": 0, **changes}


def perform(session, kind="mine", check="inventory_gain", item="minecraft:cobblestone", action_id=None, **changes):
    seen = session.plugins.observation
    menu = action_menu(session.active_manifest(), seen)
    action = menu[action_id] if action_id else next(a for a in menu.values() if a and a.kind == kind)
    session.plugin_attempt(action.model_dump(), {"small_goal": {"purpose": "確かめる", "check": check, "item": item},
                                               "reason": "今の所持品と周囲から試す"})
    before = frame(seen)
    after = {**deepcopy(before), "tick": seen.tick+5, **changes}
    result = {"request_id": action.request_id, "kind": kind, "stage": "completed", "changed": True,
              "detail": "実行完了", "before_observation": seen.id, "evidence": {"before": before, "after": after}}
    value = seen.model_dump()
    value.update(id=seen.id+":after", tick=after["tick"], inventory=after["inventory"], action=result)
    value["actor"].update({key: after_value for key, after_value in after.items() if key in value["actor"]})
    session.plugin_observed(value)
    return result


def test_learning_requires_owned_before_after_and_confirms_inventory_separately(tmp_path):
    path = tmp_path / "life.db"
    session = Session(path, Settings(playback=False))
    connected(session)
    initial = session.experience.state.self_efficacy
    result = perform(session, target_block="minecraft:air")
    assert session.plugins.learning.plan.stage == "unobserved"
    assert "所持品の増加は未確認" in session.plugins.learning.plan.review
    assert session.plugins.learning.trials[0].effects == 1
    assert session.plugins.learning.trials[0].goal_checks == 0
    assert session.experience.state.self_efficacy == initial
    # A repeated observation cannot create a second experience or reward.
    session.plugin_observed(observation(id="repeat", action=result).model_dump())
    assert session.plugins.learning.trials[0].trials == 1
    inventory = [i.model_dump() for i in session.plugins.observation.inventory]+[
        dict(slot=40, name="minecraft:cobblestone", count=1)]  # Offhand items are also owned inventory.
    perform(session, inventory=inventory, target_block="minecraft:air")
    assert session.plugins.learning.plan.stage == "confirmed"
    assert session.experience.state.self_efficacy > initial
    assert session.cognition.emotion == "pleased"
    assert session.experience.state.habits["research"].trials == 1
    session.close()
    restored = Session(path)
    try:
        assert restored.plugins.learning.trials[0].trials == 2
        assert len(restored.plugins.learning.episodes) == 2
        assert not restored.plugins.autonomous and restored.plugins.learning.pending is None
    finally:
        restored.close()


def test_missing_evidence_and_generated_claims_cannot_train_personality(tmp_path):
    with closing(Session(tmp_path / "life.db")) as session:
        connected(session)
        seen = session.plugins.observation
        action = action_menu(session.active_manifest(), seen)["mine:stone"]
        session.plugin_attempt(action.model_dump(), {"small_goal": {"purpose": "成功したと言う", "check": "inventory_gain"}})
        session.plugin_observed(observation(id="no-proof", action={"request_id": action.request_id, "kind": "mine",
            "stage": "completed", "changed": True, "detail": "ダイヤモンドを100個得た"}).model_dump())
        assert not session.plugins.learning.episodes
        assert session.plugins.learning.plan.stage == "unobserved"
        assert session.experience.state.habits["research"].trials == 0
        with pytest.raises(ValueError, match="前後の観察"):
            session.experience.outcome("invented", "research", .8, "game_observation", "invented",
                                       {"id": "invented", "status": "verified", "effect_observed": True})


def test_a_new_poll_id_cannot_confirm_a_blocked_motor_without_a_real_change(tmp_path):
    with closing(Session(tmp_path / "life.db", Settings(playback=False))) as session:
        connected(session)
        perform(session, kind="move", check="observe", item="")
        blocked = session.plugins.learning.episodes[-1]
        assert not blocked["effect_observed"] and not blocked["goal_confirmed"]
        assert session.plugins.learning.plan.stage == "unobserved"
        perform(session, kind="move", check="approach", item="", x=session.plugins.observation.actor.x+1)
        walked = session.plugins.learning.episodes[-1]
        assert walked["effect_observed"] and walked["goal_confirmed"]


def test_map_is_world_scoped_and_only_remembers_visible_places(tmp_path):
    with closing(Session(tmp_path / "life.db")) as session:
        connected(session)
        assert any(p.visited for p in session.plugins.learning.places)
        assert {name for p in session.plugins.learning.places for name in p.names} == {"minecraft:stone"}
        # Looking at a cell does not mean it was visited; a different world cannot use this map.
        far = observation().model_dump()
        far["targets"][0].update(x=10., distance=7.)
        session.plugin_observed(far)
        assert any(not p.visited and p.names == ["minecraft:stone"] for p in session.plugins.learning.places)
        session.plugin_observed(observation(id="other", world_id="other-world", targets=[]).model_dump())
        context = session.environment_context()["learning"]
        assert all(not p["names"] for p in context["past_observed_places_not_current_sight"])
        assert not context["trials_not_universal_rules"]


def test_replan_after_stale_hands_and_storage_cannot_reach_unopened_items():
    seen = observation()
    manifest = PluginCatalog.builtin()
    assert not any(key.startswith(("craft:", "deposit:", "withdraw:")) for key in action_menu(manifest, seen))
    recipe = dict(id="minecraft:oak_planks", output="minecraft:oak_planks", count=4, station="inventory", craftable=True)
    seen = EnvironmentObservation.model_validate({**seen.model_dump(), "recipes": [recipe],
        "container": dict(id=3, kind="chest", target_id="chest", items=[dict(slot=0, name="minecraft:oak_log", count=2)])})
    menu = action_menu(manifest, seen)
    assert {"craft:minecraft:oak_planks", "deposit:10", "withdraw:0", "close_container"} <= set(menu)
    def chooser(settings, system, value, schema, cancellation, tokens):
        return schema(holding="empty hand", action="withdraw:0", reason="見えた中身を取り出す")
    result = decide(Settings(), manifest, PluginState(observation=seen), {}, [], Cancellation(), chooser=chooser)
    assert current_action(result, seen, manifest)
    changed = seen.model_dump()
    changed["container"]["items"][0]["name"] = "minecraft:dirt"
    assert current_action(result, EnvironmentObservation.model_validate(changed), manifest) is None
    def mine(settings, system, value, schema, cancellation, tokens):
        return schema(holding="empty hand", action="mine:stone", reason="今の手持ちで試す")
    result = decide(Settings(), manifest, PluginState(observation=seen), {}, [], Cancellation(), chooser=mine)
    changed = seen.model_dump()
    changed["inventory"][0]["selected"] = True
    assert current_action(result, EnvironmentObservation.model_validate(changed), manifest) is None


def test_repeated_no_effect_requests_reconsideration_without_disabling_life(tmp_path):
    with closing(Session(tmp_path / "life.db")) as session:
        connected(session)
        session.plugin_options(autonomous=True)
        for _ in range(3):
            perform(session, kind="look", check="observe", yaw=30.+session.plugins.observation.tick)
        assert session.plugins.autonomous and session.plugins.learning.consecutive_no_effect == 0
        for _ in range(3):
            perform(session)
        assert session.plugins.autonomous and session.plugins.life.review_required
        assert "考え直す" in session.plugins.learning.plan.review


def test_hand_answer_is_not_inventory_answer():
    environment = {"active": True, "connected": True, "observation": observation().model_dump()}
    assert "手には何も" in answer("手に何を持ってる？", environment)
    assert "手には何も" in answer("何を装備している？", environment)
    assert "minecraft:bread" in answer("所持品は何？", environment)
    environment["learning"] = {"plan": {"stage": "running", "step": {"purpose": "見えた物の使い方を試す"}}}
    assert "見えた物の使い方を試す" in answer("何がしたい？", environment)
    environment.update(connected=False, learning={"recent_observed_changes": [{"detail": "ブロックの変化を確認。取得は未確認。"}]})
    assert "以前のMinecraft" in answer("何を学んだ？", environment)
    assert "現在の状態は分からない" in answer("今、何が見える？", environment)
    environment["active"] = False
    assert "以前のMinecraft" in answer("マイクラで何を学んだ？", environment)
    assert answer("今、何が見える？", environment) is None


def test_opened_storage_memory_is_past_evidence_and_scoped_to_world(tmp_path):
    with closing(Session(tmp_path / "life.db")) as session:
        connected(session)
        seen = observation().model_dump()
        seen["container"] = dict(id=3, kind="chest", target_id="chest", x=3., y=64., z=10.,
                                 items=[dict(slot=0, name="minecraft:oak_planks", count=4)])
        session.plugin_observed(seen)
        session.plugin_observed(observation(id="closed").model_dump())
        assert session.plugins.observation.container is None
        memories = session.environment_context()["learning"]["past_opened_storage_not_current_contents"]
        assert memories[0]["items"] == {"minecraft:oak_planks": 4}
        assert session.plugins.learning.storage[0].source_id == "session:1"
        session.plugin_observed(observation(id="different", world_id="elsewhere").model_dump())
        assert not session.environment_context()["learning"]["past_opened_storage_not_current_contents"]


def test_interrupted_operation_never_resumes_after_restart(tmp_path):
    path = tmp_path / "life.db"
    with closing(Session(path)) as session:
        connected(session)
        session.plugin_attempt(action_menu(session.active_manifest(), session.plugins.observation)["mine:stone"].model_dump())
        assert session.plugins.learning.pending
    with closing(Session(path)) as restored:
        assert not restored.plugins.autonomous and restored.plugins.learning.pending is None
        assert restored.plugins.learning.plan.stage == "unobserved"
        assert "再起動" in restored.plugins.learning.plan.review


def test_http_error_can_still_become_a_source_linked_failed_experience(tmp_path):
    with closing(Session(tmp_path / "life.db", Settings(playback=False))) as session:
        connected(session)
        runtime = Runtime(session)
        try:
            action = action_menu(session.active_manifest(), session.plugins.observation)["mine:stone"]
            session.plugin_attempt(action.model_dump())
            future = Future()
            future.set_exception(RuntimeError("Minecraftで操作に失敗した"))
            runtime.jobs[future] = {"kind": "environment_action", "environment_generation": runtime.environment_generation}
            runtime._finish_jobs()
            assert session.plugins.learning.pending.request_id == action.request_id
            before = frame(session.plugins.observation)
            session.plugin_observed(observation(id="failed", action={"request_id": action.request_id,
                "kind": "mine", "stage": "failed", "changed": False, "before_observation": "session:1",
                "evidence": {"before": before, "after": {**before, "tick": before["tick"]+1}}}).model_dump())
            assert session.plugins.learning.plan.stage == "failed"
            assert session.plugins.learning.trials[0].trials == 1
            assert not session.experience.state.habits["research"].trials
        finally:
            runtime.stop()


def test_selected_tool_intention_prepares_hand_without_choosing_a_tool_for_the_model():
    seen = observation().model_dump()
    seen["inventory"][0]["selected"] = True
    seen["inventory"] += [dict(slot=0, name="minecraft:wooden_pickaxe", count=1, durability=59)]
    seen = EnvironmentObservation.model_validate(seen)
    def chooser(settings, system, value, schema, cancellation, tokens):
        return schema(holding="minecraft:bread", use_item="minecraft:wooden_pickaxe", action="mine:stone",
                      reason="この道具で試す", small_goal={"purpose": "石を拾う", "check": "inventory_gain"})
    result = decide(Settings(), PluginCatalog.builtin(), PluginState(observation=seen), {}, [], Cancellation(), chooser=chooser)
    assert result["requested_choice"] == "mine:stone" and result["choice"] == "equip:0"
    assert result["action"]["kind"] == "select_slot" and result["small_goal"]["check"] == "held"
    assert result["small_goal"]["following"][0] == "石を拾う"
    assert current_action(result, seen, PluginCatalog.builtin())
    # Choosing an unsuitable object remains possible; the motor layer does not substitute expert knowledge.
    def use_bread(settings, system, value, schema, cancellation, tokens):
        return schema(holding="minecraft:bread", use_item="minecraft:bread", action="mine:stone", reason="試したい")
    result = decide(Settings(), PluginCatalog.builtin(), PluginState(observation=seen), {}, [], Cancellation(), chooser=use_bread)
    assert result["choice"] == "mine:stone" and result["action"]["kind"] == "mine"


def test_repeated_floor_blocks_do_not_hide_a_different_visible_object():
    seen = observation().model_dump()
    stone = seen["targets"][0]
    seen["targets"] = [{**stone, "id": f"stone{i}", "distance": 2.+i*.1} for i in range(20)]+[
        {**stone, "id": "workbench", "name": "minecraft:crafting_table", "distance": 6., "reachable": False}]
    seen["targets"][1].update(y=63.5, distance=1.5)  # The sensor ranks useful front blocks before the closer floor.
    captured = {}
    def chooser(settings, system, value, schema, cancellation, tokens):
        captured.update(value)
        return schema(holding="empty hand", action="look_left", reason="周りを確かめる")
    decide(Settings(), PluginCatalog.builtin(), PluginState(observation=EnvironmentObservation.model_validate(seen)),
           {}, [], Cancellation(), chooser=chooser)
    assert {t["name"] for t in captured["targets"]} == {"minecraft:stone", "minecraft:crafting_table"}
    assert sum(t["name"] == "minecraft:stone" for t in captured["targets"]) == 1
    assert next(t["id"] for t in captured["targets"] if t["name"] == "minecraft:stone") == "stone0"


def test_new_goal_drops_old_future_steps_but_keeps_late_experience(tmp_path):
    with closing(Session(tmp_path / "life.db")) as session:
        connected(session)
        action = action_menu(session.active_manifest(), session.plugins.observation)["mine:stone"]
        session.plugin_attempt(action.model_dump(), {"small_goal": {"purpose": "石を掘る", "check": "block_change",
                                                                    "following": ["さらに石を掘る"]}})
        runtime = Runtime(session)
        stops = []
        runtime.environment_client = SimpleNamespace(stop=lambda: stops.append(True), disconnect=lambda: None)
        try:
            runtime._handle("plugin", ("options", {"goal": "今は丸太から板材を作る", "autonomous": True}))
            assert runtime.environment_io == "environment_stop"
            for future in runtime.jobs:
                future.result(timeout=2)
            runtime._finish_jobs()
            assert stops == [True]
            assert not session.plugins.learning.plan.step.following
            assert session.plugins.learning.plan.goal == "今は丸太から板材を作る"
            before = frame(session.plugins.observation)
            session.plugin_observed(observation(id="late", action={"request_id": action.request_id,
                "kind": "mine", "stage": "completed", "changed": True, "before_observation": "session:1",
                "evidence": {"before": before, "after": {**before, "target_block": "minecraft:air"}}}).model_dump())
            assert session.plugins.learning.plan.stage == "paused"
            assert session.plugins.learning.episodes[-1]["small_goal"]["purpose"] == "石を掘る"
        finally:
            runtime.stop()


@pytest.fixture(autouse=True)
def exercise_retained_minecraft_code(monkeypatch):
    # The released app disables this plugin; keep its preserved code regression-tested.
    monkeypatch.setattr("esi04.plugins.MINECRAFT_ENABLED", True)
