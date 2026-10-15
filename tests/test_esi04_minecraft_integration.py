"""Opt-in: actual Fabric world HTTP bridge and the installed local Qwen model, no fake effects."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.minecraft import current_action, decide
from esi04.plugins import Connection, EnvironmentClient, PluginCatalog
from esi04.session import Session

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("ESI_MC_E2E"),
                                                       reason="running Fabric HTTP GameTest required")]


def test_local_model_controls_real_body_tools_and_crafting(tmp_path):
    root = Path(os.environ["ESI_MC_E2E"])
    path = root / "connection.json"
    deadline = time.monotonic()+90
    while not path.exists() and time.monotonic() < deadline:
        threading.Event().wait(.2)
    assert path.exists(), "Fabric test bridge did not start"
    connection = Connection.model_validate_json(path.read_text(encoding="utf-8"))
    settings = Settings(playback=False, llm_url=os.environ.get("ESI_LLAMA_URL", "http://127.0.0.1:8080"),
                        llm_device="cpu", llm_timeout=300, idle_deliberation=False)
    session = Session(tmp_path / "life.db", settings)
    client = EnvironmentClient(PluginCatalog.builtin(), connection)
    actions = []
    craft_only = os.environ.get("ESI_MC_CRAFT_ONLY") == "1"
    expected_food = 20 if craft_only else 6
    try:
        with httpx.Client(base_url=connection.endpoint, trust_env=False, timeout=8) as probe:
            assert probe.get("/v1/state").status_code == 401
            assert probe.get("/v1/state", headers={"Authorization": "Bearer "+connection.token,
                                                   "Origin": "https://example.org"}).status_code == 401
            assert probe.get("/v1/state", headers={"Authorization": "Bearer "+connection.token}).status_code == 403
        session.environment_mode(client.manifest.id)
        seen = client.connect(session.agent_id, "凛音エル")
        session.status.update(plugin_connected=True, plugin_session=seen.session_id)
        deadline = time.monotonic()+15
        while (seen.actor.food != expected_food or not any(i.name == "minecraft:oak_log" for i in seen.inventory)) and time.monotonic() < deadline:
            threading.Event().wait(.2)
            seen = client.observe()
        assert seen.actor.food == expected_food
        session.plugin_observed(seen.model_dump())
        def choose(goal):
            session.plugin_options(goal=goal)
            taught = [{k: entry.get(k) for k in ("label", "value", "source_turn_id", "confidence")}
                      for entry in session.knowledge.context(goal, limit=4, char_limit=700)]
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(decide, settings, client.manifest, session.plugins.model_copy(deep=True),
                                     session.experience.context(), session.plugin_recent(), Cancellation(), taught)
                while not future.done():
                    session.plugin_observed(client.observe().model_dump())
                    threading.Event().wait(.8)
                result = future.result()
            actions.append({k: result[k] for k in ("choice", "requested_choice", "use_item", "reason", "small_goal")})
            (root / "progress.json").write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
            assert result["action"], "Expected a physical action for the explicit test goal"
            latest = client.observe()
            session.plugin_observed(latest.model_dump())
            action = current_action(result, latest, client.manifest)
            assert action, "The chosen target disappeared during inference"
            session.plugin_attempt(action, result)
            client.act(action)
            deadline = time.monotonic()+18
            while time.monotonic() < deadline:
                seen = client.observe()
                session.plugin_observed(seen.model_dump())
                if seen.action.get("stage") != "running":
                    return seen
                threading.Event().wait(.2)
            raise AssertionError("Real action did not finish")
        if not craft_only:
            for _ in range(3):
                seen = choose("空腹なので、持っているパンを食べて満腹度を回復したい。")
                if seen.actor.food > 6:
                    break
            assert seen.actor.food > 6
            assert sum(i.count for i in seen.inventory if i.name == "minecraft:bread") == 1
            assert session.plugins.learning.episodes[-1]["goal_confirmed"]
            assert session.cognition.emotion == "pleased"
            # The model must split a compound goal, checking its actual hand before native mining.
            for _ in range(5):
                seen = choose("所持品slot=0にある木のツルハシを使って、目の前に見える石を一つ採掘したい。必要なら先に持ち替える。")
                if seen.action.get("kind") == "mine" and seen.action.get("changed"):
                    break
            assert any(i.name == "minecraft:wooden_pickaxe" and i.selected for i in seen.inventory)
            assert seen.action.get("kind") == "mine" and seen.action.get("changed")
            assert any(i.name == "minecraft:wooden_pickaxe" and i.durability < 59 for i in seen.inventory)
            assert session.plugins.confirmed_changes >= 2 and session.plugin_recent()
            assert len(session.plugins.learning.episodes) >= 3
            assert session.plugins.learning.places
            assert session.experience.state.habits["explore"].trials >= 1
        # A user's control/material teaching is evidence, just as in the actual runtime.
        session.create_turn("板材は丸太を材料にクラフトして作る物です。")
        for _ in range(3):
            seen = choose("採掘の目標は終わり。今は持っているminecraft:oak_logを材料に、解放済みレシピminecraft:oak_planksで板材をクラフトしたい。")
            if any(i.name == "minecraft:oak_planks" for i in seen.inventory):
                break
        assert sum(i.count for i in seen.inventory if i.name == "minecraft:oak_planks") == 4
        assert not any(i.name == "minecraft:oak_log" for i in seen.inventory)
        episode = session.plugins.learning.episodes[-1]
        assert episode["effect_observed"] and episode["gains"] == {"minecraft:oak_planks": 4}
        assert episode["losses"] == {"minecraft:oak_log": 1}
        # A model may predict that its material will increase. Do not relabel that prediction as correct.
        if episode["small_goal"]["check"] == "inventory_gain" and episode["small_goal"]["item"] == "minecraft:oak_log":
            assert not episode["goal_confirmed"]
        assert session.experience.state.habits["research"].trials >= 1
        session.checkpoint()
        (root / "model-results.json").write_text(json.dumps({"actions": actions,
            "attempts": session.plugins.attempts, "confirmed_changes": session.plugins.confirmed_changes,
            "learning": session.plugins.learning.model_dump(exclude={"pending"})},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "done.json").write_text('{"passed":true}', encoding="utf-8")
    except Exception as error:
        (root / "model-results.json").write_text(json.dumps({"actions": actions,
            "last_result": session.plugins.last_result, "state": session.plugins.model_dump()}, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "done.json").write_text(json.dumps({"passed": False, "error": type(error).__name__+": "+str(error)}), encoding="utf-8")
        raise
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        session.close()
