"""Opt-in continuous real Qwen + real Fabric + production owner/scheduler, with no action instructions."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from esi04.config import Settings
from esi04.environment_life import current_goal
from esi04.plugins import Connection
from esi04.runtime import Runtime
from esi04.session import Session

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("ESI_MC_LIFE_E2E"),
                                                       reason="running Fabric life GameTest required")]


class TextOnlyRuntime(Runtime):
    """Only the audio sink is replaced: this check measures thinking, text conversation and game physics."""
    def _speak(self, item):
        return {"text": item["text"], "index": item["index"], "played": False,
                "audio": {"path": "", "duration": 0}, "synthesis_seconds": 0, "external_evidence": False}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def wait_for(predicate, seconds, description):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(.1)
    raise AssertionError(description)


def test_no_command_life_persists_through_conversation_and_revises_goals(tmp_path):
    root = Path(os.environ["ESI_MC_LIFE_E2E"])
    path = root / "connection.json"
    wait_for(path.exists, 120, "Fabric life fixture did not start")
    connection = Connection.model_validate_json(path.read_text(encoding="utf-8"))
    settings = Settings(playback=False, llm_device="cpu", llm_timeout=360, idle_deliberation=False)
    session = Session(tmp_path / "life.db", settings)
    session.environment_mode("minecraft.fabric")
    runtime = TextOnlyRuntime(session)
    snapshots, chats, last_count, last_report, orphan_since = [], [], -1, 0, None
    started = time.monotonic()
    minimum = int(os.environ.get("ESI_MC_LIFE_DECISIONS", "12"))
    try:
        runtime.start(microphone=False)
        assert runtime.send("plugin", ("connect", {"id": "minecraft.fabric", "connection": connection.model_dump()}))
        wait_for(lambda: session.status.get("plugin_connected"), 30, "Cannot connect real Minecraft body")
        wait_for((root / "fixture.json").exists, 15, "Fixture body not ready")
        wait_for(lambda: session.plugins.observation.actor.food == 6, 10, "Initial hunger not observed")
        # Let the initial dropped items settle before evaluating the first decision.
        wait_for(lambda: session.plugins.observation.tick > 30, 10, "Initial state not observed")
        assert not session.plugins.observation.inventory, "Test food must not be picked up before ESI chooses an action"
        wait_for(lambda: any(t.name == "item:minecraft:bread" for t in session.plugins.observation.targets),
                 10, "The visible food fixture was not observed")
        assert not session.plugins.goal and not session.plugins.life.messages
        runtime.send("plugin", ("options", {"autonomous": True}))
        wait_for(lambda: session.plugins.autonomous, 5, "Autonomous life did not start")
        deadline = time.monotonic()+5400
        while time.monotonic() < deadline:
            with session.lock:
                count = session.plugins.life.decisions
                if count != last_count:
                    last_count = count
                    goal = current_goal(session.plugins)
                    snapshots.append({"seconds": round(time.monotonic()-started, 1), "decisions": count,
                        "purpose": goal.purpose if goal else None, "goals": [g.model_dump() for g in session.plugins.life.goals],
                        "observation": session.plugins.observation.model_dump(),
                        "last_reason": session.plugins.life.last_reason, "last_choice": session.plugins.life.last_choice,
                        "review_required": session.plugins.life.review_required,
                        "review_reason": session.plugins.life.review_reason,
                        "errors": {k: v for k, v in session.status.items() if "error" in k and v}})
                assert session.status.get("running"), session.status
                assert session.plugins.autonomous, "Life stopped without an explicit stop or disconnection"
                assert not session.plugins.observation.actor.dead, "ESI died in the bounded test landscape"
                assert not session.plugins.goal, "The harness must not inject an action goal"
                worker_present = any(job["kind"] == "environment_decision" for job in list(runtime.jobs.values()))
                if runtime.environment_task and not worker_present:
                    orphan_since = orphan_since or time.monotonic()
                    assert time.monotonic()-orphan_since < 10, "A pending decision lost its worker result"
                else:
                    orphan_since = None
                turns = list(session.turns.values())
                complete = [turn for turn in turns if turn["stage"] == "complete"]
                # Interrupt once before the next decision, never tell ESI which physical action to choose.
                if count >= 3 and not chats:
                    runtime.send("text", "今は何を考えているの？")
                    chats.append({"channel": "app", "text": "今は何を考えているの？", "at_decision": count})
                elif count >= 6 and len(chats) == 1 and complete:
                    write_json(root / "signals.json", {"id": "conversation1", "chat": "ここで何をして過ごしたい？"})
                    chats.append({"channel": "minecraft", "text": "ここで何をして過ごしたい？", "at_decision": count})
                elif count >= 9 and len(chats) == 2 and len(complete) >= 2:
                    runtime.send("text", "さっき何を覚えた？")
                    chats.append({"channel": "app", "text": "さっき何を覚えた？", "at_decision": count})
                episodes = session.plugins.learning.episodes
                active_kinds = {event["kind"] for event in episodes if event["effect_observed"]}
                goals_formed = len({g.purpose.strip() for g in session.plugins.life.goals if g.origin == "self"})
                visited = len([p for p in session.plugins.learning.places if p.visited])
                achieved = (goals_formed >= 2 and len(active_kinds) >= 3 and visited >= 3
                    and any(event["gains"] for event in episodes)
                    and any(event["kind"] == "eat" and event["goal_confirmed"] for event in episodes))
                if time.monotonic()-last_report >= 15:
                    write_json(root / "progress.json", {"snapshots": snapshots, "chats": chats,
                        "seconds": round(time.monotonic()-started, 1), "phase": session.status.get("plugin_phase"),
                        "recent_episodes": episodes[-8:], "visited": visited,
                        "inventory": [i.model_dump() for i in session.plugins.observation.inventory],
                        "food": session.plugins.observation.actor.food,
                        "jobs": session.status.get("jobs", []),
                        "errors": {k:v for k,v in session.status.items() if "error" in k and v}})
                    last_report = time.monotonic()
                if count >= minimum and len(complete) >= 3 and achieved and not session.plugins.learning.pending:
                    break
            threading.Event().wait(.2)
        assert last_count >= minimum, "Not enough continuous self-directed decisions"
        wait_for(lambda: not session.plugins.learning.pending, 20, "Last physical action did not finish")
        with session.lock:
            life = session.plugins.life.model_dump()
            episodes = list(session.plugins.learning.episodes)
            turns = list(session.turns.values())
            places = [p for p in session.plugins.learning.places if p.visited]
            assert len({g["purpose"].strip() for g in life["goals"] if g["origin"] == "self"}) >= 2, "ESI did not form its own successive purposes"
            assert len({event["kind"] for event in episodes if event["effect_observed"]}) >= 3, "Activity remained a single repeated primitive"
            assert len(places) >= 3, "ESI did not explore beyond its initial cell"
            assert any(event["gains"] for event in episodes), "No real acquisition was observed"
            assert any(event["kind"] == "eat" and event["goal_confirmed"] for event in episodes), "Hunger never led to confirmed eating"
            assert len([turn for turn in turns if turn["stage"] == "complete"]) >= 3
            for turn in turns:
                if turn["stage"] == "complete":
                    assert len(turn["reply"]) > 12, "A bare acknowledgement is not a meaningful life conversation"
                    assert turn.get("grounding", {}).get("refs"), "Life answers must refer to actual goals or observations"
            game_turns = [turn for turn in turns if turn.get("minecraft_message_id")]
            assert game_turns and all(turn["id"] in life["sent_messages"] for turn in game_turns), "In-game reply was not acknowledged by the bridge"
            assert all(turn.get("grounding", {}).get("policy") != "pretrained_knowledge" for turn in turns)
            assert any("minecraft_shared_life" in str(turn) for turn in turns), "Conversation did not use the life context"
            assert session.plugins.autonomous and not session.plugins.goal
            write_json(root / "results.json", {"seconds": round(time.monotonic()-started, 1), "snapshots": snapshots,
                "life": life, "episodes": episodes, "turns": turns, "visited_places": [p.model_dump() for p in places],
                "audio": "text-only sink; physical audio devices are outside this test"})
        # A deliberate stop still wins; retaining intentions must never auto-resume native input.
        runtime.send("plugin", ("stop", {}))
        wait_for(lambda: not session.plugins.autonomous, 5, "Stop did not disable autonomy")
        runtime.stop()
        session.checkpoint()
        goals = [goal.id for goal in session.plugins.life.goals]
        session.close()
        restored = Session(tmp_path / "life.db")
        try:
            assert [goal.id for goal in restored.plugins.life.goals] == goals
            assert not restored.plugins.autonomous and not restored.status.get("plugin_connected")
        finally:
            restored.close()
        write_json(root / "done.json", {"passed": True})
    except Exception as error:
        write_json(root / "results-failed.json", {"error": type(error).__name__+": "+str(error),
            "snapshots": snapshots, "plugins": session.plugins.model_dump(), "status": session.status,
            "turns": list(session.turns.values())})
        write_json(root / "done.json", {"passed": False, "error": type(error).__name__+": "+str(error)[:1000]})
        raise
    finally:
        if not runtime.halt.is_set():
            runtime.stop()
        if session.store.db:
            try:
                session.close()
            except Exception:
                pass
