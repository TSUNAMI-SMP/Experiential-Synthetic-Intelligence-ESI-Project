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


def test_local_model_chooses_real_food_and_mining_actions(tmp_path):
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
        while seen.actor.food != 6 and time.monotonic() < deadline:
            threading.Event().wait(.2)
            seen = client.observe()
        assert seen.actor.food == 6
        session.plugin_observed(seen.model_dump())
        def choose(goal):
            session.plugin_options(goal=goal)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(decide, settings, client.manifest, session.plugins.model_copy(deep=True),
                                     session.experience.context(), session.plugin_recent(), Cancellation())
                while not future.done():
                    session.plugin_observed(client.observe().model_dump())
                    threading.Event().wait(.8)
                result = future.result()
            actions.append({"choice": result["choice"], "reason": result["reason"]})
            assert result["action"], "Expected a physical action for the explicit test goal"
            action = current_action(result, client.observe(), client.manifest)
            assert action, "The chosen target disappeared during inference"
            client.act(action)
            deadline = time.monotonic()+18
            while time.monotonic() < deadline:
                seen = client.observe()
                session.plugin_observed(seen.model_dump())
                if seen.action.get("stage") != "running":
                    return seen
                threading.Event().wait(.2)
            raise AssertionError("Real action did not finish")
        for _ in range(3):
            seen = choose("空腹なので、持っているパンを食べて満腹度を回復したい。")
            if seen.actor.food > 6:
                break
        assert seen.actor.food > 6
        assert sum(i.count for i in seen.inventory if i.name == "minecraft:bread") == 1
        # Teach one control at a time as well as testing the physical effect of each choice.
        for _ in range(3):
            seen = choose("所持品slot=0のminecraft:wooden_pickaxeを装備して手に持つ。")
            if any(i.name == "minecraft:wooden_pickaxe" and i.selected for i in seen.inventory):
                break
        assert any(i.name == "minecraft:wooden_pickaxe" and i.selected for i in seen.inventory)
        for _ in range(5):
            seen = choose("木のツルハシを手に持って、目の前に見えている石を一つ採掘したい。")
            if (seen.action.get("kind") == "mine" and seen.action.get("changed")
                    and any(i.name == "minecraft:wooden_pickaxe" and i.durability < 59 for i in seen.inventory)):
                break
        assert seen.action.get("kind") == "mine" and seen.action.get("changed")
        assert any(i.name == "minecraft:wooden_pickaxe" and i.durability < 59 for i in seen.inventory)
        assert session.plugins.confirmed_changes >= 2 and session.plugin_recent()
        session.checkpoint()
        (root / "model-results.json").write_text(json.dumps({"actions": actions,
            "attempts": session.plugins.attempts, "confirmed_changes": session.plugins.confirmed_changes},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "done.json").write_text('{"passed":true}', encoding="utf-8")
    except Exception as error:
        (root / "model-results.json").write_text(json.dumps({"actions": actions,
            "last_result": session.plugins.last_result}, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "done.json").write_text(json.dumps({"passed": False, "error": type(error).__name__+": "+str(error)}), encoding="utf-8")
        raise
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        session.close()
