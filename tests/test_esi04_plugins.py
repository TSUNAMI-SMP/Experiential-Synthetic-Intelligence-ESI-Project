"""Environment isolation, grounded bodies/actions, source-linked experience and lease privacy."""

import json
from concurrent.futures import Future

import httpx
import pytest

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.dialogue import DialogueLLM
from esi04.minecraft import action_menu, answer, current_action, decide
from esi04.plugins import Connection, EnvironmentClient, EnvironmentObservation, PluginCatalog, PluginState
from esi04.runtime import Runtime
from esi04.session import SavedState, Session


def observation(**changes):
    result = dict(id="session:1", plugin_id="minecraft.fabric", world_id="world", session_id="session", tick=12,
        actor=dict(id="actor", name="ESI", dimension="minecraft:overworld", x=3.5, y=64., z=8.5, yaw=0., pitch=0.,
                   health=18., max_health=20., food=8, saturation=0., air=300),
        inventory=[dict(slot=10, name="minecraft:bread", count=2, edible=True)],
        targets=[dict(id="stone", kind="block", name="minecraft:stone", x=3.5, y=64.5, z=10.5,
                      distance=2.3, reachable=True, harvestable=False)])
    return EnvironmentObservation.model_validate({**result, **changes})


def connected(session):
    session.environment_mode("minecraft.fabric")
    session.status.update(plugin_connected=True, plugin_session="session")
    session.plugin_observed(observation().model_dump())


@pytest.fixture
def session(tmp_path):
    value = Session(tmp_path / "life.db", Settings(playback=False, natural_dialogue=False, idle_deliberation=False))
    yield value
    value.close()


def test_legacy_saves_and_room_body_survive_external_life(session):
    old = session.dump()
    old.pop("plugins")
    assert SavedState.model_validate(old).plugins == PluginState()
    body, position = session.world.body.model_dump(), session.world.position.model_dump()
    connected(session)
    for _ in range(10):
        session.advance_world(60, autonomy=True, allow_requests=True)
    assert session.world.body.model_dump() == body and session.world.position.model_dump() == position
    assert session.world.elapsed == 600
    assert session.experience.state.needs.maintenance == pytest.approx(.6)
    assert session.experience.state.needs.comfort == session.experience.state.needs.rest == 0
    with pytest.raises(ValueError):
        session.scene_operation("place", {"kind": "snack"})
    session.environment_mode("")
    assert session.world.body.model_dump() == body
    session.advance_world(5)
    assert session.world.body.model_dump() != body


def test_reopen_keeps_experience_but_never_restores_connection_or_autonomy(tmp_path):
    path = tmp_path / "life.db"
    first = Session(path)
    connected(first)
    first.plugin_options(autonomous=True)
    result = dict(request_id="a"*32, kind="mine", stage="completed", changed=True, detail="ブロックが変わった")
    first.plugin_observed(observation(action=result).model_dump())
    first.plugin_observed(observation(id="session:2", action=result).model_dump())
    assert first.plugins.attempts == first.plugins.confirmed_changes == 1
    first.close()
    restored = Session(path)
    try:
        assert restored.plugins.active_id == "minecraft.fabric" and not restored.plugins.autonomous
        assert not restored.status.get("plugin_connected")
        assert restored.plugins.attempts == 1 and len(restored.plugin_recent()) == 1
        assert restored.experience.state.needs.comfort == restored.experience.state.needs.rest == 0
    finally:
        restored.close()


def test_stale_or_foreign_observations_cannot_change_body(session):
    connected(session)
    for changed in ({"session_id": "previous"}, {"plugin_id": "other.environment"}):
        with pytest.raises(ValueError):
            session.plugin_observed(observation(**changed).model_dump())
    assert session.plugins.observation.session_id == "session"


def test_body_answers_use_minecraft_and_disconnected_answers_cannot_claim_old_state(session):
    connected(session)
    llm = DialogueLLM(session.settings)
    for text in ("活動エネルギーを数値で教えて", "体調はどう？", "居心地はどう？"):
        result = llm.stream(text, [], Cancellation(), lambda _: None, lambda _: None, session.dialogue_context(text))
        assert "8/20" in result["text"] and "%" not in result["text"]
    for text in ("水分はいくつ？", "疲れている？", "体温は？"):
        result = llm.stream(text, [], Cancellation(), lambda _: None, lambda _: None, session.dialogue_context(text))
        assert "使っていない" in result["text"]
    session.status["plugin_connected"] = False
    result = llm.stream("体調はどう？", [], Cancellation(), lambda _: None, lambda _: None, session.dialogue_context("体調はどう？"))
    assert "分からない" in result["text"] and "8/20" not in result["text"]


def test_plugin_question_does_not_open_a_room_question_or_require_an_answer_button(session):
    connected(session)
    session.plugin_question("石の採掘には何が必要？", "手で掘る結果を知りたい")
    question = session.experience.pending_question()
    assert question.text == session.brain.active.text == "石の採掘には何が必要？"
    assert len(session.experience.state.dialogue_questions) == 1
    turn = session.create_turn("石の採掘にはツルハシが必要だよ")
    assert turn["question_reply"]["relation"] == "answered"
    assert session.experience.pending_question() is None
    session.plugin_question("石の採掘には何が必要？", "同じ観察の繰り返し")
    assert len(session.experience.state.dialogue_questions) == 1


def test_decision_is_bound_to_the_observed_menu_and_does_not_grant_items():
    manifest, seen = PluginCatalog.builtin(), observation()
    menu = action_menu(manifest, seen)
    assert "eat:10" in menu and "mine:stone" in menu and not any("diamond" in key for key in menu)
    captured = {}
    def chooser(settings, system, data, schema, cancellation, tokens):
        captured.update(data)
        with pytest.raises(ValueError):
            schema(holding="empty hand", action="mine:hidden-ore", reason="invented")
        with pytest.raises(ValueError):
            schema(holding="minecraft:wooden_pickaxe", action="mine:stone", reason="invented held tool")
        return schema(holding="empty hand", action="eat:10", reason="食べ物を持っていて空腹")
    result = decide(Settings(), manifest, PluginState(observation=seen), {}, [], Cancellation(), chooser=chooser)
    assert result["action"]["slot"] == 10 and result["action"]["observation_id"] == seen.id
    assert result["world_id"] == seen.world_id
    assert not any("seed" in key for key in captured)


def test_loopback_client_authenticates_each_request_and_never_sends_reason_as_code():
    calls = []
    secret = "private-connection-code"
    def respond(request):
        assert request.headers["authorization"] == "Bearer "+secret
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/v1/connect":
            return httpx.Response(200, json={"protocol": 1, "plugin_id": "minecraft.fabric", "lease": "lease"})
        assert request.headers["x-esi-lease"] == "lease"
        return httpx.Response(200, json=observation().model_dump() if request.url.path == "/v1/state" else {"accepted": True})
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    client = EnvironmentClient(PluginCatalog.builtin(), Connection(token=secret), factory)
    seen = client.connect("character", "凛音エル")
    action = action_menu(client.manifest, seen)["eat:10"]
    action.reason = "a reason is not a command"
    client.act(action.model_dump())
    client.disconnect()
    assert "reason" not in calls[2][1] and not client.lease
    assert secret not in repr(client.connection)
    for endpoint in ("https://example.com", "http://127.0.0.1:8766/path", "http://user@localhost:8766"):
        with pytest.raises(ValueError) as error:
            Connection(endpoint=endpoint, token=secret)
        assert secret not in str(error.value)


def test_canceled_old_decision_cannot_block_a_new_connection(session):
    runtime = Runtime(session)
    try:
        connected(session)
        cancellation = Cancellation()
        runtime.environment_task = {"cancellation": cancellation}
        old_generation = runtime.environment_generation
        runtime._disconnect_environment()
        assert cancellation.event.is_set() and runtime.environment_task is None
        future = Future()
        future.set_result({"choice": "wait"})
        runtime.jobs[future] = dict(kind="environment_decision", environment_generation=old_generation,
                                    cancellation=cancellation)
        current = {"cancellation": Cancellation()}
        runtime.environment_task = current
        runtime._finish_jobs()
        assert runtime.environment_task is current and not runtime.jobs
    finally:
        runtime.stop()


def test_minecraft_stop_disables_autonomy(session):
    connected(session)
    session.plugin_options(autonomous=True)
    session.plugin_observed(observation(action={"paused": True, "stage": "stopped"}).model_dump())
    assert not session.plugins.autonomous


def test_slow_decision_rechecks_latest_visible_target_and_actual_inventory():
    manifest, seen = PluginCatalog.builtin(), observation()
    def chooser(settings, system, data, schema, cancellation, tokens):
        return schema(holding="empty hand", action="mine:stone", reason="道具を確かめた")
    result = decide(Settings(), manifest, PluginState(observation=seen), {}, [], Cancellation(), chooser=chooser)
    latest = observation(id="session:999", tick=999)
    assert current_action(result, latest, manifest)["observation_id"] == latest.id
    assert current_action(result, latest.model_copy(update={"targets": []}), manifest) is None
    foreign = latest.model_copy(update={"session_id": "other"})
    assert current_action(result, foreign, manifest) is None
    def eat(settings, system, data, schema, cancellation, tokens):
        return schema(holding="empty hand", action="eat:10", reason="空腹")
    result = decide(Settings(), manifest, PluginState(observation=seen), {}, [], Cancellation(), chooser=eat)
    assert current_action(result, latest.model_copy(update={"inventory": []}), manifest) is None


def test_one_corrupt_external_definition_does_not_hide_the_bundled_plugin(tmp_path):
    (tmp_path / "broken.json").write_text("{incomplete", encoding="utf-8")
    assert PluginCatalog(tmp_path).manifests()[0].id == "minecraft.fabric"


def test_teaching_about_food_does_not_become_an_unrelated_hunger_report(session):
    connected(session)
    assert answer("パンは食べると満腹度を回復するよ", session.environment_context()) is None
    assert answer("私はパンを食べている", session.environment_context()) is None
    assert answer("パンを食べるとどうなるの？", session.environment_context()) is None
    assert answer("ダイヤモンドはどこ？", session.environment_context()) is None


def test_late_connection_completion_is_disconnected_after_mode_change(session):
    class LateClient:
        closed = False
        def disconnect(self):
            self.closed = True
    runtime, client = Runtime(session), LateClient()
    try:
        future = Future()
        generation = runtime.environment_generation
        runtime.jobs[future] = dict(kind="environment_connect", environment_generation=generation, client=client)
        runtime._handle("plugin", ("virtual", {}))
        future.set_result(observation())
        runtime._finish_jobs()
        assert client.closed and runtime.environment_client is None and not session.plugins.active_id
        assert not session.status.get("plugin_connected")
    finally:
        runtime.stop()


def test_queued_stop_remains_busy_when_an_older_observation_finishes(session):
    runtime = Runtime(session)
    try:
        connected(session)
        completed, stopping = Future(), Future()
        completed.set_result(observation())
        runtime.jobs[completed] = dict(kind="environment_observe", environment_generation=runtime.environment_generation)
        runtime.jobs[stopping] = dict(kind="environment_stop", environment_generation=runtime.environment_generation)
        runtime.environment_io = "environment_stop"
        runtime._finish_jobs()
        assert runtime.environment_io == "environment_stop"
        stopping.set_result({"stopped": True})
        runtime._finish_jobs()
        assert runtime.environment_io is None
    finally:
        runtime.stop()


def test_minecraft_conversation_distinguishes_story_from_equipment_request(session):
    runtime = Runtime(session)
    try:
        connected(session)
        runtime._new_turn("私はパンを食べているよ")
        assert not list(session.turns.values())[-1].get("plugin_request")
        runtime._new_turn("木のツルハシを手に持って")
        assert session.plugins.goal == "木のツルハシを手に持って"
        assert not session.plugins.autonomous
        runtime._new_turn("操作を止めて")
        assert list(session.turns.values())[-1].get("plugin_request") == "Minecraftでの自律活動を停止した"
        for stop in ("やめて", "停止して", "もう止めて"):
            session.plugin_options(autonomous=True)
            runtime._new_turn(stop)
            assert not session.plugins.autonomous
    finally:
        runtime.stop()
