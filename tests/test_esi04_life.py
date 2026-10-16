"""Life must continue without imperative input; conversation and observations share durable intentions."""

import time
from concurrent.futures import Future
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_esi04_environment_learning import perform
from test_esi04_plugins import connected, observation

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.dialogue import DialogueLLM, compact
from esi04.environment_dialogue import speak
from esi04.environment_life import (
    adopt_goal,
    blocked_actions,
    current_goal,
    decision_signature,
    life_context,
    situation_signature,
)
from esi04.minecraft import action_menu, attention_candidates, decide, goal_conditions
from esi04.plugins import PluginCatalog
from esi04.runtime import Runtime
from esi04.session import Session


@pytest.fixture
def session(tmp_path):
    with closing(Session(tmp_path / "life.db", Settings(playback=False, natural_dialogue=False))) as value:
        connected(value)
        value.plugin_options(autonomous=True)
        yield value


def choose(session, purpose="見える物を確かめたい", check="observe", item="", amount=1,
           operation="new", goal_id="", source_message="", action="look_left", target_id="", **extra):
    def chooser(settings, system, data, schema, cancellation, tokens):
        return schema(holding="empty hand", action=action, reason="今の関心と体験から選んだ", life_goal={
            "operation": operation, "goal_id": goal_id, "purpose": purpose, "focus": "curiosity",
            "reason": "見えている物に興味がある", "source_message": source_message,
            "check": {"purpose": purpose, "check": check, "item": item, "amount": amount, "target_id": target_id}}, **extra)
    result = decide(session.settings, session.active_manifest(), session.plugins, session.experience.context(),
                    session.plugin_recent(), Cancellation(), chooser=chooser)
    session.plugin_decided(result)
    return result


def test_no_command_creates_own_purpose_and_only_observation_can_complete_it(session):
    assert session.plugins.goal == "" and not session.plugins.life.messages
    decision = choose(session, purpose="近くの物を自分で知りたい")
    goal = current_goal(session.plugins)
    assert goal.origin == "self" and goal.stage == "active"
    assert goal.source_id == session.plugins.observation.id
    # The model's 'finish' is a proposal, not evidence.
    choose(session, operation="finish", action="wait", purpose=goal.purpose, goal_id=goal.id)
    assert current_goal(session.plugins).stage == "active"
    perform(session, kind="look", check="observe", yaw=30)
    assert goal.stage == "completed" and goal.evidence
    assert not current_goal(session.plugins)
    assert session.plugins.autonomous
    assert decision["life_goal"]["purpose"] == goal.purpose


def test_suspended_purpose_resumes_and_is_world_and_dimension_scoped(session):
    choose(session, purpose="石から得られる物を確かめたい", check="inventory_gain", item="minecraft:cobblestone")
    old = current_goal(session.plugins)
    choose(session, purpose="周りの地形も知りたい")
    new = current_goal(session.plugins)
    assert old.stage == "suspended" and old.id != new.id
    choose(session, operation="resume", goal_id=old.id, purpose=old.purpose)
    assert current_goal(session.plugins).id == old.id and new.stage == "suspended"
    session.plugin_observed(observation(id="other", world_id="other").model_dump())
    assert not current_goal(session.plugins)
    assert not life_context(session.plugins)["other_goals"]
    session.plugin_observed(observation(id="nether", actor={**observation().actor.model_dump(),
                             "dimension": "minecraft:the_nether"}).model_dump())
    assert not current_goal(session.plugins)


def test_quantified_acquisition_requires_actual_total_increase(session):
    choose(session, purpose="丸石を二つ持って帰りたい", check="inventory_gain", item="minecraft:cobblestone", amount=2)
    goal = current_goal(session.plugins)
    initial = [i.model_dump() for i in session.plugins.observation.inventory]
    perform(session, inventory=initial+[dict(slot=0, name="minecraft:cobblestone", count=1)])
    assert goal.stage == "active"
    perform(session, inventory=initial+[dict(slot=0, name="minecraft:cobblestone", count=2)])
    assert goal.stage == "completed" and goal.attempts == 2


def test_reconsidering_a_target_keeps_progress_toward_the_same_quantity(session):
    choose(session, purpose="丸石を二つ集めたい", check="inventory_gain", item="minecraft:cobblestone", amount=2, target_id="stone")
    goal = current_goal(session.plugins)
    inventory = [i.model_dump() for i in session.plugins.observation.inventory]
    perform(session, inventory=inventory+[dict(slot=0, name="minecraft:cobblestone", count=1)])
    choose(session, purpose=goal.purpose, check="inventory_gain", item="minecraft:cobblestone", amount=2,
           goal_id=goal.id, operation="reconsider", action="wait", target_id="")
    assert goal.baseline_inventory.get("minecraft:cobblestone", 0) == 0
    perform(session, inventory=inventory+[dict(slot=0, name="minecraft:cobblestone", count=2)])
    assert goal.stage == "completed"


@pytest.mark.parametrize("name,kind,item", [("minecraft:oak_leaves", "block", "minecraft:oak_leaves"),
                                         ("item:minecraft:bread", "entity", "minecraft:bread")])
def test_looking_at_another_object_cannot_confirm_observing_the_named_object(session, name, kind, item):
    value = session.plugins.observation.model_dump()
    value["targets"].append({**value["targets"][0], "id": "leaf", "name": name, "kind": kind, "x": 4.5})
    session.plugin_observed(value)
    choose(session, purpose="選んだ対象を見たい", check="observe", item=item, target_id="leaf")
    goal = current_goal(session.plugins)
    perform(session, kind="look", check="observe", item="", action_id="look:stone", pitch=30)
    assert goal.stage == "active" and not goal.evidence
    perform(session, kind="look", check="observe", item="", action_id="look:leaf", yaw=-26.6, pitch=20)
    assert goal.stage == "completed" and goal.evidence


def test_abandoning_the_current_goal_does_not_resume_it_through_its_id(session):
    choose(session, purpose="今は取りやめたい試み")
    goal = current_goal(session.plugins)
    choose(session, purpose=goal.purpose, operation="abandon", goal_id=goal.id, action="wait")
    assert goal.stage == "abandoned" and not current_goal(session.plugins)


def test_repeated_gaze_without_new_scene_leaves_other_choices_available(session):
    for yaw in (30, 60, 90):
        perform(session, kind="look", check="observe", item="", yaw=yaw)
    menu = action_menu(session.active_manifest(), session.plugins.observation)
    blocked = blocked_actions(session.plugins, menu)
    assert all(key in blocked for key, action in menu.items() if action and action.kind == "look")
    assert not {"wait", "forward", "eat:10"} & blocked
    assert session.plugins.autonomous and session.plugins.life.review_required
    value = session.plugins.observation.model_dump()
    value["id"] = "new-object"
    value["targets"].append({**value["targets"][0], "id": "new-wood", "name": "minecraft:oak_log"})
    session.plugin_observed(value)
    menu = action_menu(session.active_manifest(), session.plugins.observation)
    assert not any(menu[key] and menu[key].kind == "look" for key in blocked_actions(session.plugins, menu))


def test_turning_between_familiar_views_is_not_endless_new_exploration(session):
    first = session.plugins.observation.model_dump()
    other = {**first["targets"][0], "id": "tree", "name": "minecraft:oak_log"}
    for n, targets in enumerate(([other], first["targets"], [other])):
        value = session.plugins.observation.model_dump()
        value.update(id=f"view-{n}", targets=targets)
        session.plugin_observed(value)
        perform(session, kind="look", check="observe", yaw=30+n*60)
    menu = action_menu(session.active_manifest(), session.plugins.observation)
    blocked = blocked_actions(session.plugins, menu)
    assert all(name in blocked for name, action in menu.items() if action and action.kind == "look")
    assert not {"wait", "forward", "eat:10"} & blocked


def test_a_new_need_preserves_the_old_intention_instead_of_relabelling_it(session):
    choose(session, purpose="木を調べたい")
    old = current_goal(session.plugins)
    adopt_goal(session.plugins, {"choice": "eat:10", "reason": "空腹を先に満たしたい", "life_goal": {
        "operation": "reconsider", "goal_id": old.id, "focus": "hunger", "purpose": "食事をしたい",
        "check": {"check": "food_increase"}}}, session.world.elapsed)
    active = current_goal(session.plugins)
    assert old.stage == "suspended" and old.purpose == "木を調べたい"
    assert active.id != old.id and active.purpose == "食事をしたい" and active.focus == "hunger"


def test_model_conditions_cannot_pair_an_empty_sentinel_with_a_waypoint_observation(session):
    value = session.plugins.observation.model_dump()
    value["targets"].append({**value["targets"][0], "id": "way", "name": "足場", "kind": "waypoint"})
    session.plugin_observed(value)
    seen = session.plugins.observation
    choices = goal_conditions(action_menu(session.active_manifest(), seen), seen)
    assert choices["visit:way"] == {"check": "approach", "target_id": "way", "item": ""}
    assert "look:way" not in choices
    assert all(value.get("item", "") not in {"empty", "none"} for value in choices.values())
    assert choices["food"]["check"] == "food_increase"


def test_needs_compete_for_attention_without_an_action_or_minecraft_target(session):
    needs = {"hunger": .91, "curiosity": .72, "growth": .81, "social": .4}
    assert attention_candidates(needs, {"traits": {"flexibility": .5}}) == ["hunger"]
    needs["hunger"] = .2
    assert attention_candidates(needs, {}) == ["growth"]
    needs["curiosity"] = .79
    assert attention_candidates(needs, {}) == ["curiosity", "growth"]


def test_first_owned_visit_is_learning_but_circling_known_ground_is_not(session):
    for _ in range(10):
        session.advance_world(60)
    before_growth = session.experience.state.needs.growth
    before_confidence = session.experience.state.self_efficacy
    perform(session, kind="move", check="none", item="", action_id="left", x=4.5)
    event = session.plugins.learning.episodes[-1]
    assert event["novel_visit"] and event["movement"] == 1 and not event["gains"]
    assert not event["goal_confirmed"]  # Learning a place does not declare an unrelated goal complete.
    assert "初めて" in event["detail"] and "位置が約1.0" in event["detail"]
    assert session.experience.state.needs.growth < before_growth
    assert session.experience.state.self_efficacy > before_confidence
    learned_at = session.experience.state.last_learning
    trials = session.experience.state.habits["research"].trials
    session.advance_world(60)
    perform(session, kind="move", check="none", item="", action_id="right", x=3.5)
    assert not session.plugins.learning.episodes[-1]["novel_visit"]
    assert session.experience.state.last_learning == learned_at
    assert session.experience.state.habits["research"].trials == trials


def test_glances_do_not_replace_a_real_visit_in_what_was_learned(session):
    from esi04.environment_learning import learning_context
    perform(session, kind="move", check="none", item="", action_id="left", x=4.5)
    for yaw in (30, 60, 90):
        perform(session, kind="look", check="observe", item="", yaw=yaw)
    facts = learning_context(session.plugins)["recent_observed_changes"]
    assert facts[-1]["kind"] == "move" and facts[-1]["novel_visit"]
    assert "初めて" in facts[-1]["detail"] and "位置が約1.0" in facts[-1]["detail"]


def test_late_result_cannot_complete_another_life_goal(session):
    choose(session, purpose="石を拾いたい", check="inventory_gain", item="minecraft:cobblestone")
    goal = current_goal(session.plugins)
    seen = session.plugins.observation
    action = action_menu(session.active_manifest(), seen)["mine:stone"]
    session.plugin_attempt(action.model_dump())
    choose(session, purpose="別の物を試したい", check="block_change")
    other = current_goal(session.plugins)
    from test_esi04_environment_learning import frame
    before = frame(seen)
    after = {**before, "tick": before["tick"]+1, "inventory": before["inventory"]+[
        dict(slot=0, name="minecraft:cobblestone", count=1)]}
    session.plugin_observed(observation(id="after", inventory=after["inventory"], action={"kind": "mine",
        "request_id": action.request_id, "stage": "completed", "before_observation": seen.id,
        "evidence": {"before": before, "after": after}}).model_dump())
    assert goal.stage == "completed" and other.stage == "active" and not other.evidence


def test_late_completion_cannot_be_consumed_by_a_different_job_owner(session):
    class LateCompletion(Future):
        checks = 0
        def done(self):
            self.checks += 1
            # Finish after the Minecraft owner has checked, while the inherited
            # tool/audio finishers are still running in this same scheduler tick.
            return self.checks >= 4
    def chooser(settings, system, data, schema, cancellation, tokens):
        return schema(holding="empty hand", action="wait", reason="次の関心を考えたい", life_goal={
            "operation": "new", "purpose": "少し考えてから活動したい", "check": {"check": "none"}})
    value = decide(session.settings, session.active_manifest(), session.plugins, {}, [], Cancellation(), chooser=chooser)
    runtime = Runtime(session)
    cancellation = Cancellation()
    future = LateCompletion()
    future.set_result(value)
    runtime.environment_task = {"cancellation": cancellation}
    runtime.jobs[future] = dict(kind="environment_decision", cancellation=cancellation,
                               environment_generation=runtime.environment_generation)
    try:
        runtime._finish_jobs()
        assert future in runtime.jobs, "A different owner discarded the Minecraft result"
        assert session.plugins.life.decisions == 0
        runtime._finish_jobs()
        assert future not in runtime.jobs and runtime.environment_task is None
        assert session.plugins.life.decisions == 1
    finally:
        runtime.stop()


def test_late_reply_applies_shared_memory_and_game_delivery_in_the_same_completion(session):
    class LateReply(Future):
        checks = 0
        def done(self):
            self.checks += 1
            return self.checks >= 3
    runtime = Runtime(session)
    runtime.environment_client = SimpleNamespace(chat_available=True, disconnect=lambda: None)
    try:
        runtime._new_turn("今は何を考えているの？", "minecraft")
        identifier = runtime.current["turn_id"]
        session.update_turn(identifier, minecraft_message_id="native-message")
        future = LateReply()
        future.set_result({"text": "今は木の近くで何ができるか考えたい。", "finish_reason": "stop",
                           "life_intention": "木の近くを歩いてみたい。"})
        runtime.jobs[future] = dict(kind="reply", turn_id=identifier)
        runtime._finish_jobs()
        assert future not in runtime.jobs
        assert identifier in runtime.environment_outbox
        replies = [m for m in session.plugins.life.messages if m.id == identifier and m.speaker == "esi"]
        assert len(replies) == 1 and replies[0].intention == "木の近くを歩いてみたい。"
        runtime._finish_jobs()
        assert len([m for m in session.plugins.life.messages if m.id == identifier and m.speaker == "esi"]) == 1
    finally:
        runtime.stop()


def test_ordinary_speech_and_reply_are_shared_without_replacing_purpose(session):
    choose(session, purpose="近くの石を調べたい", check="inventory_gain", item="minecraft:cobblestone")
    goal = current_goal(session.plugins)
    turn = session.create_turn("そろそろ暗くなってきたね")
    assert current_goal(session.plugins).id == goal.id
    context = compact(session.dialogue_context(turn["input"]))
    assert context["environment"]["life"]["active_goal"]["purpose"] == goal.purpose
    assert context["environment"]["life"]["recent_conversation"][-1]["text"] == turn["input"]
    session.plugin_dialogue(turn["id"], "安全な場所についても考えてみたい。")
    choose(session, purpose="安全な場所で過ごしたい", source_message=turn["id"])
    assert current_goal(session.plugins).origin == "conversation"
    assert goal.stage == "suspended"
    assert [m.speaker for m in session.plugins.life.messages[-2:]] == ["user", "esi"]


def test_persistent_goals_and_conversations_do_not_restore_access(tmp_path):
    path = tmp_path / "life.db"
    with closing(Session(path)) as original:
        connected(original)
        choose(original, purpose="石の使い方を調べたい", check="inventory_gain", item="minecraft:cobblestone")
        identifier = current_goal(original.plugins).id
        original.create_turn("この石が気になるね")
        original.plugin_options(autonomous=True)
        original.checkpoint()
    with closing(Session(path)) as restored:
        assert restored.plugins.life.active_goal == identifier
        assert restored.plugins.life.messages[-1].text == "この石が気になるね"
        assert not restored.plugins.autonomous and not restored.status.get("plugin_connected")
        assert "現在の周囲は分からない" in compact(restored.dialogue_context("今どこ？"))["environment"]["facts"]["connection"]["text"]


def test_no_effect_reconsiders_and_filters_only_same_failed_operation(session):
    for _ in range(3):
        perform(session)
    assert session.plugins.autonomous and session.plugins.life.review_required
    blocked = blocked_actions(session.plugins, action_menu(session.active_manifest(), session.plugins.observation))
    assert "mine:stone" in blocked and "eat:10" not in blocked
    seen = session.plugins.observation.model_dump()
    seen["inventory"][0]["selected"] = True
    session.plugin_observed(seen)
    assert "mine:stone" not in blocked_actions(session.plugins, action_menu(session.active_manifest(), session.plugins.observation))


def test_wait_wakes_on_new_world_or_conversation_and_pending_question_does_not_block(session, monkeypatch):
    runtime = Runtime(session)
    runtime.environment_client = SimpleNamespace(disconnect=lambda: None)
    runtime.environment_next_poll = time.monotonic()+1000
    captured = []
    runtime._job = lambda *args, **kwargs: captured.append((args, kwargs))
    try:
        choose(session, action="wait", wait_seconds=120)
        runtime.schedule_environment()
        assert not captured and session.status["plugin_phase"] == "resting"
        session.plugin_question("これは何だろう？", "気になる")
        assert session.experience.pending_question()
        # A new body signal wakes the wait even while an unanswered question remains pending.
        seen = session.plugins.observation.model_dump()
        seen["actor"]["food"] = 5
        session.plugin_observed(seen)
        runtime.schedule_environment()
        assert captured[-1][0][1] == "environment_decision"
        assert session.status["plugin_phase"] == "deciding"
        assert captured[-1][0][6]["allow_question"] is False
    finally:
        runtime.stop()


def test_polling_does_not_hide_decision_errors_or_deciding_state(session):
    runtime = Runtime(session)
    try:
        session.status.update(plugin_error="LLMの判断を再試行", plugin_error_kind="environment_decision", plugin_phase="deciding")
        future = Future()
        future.set_result(observation(id="new"))
        runtime.jobs[future] = dict(kind="environment_observe", environment_generation=runtime.environment_generation)
        runtime._finish_jobs()
        assert session.status["plugin_phase"] == "deciding"
        assert session.status["plugin_error"] == "LLMの判断を再試行"
    finally:
        runtime.stop()


def test_new_food_during_inference_cancels_old_decision_but_not_the_life_goal(session):
    choose(session, purpose="周りの物を調べたい")
    goal = current_goal(session.plugins)
    runtime = Runtime(session)
    cancellation = Cancellation()
    runtime.environment_task = {"cancellation": cancellation,
                                "situation": decision_signature(session.plugins.observation)}
    try:
        future = Future()
        value = observation(id="fresh", inventory=[dict(slot=10, name="minecraft:bread", count=4, edible=True)])
        future.set_result(value)
        runtime.jobs[future] = dict(kind="environment_observe", environment_generation=runtime.environment_generation)
        runtime._finish_jobs()
        assert cancellation.event.is_set() and current_goal(session.plugins).id == goal.id
        assert "所持品" in session.plugins.learning.plan.review
        context = life_context(session.plugins)
        assert context["review_required"] and "所持品" in context["review_reason"]
        assert "所持品" in context["active_goal"]["review"]
        assert decision_signature(value) == decision_signature(value.model_copy(update={"id": "next", "tick": 999}))
    finally:
        runtime.stop()


def test_a_stale_target_cancellation_reaches_the_next_decision_without_inventing_an_outcome(session):
    choose(session, purpose="近くの物を手に入れたい", check="inventory_gain", item="minecraft:cobblestone")
    goal = current_goal(session.plugins)
    reason = session.plugins.life.last_reason
    session.plugin_interrupted("対象が今の観察から消えたため、選んだ操作は実行せず考え直す。")
    context = life_context(session.plugins)
    assert context["review_required"] and "実行せず" in context["review_reason"]
    assert current_goal(session.plugins).id == goal.id and goal.stage == "active"
    assert not session.plugins.learning.episodes and session.plugins.life.last_reason == reason
    choose(session, purpose=goal.purpose, operation="reconsider", goal_id=goal.id, action="wait")
    assert not session.plugins.life.review_required and not session.plugins.life.review_reason


def test_a_missing_goal_target_requires_an_explicit_new_or_reconsidered_choice(session, monkeypatch):
    choose(session, purpose="見える石を調べたい", check="observe", item="minecraft:stone", target_id="stone")
    goal = current_goal(session.plugins)
    session.plugin_observed(observation(id="target-lost", targets=[]).model_dump())
    assert life_context(session.plugins)["review_required"]
    def request(settings, system, data, schema, cancellation, tokens, **kwargs):
        assert data["life"]["review_required"] and "現在の観察にない" in data["life"]["review_reason"]
        value = dict(purpose="お腹を満たしたい", comparison="好奇心より今は空腹が気になる", focus="hunger", goal_id="", reason="今は空腹も気になる",
            criterion="food", amount=1, source_message="",
            use_item="minecraft:bread", action="eat:10", question="", wait_seconds=5)
        with pytest.raises(ValueError):
            schema(operation="continue", **value)
        return schema(operation="new", **value)
    monkeypatch.setattr("esi04.minecraft.request", request)
    result = decide(session.settings, session.active_manifest(), session.plugins, {"needs": {"curiosity": .9}}, [], Cancellation(), chooser=request)
    assert result["life_goal"]["operation"] == "new" and result["choice"] == "eat:10"
    assert current_goal(session.plugins).id == goal.id and not session.plugins.learning.episodes


def test_cancelled_inference_socket_error_is_not_a_failed_life_action(session):
    choose(session, purpose="自分の目的を保っておきたい")
    goal = current_goal(session.plugins)
    runtime = Runtime(session)
    cancellation = Cancellation()
    cancellation.set()
    runtime.environment_task = {"cancellation": cancellation}
    future = Future()
    future.set_exception(OSError(9, "Bad file descriptor"))
    runtime.jobs[future] = dict(kind="environment_decision", cancellation=cancellation,
                               environment_generation=runtime.environment_generation)
    try:
        runtime._finish_jobs()
        assert runtime.environment_task is None and not session.status.get("plugin_error")
        assert session.plugins.autonomous and current_goal(session.plugins).id == goal.id
        assert not session.plugins.learning.episodes
    finally:
        runtime.stop()


def test_reconsideration_changes_future_check_without_retroactive_completion(session):
    choose(session, purpose="丸石を集めたい", check="block_change", item="minecraft:stone")
    goal = current_goal(session.plugins)
    choose(session, purpose=goal.purpose, operation="reconsider", goal_id=goal.id,
           check="inventory_gain", item="minecraft:cobblestone", amount=2)
    assert goal.check.check == "inventory_gain" and goal.check.amount == 2
    assert goal.stage == "active" and not goal.evidence


def test_world_message_uses_same_turn_once_and_reply_is_delivered_only_after_success(session):
    runtime = Runtime(session)
    delivered = []
    runtime.environment_client = SimpleNamespace(chat_available=True, disconnect=lambda: None,
                                                  say=lambda identifier, text: delivered.append((identifier, text)))
    runtime.environment_next_poll = time.monotonic()+1000
    try:
        session.plugin_observed(observation(messages=[dict(id="message1", sender="owner", text="何を考えているの？", tick=12)]).model_dump())
        runtime.schedule_environment()
        turn = session.turns[runtime.current["turn_id"]]
        assert turn["source"] == "text" and turn["input_channel"] == "minecraft" and turn["minecraft_message_id"] == "message1"
        assert session.plugins.life.messages[-1].text == turn["input"]
        runtime.schedule_environment()
        assert len(session.turns) == 1
        future = Future()
        future.set_result({"text": "周りを調べたい。", "finish_reason": "stop"})
        runtime.jobs[future] = dict(kind="reply", turn_id=turn["id"])
        runtime._finish_jobs()
        assert turn["id"] in runtime.environment_outbox
        assert turn["id"] not in session.plugins.life.sent_messages
        runtime.schedule_environment()
        deadline = time.monotonic()+2
        while not delivered and time.monotonic() < deadline:
            time.sleep(.01)
        runtime._finish_jobs()
        assert delivered == [(turn["id"], "周りを調べたい。")]
        assert turn["id"] in session.plugins.life.sent_messages
        runtime._disconnect_environment()
        assert not runtime.environment_outbox
    finally:
        runtime.stop()


def test_grounded_life_conversation_uses_selected_facts_and_future_intentions(session):
    choose(session, purpose="石がどう変わるか知りたい", check="inventory_gain", item="minecraft:cobblestone")
    context = session.dialogue_context("ここでは何をしたい？")
    def chooser(settings, system, data, schema, cancellation, tokens):
        assert "石がどう変わるか知りたい" in data["facts"]["goal"]
        with pytest.raises(ValueError):
            schema(focus="experience", acknowledgement="", refs=["invented_diamonds"], quote="", intention="", question="")
        return schema(focus="purpose", acknowledgement="", refs=["goal"], quote="", intention="手に入る物を確かめたい", question="")
    answer, meta = speak(Settings(natural_dialogue=True), "ここでは何をしたい？", [], context, Cancellation(), chooser)
    assert "石がどう変わるか知りたい" in answer and "確かめたい" in answer
    assert meta["grounding"]["policy"] == "minecraft_shared_life"
    def hallucinate(settings, system, data, schema, cancellation, tokens):
        return schema(focus="experience", acknowledgement="", refs=[], quote="ダイヤを拾った", intention="ダイヤを拾ったので家を作りたい", question="")
    answer, _ = speak(Settings(natural_dialogue=True), "何か気になる？", [], context, Cancellation(), hallucinate)
    assert "ダイヤを拾った" not in answer


def test_body_reports_do_not_replace_current_activity_and_stopping_keeps_goal(session):
    choose(session, purpose="近くの足場を調べたい", check="approach")
    goal = current_goal(session.plugins)
    turn = session.create_turn("今は何を考えているの？")
    result = DialogueLLM(session.settings).stream(turn["input"], [], Cancellation(), lambda _: None, lambda _: None,
                                                session.dialogue_context(turn["input"]))
    assert goal.purpose in result["text"] and "満腹度" not in result["text"]
    runtime = Runtime(session)
    try:
        runtime._new_turn("操作を止めて")
        assert not session.plugins.autonomous and current_goal(session.plugins).id == goal.id
    finally:
        runtime.stop()


def test_observation_signature_does_not_wake_rest_for_only_poll_ids(session):
    before = situation_signature(session.plugins.observation)
    assert before == situation_signature(observation(id="new", tick=900))
    assert before != situation_signature(observation(daylight="night"))


def test_more_than_one_direction_of_waypoints_and_visit_memory_reach_the_model(session):
    seen = observation().model_dump()
    actor = session.plugins.observation.actor
    seen["targets"] += [dict(id=f"w:{dx}:{dz}", kind="waypoint", name="足場", x=actor.x+dx, y=actor.y,
                             z=actor.z+dz, distance=4) for dx, dz in [(0,4), (4,0), (0,-4), (-4,0)]]
    session.plugin_observed(seen)
    captured = {}
    def chooser(settings, system, data, schema, cancellation, tokens):
        captured.update(data)
        return schema(holding="empty hand", action="look_left", reason="周りを調べる")
    decide(session.settings, PluginCatalog.builtin(), session.plugins, {}, [], Cancellation(), chooser=chooser)
    targets = [target for target in captured["targets"] if target["kind"] == "waypoint"]
    assert len(targets) == 4 and all("visited_before" in target for target in targets)


def test_short_model_references_round_trip_to_the_exact_native_target(session, monkeypatch):
    captured = {}
    def request(settings, system, data, schema, cancellation, tokens, **kwargs):
        captured.update(data)
        assert data["targets"][0]["id"] == "t1"
        assert "mine:t1" in data["menu"] and "mine:stone" not in data["menu"]
        assert "goal" not in data  # An instruction to choose a goal must not become the goal text.
        assert "id" not in data["self"]
        assert "reason" not in data["life"]
        assert data["life"]["recent_conversation"][-1]["text"] == "次に木の近くを歩いてみたい。"
        assert "この長い報告文" not in str(data["life"])
        # Facing west is different from the visible stone's positive-Z world offset.
        assert data["gaze"]["forward_xz"] == [-1, 0]
        assert data["gaze"]["pitch_degrees"] == 25
        assert data["targets"][0]["offset"] == [0, .5, 2]
        with pytest.raises(ValueError):
            schema(purpose="石を知りたい", comparison="空腹と好奇心を比べる", focus="curiosity", goal_id="", operation="new",
                criterion="look:hidden", amount=1, source_message="", reason="気になる",
                use_item="empty hand", action="mine:t1", question="", wait_seconds=5)
        return schema(purpose="石から何が得られるか知りたい", comparison="空腹と好奇心を比べる", focus="curiosity", goal_id="", operation="new",
            criterion="gain:any", amount=1, source_message="",
            reason="見える石に関心がある", use_item="empty hand", action="mine:t1", question="", wait_seconds=5)
    monkeypatch.setattr("esi04.minecraft.request", request)
    # Change only model transport; production input preparation and native references remain real.
    seen = session.plugins.observation
    seen.actor.yaw, seen.actor.pitch = 90, 25
    turn = session.create_turn("近くの木をどう思う？")
    session.dialogue_result(turn["id"], {"text": "この長い報告文は、新しい事実や動機ではない。", "finish_reason": "stop",
                                       "life_intention": "次に木の近くを歩いてみたい。"})
    result = decide(session.settings, session.active_manifest(), session.plugins, {"needs": {"curiosity": .9}}, [], Cancellation(), chooser=request)
    assert result["choice"] == "mine:stone" and result["action"]["target_id"] == "stone"
    assert result["life_goal"]["check"]["check"] == "inventory_gain"
    assert result["small_goal"]["check"] == "inventory_gain" and not result["small_goal"]["item"]
    assert not session.plugins.learning.episodes and not session.plugins.life.goals


def test_normal_life_question_cannot_select_an_unsolicited_body_gauge(session):
    def chooser(settings, system, data, schema, cancellation, tokens):
        assert "body" not in data["facts"]
        assert "sight" in data["facts"]
        return schema(focus="chat", acknowledgement="", refs=["sight"], quote="", intention="近くの物をもっと知りたい", question="")
    text, _ = speak(Settings(natural_dialogue=True), "ここで何が気になる？", [], session.dialogue_context("ここで何が気になる？"),
                    Cancellation(), chooser)
    assert "満腹度" not in text and "近くの物をもっと知りたい" in text


def test_environment_reply_questions_obey_the_same_quiet_time_as_autonomous_questions(session):
    def chooser(settings, system, data, schema, cancellation, tokens):
        return schema(focus="chat", acknowledgement="", refs=["sight"], quote="", intention="",
                      question="この石から何が得られるの？")
    session.experience.state.preferences.quiet_until = session.world.elapsed+60
    context = session.dialogue_context("周りを見ているんだね")
    assert not context["character"]["allow_environment_question"]
    text, extra = speak(Settings(natural_dialogue=True), "周りを見ているんだね", [], context, Cancellation(), chooser)
    assert not extra["asked_followup"] and "この石から" not in text
    session.experience.state.preferences.quiet_until = 0
    text, extra = speak(Settings(natural_dialogue=True), "周りを見ているんだね", [],
                        session.dialogue_context("周りを見ているんだね"), Cancellation(), chooser)
    assert extra["asked_followup"] and "この石から" in text


def test_game_channel_shares_human_learning_question_answers_and_presence(session):
    runtime = Runtime(session)
    try:
        session.plugin_question("星砂村とは何？", "まだ知らない場所の説明を聞きたい")
        session.experience.state.availability = "unobserved"
        runtime._new_turn("星砂村は月の国です。", "minecraft")
        turn = session.turns[runtime.current["turn_id"]]
        assert turn["source"] == "text" and turn["input_channel"] == "minecraft"
        assert session.experience.state.availability == "present"
        assert turn["question_reply"]["relation"] == "answered"
        assert not session.experience.pending_question()
        assert any(entry["source_turn_id"] == turn["id"] and "月の国" in entry["value"] for entry in session.knowledge.entries())
        assert session.plugins.life.messages[-1].channel == "minecraft"
    finally:
        runtime.stop()


def test_weak_language_acknowledgement_still_answers_with_its_actual_purpose(session):
    choose(session, purpose="見える石を自分で試したい")
    def weak(settings, system, data, schema, cancellation, tokens):
        return schema(focus="chat", acknowledgement="うん。", refs=[], quote="", intention="", question="")
    text, metadata = speak(Settings(natural_dialogue=True), "今は何を考えているの？", [],
                            session.dialogue_context("今は何を考えているの？"), Cancellation(), weak)
    assert "見える石を自分で試したい" in text and metadata["grounding"]["refs"]


def test_motor_offers_target_gaze_and_nearby_entities_but_not_same_camera_pose(session):
    seen = session.plugins.observation.model_copy(deep=True)
    from esi04.plugins import Target
    seen.targets.append(Target(id="e:owner", kind="entity", name="minecraft:player", x=5, y=64, z=9, distance=2))
    menu = action_menu(session.active_manifest(), seen)
    assert "look_ahead" not in menu and "look:stone" in menu
    assert menu["look:stone"].pitch > 0  # Eye height, not the actor's feet, determines gaze.
    assert "approach:e:owner" in menu and "attack:e:owner" not in menu
    seen.actor.pitch = 45
    menu = action_menu(session.active_manifest(), seen)
    assert "look_down" not in menu and "look_ahead" in menu


def test_first_observed_pickup_satisfies_learning_need_without_rewarding_replayed_claims(session):
    seen = session.plugins.observation.model_dump()
    seen["inventory"].append(dict(slot=0, name="minecraft:dirt", count=1, selected=True, placeable=True))
    seen["targets"] = [dict(id="e:wood", kind="entity", name="item:minecraft:oak_log", x=5.5, y=64,
                             z=8.5, distance=2)]
    session.plugin_observed(seen)
    session.advance_world(30)
    inventory = [i.model_dump() for i in session.plugins.observation.inventory]
    result = perform(session, kind="go_to", check="inventory_gain", item="minecraft:oak_log",
                     inventory=inventory+[dict(slot=1, name="minecraft:oak_log", count=1)], x=5.5)
    assert session.experience.state.last_learning == 30
    assert session.experience.state.habits["research"].trials == 1
    session.plugin_observed(session.plugins.observation.model_dump())
    assert session.experience.state.habits["research"].trials == 1
    session.advance_world(10)
    seen = session.plugins.observation.model_dump()
    seen["targets"][0].update(id="e:another", x=7.5)
    session.plugin_observed(seen)
    perform(session, kind="go_to", check="inventory_gain", item="minecraft:oak_log",
            inventory=inventory+[dict(slot=1, name="minecraft:oak_log", count=2)], x=7.5)
    assert session.experience.state.last_learning == 30  # Repeating the same transfer is not new knowledge.
    assert session.experience.state.habits["explore"].trials == 1
    assert result["evidence"]["before"] != result["evidence"]["after"]


def test_failed_walk_can_still_satisfy_a_real_acquisition_goal(session):
    from test_esi04_environment_learning import frame
    seen = session.plugins.observation.model_dump()
    seen["targets"].append(dict(id="e:bread", kind="entity", name="item:minecraft:bread", x=5.5,
                                y=64, z=8.5, distance=2))
    session.plugin_observed(seen)
    choice = choose(session, purpose="落ちたパンを一つ手に入れたい", check="inventory_gain", item="minecraft:bread",
                    action="walk:e:bread")
    goal = current_goal(session.plugins)
    seen = session.plugins.observation
    session.plugin_attempt(choice["action"], {**choice, "small_goal": {"purpose": "対象まで歩く", "check": "approach"}})
    before = frame(seen, target_distance=2)
    inventory = [dict(slot=10, name="minecraft:bread", count=3, edible=True)]
    after = {**before, "tick": seen.tick+3, "x": 4.8, "inventory": inventory, "target_distance": .7}
    result = {"kind": "go_to", "request_id": choice["action"]["request_id"], "stage": "failed",
              "detail": "途中で足場が変わった", "before_observation": seen.id, "evidence": {"before": before, "after": after}}
    value = seen.model_dump()
    value.update(id="after-partial", tick=after["tick"], inventory=inventory, action=result)
    value["actor"]["x"] = after["x"]
    session.plugin_observed(value)
    assert session.plugins.learning.plan.stage == "failed"
    assert goal.stage == "completed" and goal.evidence
    assert session.plugins.learning.episodes[-1]["gains"] == {"minecraft:bread": 1}
