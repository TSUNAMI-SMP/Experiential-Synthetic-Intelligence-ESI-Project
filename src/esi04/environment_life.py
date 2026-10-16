"""Durable intentions shared by game activity and conversation, separate from observed facts."""

import hashlib
import math
from collections import Counter
from typing import Annotated, Literal

from pydantic import Field

from esi.models import uid
from esi02.world import Model
from esi04.environment_learning import SmallGoal, counts, held_item

Focus = Literal["hunger", "safety", "curiosity", "growth", "social", "achievement", "autonomy"]


class GoalCondition(SmallGoal):
    amount: int = Field(default=1, ge=1, le=64)
    target_id: str = Field(default="", max_length=120)


class GoalChoice(Model):
    operation: Literal["new", "continue", "resume", "reconsider", "finish", "abandon"] = "continue"
    goal_id: str = Field(default="", max_length=32)
    purpose: str = Field(default="", max_length=160)
    focus: Focus = "curiosity"
    check: GoalCondition = Field(default_factory=GoalCondition)
    reason: str = Field(default="", max_length=200)
    source_message: str = Field(default="", max_length=100)


class LifeGoal(Model):
    id: str = Field(default_factory=lambda: uid()[:16])
    world_id: str
    dimension: str
    purpose: str = Field(max_length=160)
    focus: Focus = "curiosity"
    origin: Literal["self", "conversation"] = "self"
    source_id: str = ""
    stage: Literal["active", "suspended", "completed", "abandoned"] = "active"
    check: GoalCondition = Field(default_factory=GoalCondition)
    reason: str = Field(default="", max_length=200)
    review: str = Field(default="", max_length=400)
    created_at: float = 0
    updated_at: float = 0
    attempts: int = Field(default=0, ge=0)
    confirmations: int = Field(default=0, ge=0)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    baseline_inventory: dict[str, int] = Field(default_factory=dict, max_length=41)
    baseline_food: int = 20
    target_position: list[float] = Field(default_factory=list, max_length=3)


class LifeMessage(Model):
    id: str
    world_id: str
    speaker: Literal["user", "esi"]
    text: str = Field(max_length=1000)
    intention: str = Field(default="", max_length=400)
    at: float = 0
    channel: str = "app"


class ActionMemory(Model):
    world_id: str
    dimension: str
    choice: str = Field(max_length=200)
    signature: str = Field(max_length=400)
    scene: str = Field(default="", max_length=64)
    # Visible things vary with the gaze. Keep their identities separately so
    # turning between two familiar views cannot masquerade as a new scene.
    visible: list[Annotated[str, Field(max_length=180)]] = Field(default_factory=list, max_length=96)
    situation: str = Field(default="", max_length=64)
    effect: bool = False
    progress: bool = False
    source_id: str = ""
    at: float = 0


class LifeState(Model):
    goals: list[LifeGoal] = Field(default_factory=list, max_length=24)
    active_goal: str = ""
    messages: list[LifeMessage] = Field(default_factory=list, max_length=24)
    actions: list[ActionMemory] = Field(default_factory=list, max_length=32)
    decisions: int = Field(default=0, ge=0)
    review_required: bool = False
    review_reason: str = Field(default="", max_length=400)
    last_reason: str = Field(default="", max_length=300)
    last_choice: str = Field(default="", max_length=200)
    last_world: str = ""
    last_dimension: str = ""
    wait_until: float = 0
    wait_signature: str = ""
    seen_messages: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=64)
    sent_messages: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=64)


def current_goal(state, observation=None):
    seen = observation or state.observation
    if not seen:
        return None
    return next((g for g in state.life.goals if g.id == state.life.active_goal
                 and g.stage == "active" and (g.world_id, g.dimension) == (seen.world_id, seen.actor.dimension)), None)


def remember_message(state, identifier, speaker, text, at, channel="app", intention=""):
    seen = state.observation
    if not seen or any(m.id == identifier and m.speaker == speaker for m in state.life.messages):
        return
    state.life.messages = (state.life.messages+[LifeMessage(id=identifier, world_id=seen.world_id,
        speaker=speaker, text=text[:1000], intention=intention[:400], at=at, channel=channel)])[-24:]
    # A reply or proposal is a reason to revisit a deliberate wait, not an order to move.
    state.life.wait_until = 0


def request_review(state, reason):
    """An unexecuted/stale request is a reason to reconsider, not an observed failure or success."""
    life, seen = state.life, state.observation
    life.review_required, life.review_reason = True, str(reason)[:400]
    life.wait_until = 0
    if seen:
        life.last_world, life.last_dimension = seen.world_id, seen.actor.dimension
        goal = current_goal(state)
        if goal:
            goal.review = life.review_reason


def situation_signature(observation):
    actor = observation.actor
    return repr((observation.world_id, actor.dimension, round(actor.x), round(actor.y), round(actor.z),
                 actor.food, round(actor.health, 1), actor.dead,
                 sorted((i.slot, i.name, i.count, i.selected) for i in observation.inventory),
                 sorted({t.name for t in observation.targets if t.kind != "waypoint"}), observation.daylight))


def decision_signature(observation):
    """Material changes interrupt slow inference; ambient animation and poll IDs do not."""
    actor = observation.actor
    return (observation.world_id, observation.session_id, actor.dimension, actor.dead, actor.food//4,
            int(actor.health)//2, actor.air < 60, held_item(observation),
            tuple(sorted((i.slot, i.name, i.count) for i in observation.inventory)),
            observation.container.id if observation.container else 0, observation.action.get("paused", False))


def action_signature(action, observation):
    """Conditions of a failed attempt; a new hand/target/location can justify another try."""
    target = next((t for t in observation.targets if t.id == action.get("target_id")), None)
    return repr((action.get("kind"), (target.id, target.name) if target else "", held_item(observation),
                 action.get("slot"), action.get("recipe_id"), action.get("container_id"),
                 float(action.get("forward", 0)), float(action.get("strafe", 0)), bool(action.get("jump", False)),
                 round(observation.actor.x), round(observation.actor.y), round(observation.actor.z),
                 round(action.get("yaw", 0)), round(action.get("pitch", 0))))[:400]


def scene_signature(observation):
    targets = sorted((t.id, t.name, round(t.x, 1), round(t.y, 1), round(t.z, 1))
                     for t in observation.targets if t.kind != "waypoint")
    return hashlib.sha256(repr((situation_signature(observation), targets)).encode()).hexdigest()


def visible_objects(observation):
    return sorted({repr((t.id, t.name, round(t.x), round(t.y), round(t.z)))[:180]
                   for t in observation.targets if t.kind != "waypoint"})[:96]


def gaze_situation(observation):
    """Body and surroundings can change independently of which way we look."""
    actor = observation.actor
    value = (observation.world_id, actor.dimension, round(actor.x), round(actor.y), round(actor.z),
             actor.food, round(actor.health), observation.daylight,
             sorted((i.slot, i.name, i.count) for i in observation.inventory))
    return hashlib.sha256(repr(value).encode()).hexdigest()


def gaze_stagnated(state):
    seen = state.observation
    recent = [a for a in state.life.actions if (a.world_id, a.dimension) == (seen.world_id, seen.actor.dimension)][-3:]
    if len(recent) != 3 or not all(a.choice.startswith("look") and not a.progress for a in recent):
        return False
    if all(a.situation for a in recent):
        familiar = {item for a in recent for item in a.visible}
        return (all(a.situation == gaze_situation(seen) for a in recent)
                and set(visible_objects(seen)) <= familiar)
    # Compatibility with histories saved before identities were recorded.
    return all(a.scene == scene_signature(seen) for a in recent)


def blocked_actions(state, menu):
    """Prevent only repeatedly ineffective requests, without replacing them with a scripted action."""
    seen = state.observation
    recent = [a for a in state.life.actions[-8:] if (a.world_id, a.dimension) == (seen.world_id, seen.actor.dimension)]
    failures = Counter(a.signature for a in recent if not a.effect)
    stale_gaze = gaze_stagnated(state)
    return {name for name, action in menu.items() if action and (
        failures[action_signature(action.model_dump(), seen)] >= 2 or stale_gaze and action.kind == "look")}


def adopt_goal(state, value, at):
    """A model can propose/review intentions; only source-linked observations complete them."""
    life, seen = state.life, state.observation
    proposal = GoalChoice.model_validate(value.get("life_goal", {}))
    old = current_goal(state)
    if old and proposal.operation in {"continue", "reconsider", "finish"} and proposal.focus != old.focus:
        # Changing the underlying need is a different intention. Do not keep an
        # old exploration label on a newly chosen meal, or erase its history.
        proposal = proposal.model_copy(update={"operation": "new", "goal_id": ""})
    selected = next((g for g in life.goals if g.id == proposal.goal_id
                     and (g.world_id, g.dimension) == (seen.world_id, seen.actor.dimension)), None)
    if selected and selected.stage in {"completed", "abandoned"}:
        selected = None
    if proposal.operation in {"finish", "abandon"} and old:
        if proposal.operation == "abandon":
            if selected and selected.id == old.id:
                selected = None
            old.stage = "abandoned"
            old.review = proposal.reason or "今は別の目的を選ぶことにした。"
            life.active_goal = ""
            old = None
        else:
            old.review = "達成という判断は提案。実際の確認条件が成立するまで達成済みにしない。"
    if (proposal.operation == "new" or not old and not selected and proposal.operation in {"continue", "reconsider"}) and proposal.purpose.strip():
        selected = next((g for g in life.goals if g.stage in {"active", "suspended"}
                        and (g.world_id, g.dimension, g.purpose) == (seen.world_id, seen.actor.dimension, proposal.purpose)), None)
        if selected is None:
            selected = LifeGoal(world_id=seen.world_id, dimension=seen.actor.dimension, purpose=proposal.purpose,
                focus=proposal.focus, check=proposal.check, reason=proposal.reason, source_id=value.get("observation_id", seen.id),
                created_at=at, updated_at=at)
            selected.baseline_inventory = dict(counts([i.model_dump() for i in seen.inventory]))
            selected.baseline_food = seen.actor.food
            target = next((t for t in seen.targets if t.id == proposal.check.target_id), None)
            if target:
                selected.target_position = [target.x, target.y, target.z]
            message = next((m for m in life.messages if m.id == proposal.source_message
                            and m.speaker == "user" and m.world_id == seen.world_id), None)
            if message:
                selected.origin, selected.source_id = "conversation", message.id
            # Retain unfinished intentions preferentially while bounding the save/context.
            archived = [g for g in life.goals if g.stage in {"completed", "abandoned"}]
            unfinished = [g for g in life.goals if g.stage not in {"completed", "abandoned"}]
            life.goals = (archived[-8:]+unfinished[-15:]+[selected])[-24:]
    if selected:
        if old and old.id != selected.id:
            old.stage, old.updated_at = "suspended", at
        selected.stage, selected.updated_at = "active", at
        life.active_goal = selected.id
        if proposal.reason:
            selected.reason = proposal.reason
        if proposal.purpose:
            selected.focus = proposal.focus
        if proposal.operation in {"reconsider", "new"}:
            if proposal.purpose:
                selected.purpose = proposal.purpose
            selected.review = proposal.reason or "直前の結果から方法を見直す。"
            if proposal.check.model_dump() != selected.check.model_dump():
                changed_metric = (proposal.check.check, proposal.check.item) != (selected.check.check, selected.check.item)
                selected.check = proposal.check
                if changed_metric:
                    selected.baseline_inventory = dict(counts([i.model_dump() for i in seen.inventory]))
                    selected.baseline_food = seen.actor.food
                target = next((t for t in seen.targets if t.id == proposal.check.target_id), None)
                selected.target_position = [target.x, target.y, target.z] if target else []
    life.decisions += 1
    life.last_world, life.last_dimension = seen.world_id, seen.actor.dimension
    life.last_choice, life.last_reason = value["choice"], value["reason"][:300]
    life.review_required, life.review_reason = False, ""
    if value["choice"] in {"wait", "ask"}:
        # Duration is selected by ESI. A changed body/world or a new message wakes deliberation early.
        life.wait_until = at+max(2, min(120, value.get("wait_seconds", 10)))
        life.wait_signature = situation_signature(seen)


def observe_goal_result(state, pending, verified, identifier, at):
    """Completed primitive ≠ completed life goal. A different goal's result cannot complete this one."""
    seen, life = state.observation, state.life
    progress = bool(verified["gains"] or verified["losses"] or verified["food_delta"]
                    or verified["block_changed"] or verified["movement"] > .15)
    life.actions = (life.actions+[ActionMemory(world_id=seen.world_id, dimension=seen.actor.dimension,
        choice=pending.choice or pending.kind, signature=pending.signature,
        scene=scene_signature(seen),
        visible=visible_objects(seen), situation=gaze_situation(seen),
        effect=verified["effect_observed"], progress=progress, source_id=identifier, at=at)])[-32:]
    goal = next((g for g in life.goals if g.id == pending.life_goal_id and (g.world_id, g.dimension) ==
                 (seen.world_id, seen.actor.dimension)), None)
    if goal and goal.stage not in {"completed", "abandoned"}:
        goal.attempts += 1
        goal.updated_at, goal.review = at, verified["detail"][:400]
        criterion = goal.check
        item = criterion.item
        after = verified["after"]
        inventory = counts(after.get("inventory", []))
        baseline = goal.baseline_inventory
        target = next((t for t in seen.targets if t.id == criterion.target_id), None)
        position = [target.x, target.y, target.z] if target and target.kind == "entity" else goal.target_position
        reached = (bool(position) and (not criterion.target_id.startswith("e:") or target is not None)
                   and math.dist([after.get(a, 0) for a in "xyz"], position) < 1.6)
        checks = {
            "inventory_gain": inventory.get(item, 0)-baseline.get(item, 0) >= criterion.amount if item else bool(verified["gains"]),
            "inventory_decrease": baseline.get(item, 0)-inventory.get(item, 0) >= criterion.amount if item else bool(verified["losses"]),
            "food_increase": after.get("food", 0)-goal.baseline_food >= criterion.amount,
            "held": after.get("held") == item if item else False,
            "block_change": verified["block_changed"] and (not item or pending.target_name == item),
            "approach": reached if goal.target_position else not criterion.target_id and verified["movement"] > .15 and pending.kind in {"move", "go_to"},
            "container_open": bool(after.get("container_id")) and pending.kind == "use",
            "observe": pending.kind == "look" and verified.get("camera_changed", False)
                       and (not criterion.target_id or pending.target_id == criterion.target_id and target is not None)
                       and (not item or pending.target_name.removeprefix("item:") == item.removeprefix("item:")),
            "none": False,
        }
        # A route can fail after the wanted item was already picked up. Judge the
        # goal by its observed condition, independently of the motor's completion status.
        if checks[criterion.check]:
            goal.confirmations += 1
            goal.evidence = (goal.evidence+[identifier])[-8:]
            goal.stage = "completed"
            if life.active_goal == goal.id:
                life.active_goal = ""
    # No unobservable permanent pause. A string of ineffective/circular actions calls for another decision.
    scoped = [a for a in life.actions[-6:] if (a.world_id, a.dimension) == (seen.world_id, seen.actor.dimension)]
    if gaze_stagnated(state):
        request_review(state, "同じ周囲で視線だけを変えても、新しい変化や目的への進展を確認できていない。見るだけの反復を休み、別の方法・目的・質問・待機を考える。")
    elif len(scoped) >= 3 and (all(not a.effect for a in scoped[-3:]) or
                            len(scoped) >= 6 and all(not a.progress for a in scoped)):
        request_review(state, "変化や目的への進展が乏しいため、同じ操作を続けず目的・方法を見直す。")
        if goal:
            goal.review = life.review_reason


def waypoint_context(state, target):
    seen = state.observation
    nearby = [p for p in state.learning.places if (p.world_id, p.dimension) == (seen.world_id, seen.actor.dimension)
              and math.dist((p.x, p.y, p.z), (target.x, target.y, target.z)) < 3]
    return {"visited_before": any(p.visited for p in nearby),
            "past_risks": list(dict.fromkeys(r for p in nearby for r in p.risks))[:2]}


def life_context(state):
    seen = state.observation
    if not seen:
        return {}
    goals = [g for g in state.life.goals if (g.world_id, g.dimension) == (seen.world_id, seen.actor.dimension)]
    active = current_goal(state)
    local = (state.life.last_world, state.life.last_dimension) == (seen.world_id, seen.actor.dimension)
    missing_target = bool(active and active.check.target_id and not any(t.id == active.check.target_id for t in seen.targets))
    others = [g for g in goals if g.id != state.life.active_goal]
    def compact(g):
        return {"id": g.id, "purpose": g.purpose, "focus": g.focus, "stage": g.stage,
                "attempts": g.attempts, "confirmations": g.confirmations,
                "check": {"check": g.check.check, "item": g.check.item, "amount": g.check.amount,
                          "target_id": g.check.target_id}, "review": g.review[:180]}
    return {"active_goal": compact(active) if active else None,
            "other_goals": [compact(g) for g in others[-4:]],
            "review_required": bool(local and state.life.review_required or missing_target),
            "review_reason": ((state.life.review_reason or state.life.last_reason)[:240] if local and state.life.review_required else
                              "目的の対象は現在の観察にない。以前の場所の記憶と、今見えている対象を分けて方法・目的を再考する。" if missing_target else ""),
            "recent_conversation": [{"id": m.id, "speaker": m.speaker, "text": m.text[:240],
                                     "channel": m.channel, "intention": m.intention}
                                    for m in state.life.messages if m.world_id == seen.world_id][-6:],
            "last_choice": state.life.last_choice if local else "", "reason": state.life.last_reason if local else "",
            "pending_goal_is_intention_not_observation": True}


def observation_facts(environment):
    """Safe, selectable speech facts; model prose cannot manufacture additional game events."""
    facts = {}
    seen = environment.get("observation")
    if not environment.get("connected") or not seen:
        return {"connection": {"text": "今はMinecraftとの接続が止まっていて、現在の周囲は分からないよ。", "source": "connection"}}
    actor = seen["actor"]
    facts["body"] = {"text": f"今の満腹度は{actor['food']}/20、体力は{actor['health']:g}/{actor['max_health']:g}だよ。", "source": seen["id"]}
    names = list(dict.fromkeys(t["name"] for t in seen["targets"] if t["kind"] != "waypoint"))[:6]
    facts["sight"] = {"text": "近くには"+"、".join(names)+"が見えているよ。" if names else "まだ周囲に何があるか確かめられていないよ。", "source": seen["id"]}
    inventory = seen["inventory"]
    held = next((i["name"] for i in inventory if i.get("selected")), "何もない状態")
    facts["inventory"] = {"text": ("所持品には"+"、".join(f"{i['name']}を{i['count']}個" for i in inventory[:6])+"あるよ。" if inventory else "所持品は空だよ。")+f"手に持っているのは{held}だよ。", "source": seen["id"]}
    agenda = environment.get("life", {})
    goal = agenda.get("active_goal")
    if goal:
        facts["goal"] = {"text": f"今の目的は「{goal['purpose']}」だよ。まだ達成したとは確認していないよ。", "source": goal["id"]}
    if agenda.get("reason"):
        facts["reason"] = {"text": f"「{agenda['reason']}」と考えているところだよ。", "source": "intention"}
    for n, event in enumerate(environment.get("learning", {}).get("recent_observed_changes", [])[-2:]):
        facts[f"experience{n}"] = {"text": "さっきの操作で確認したのは、"+event["detail"], "source": event.get("source_observation", "")}
    daylight = seen.get("daylight", "unknown")
    if daylight != "unknown":
        facts["daylight"] = {"text": "今いる場所は"+{"day": "昼", "night": "夜"}[daylight]+"だよ。", "source": seen["id"]}
    if not environment.get("autonomous"):
        facts["activity"] = {"text": "今は自律活動が停止しているよ。目的や経験は覚えているよ。", "source": "control"}
    return facts
