"""Bounded, world-scoped experience. Intentions and hypotheses never become sensor facts."""

import math
from collections import Counter
from typing import Annotated, Literal

from pydantic import Field

from esi02.world import Model


class SmallGoal(Model):
    purpose: str = Field(default="", max_length=160)
    check: Literal["observe", "held", "inventory_gain", "inventory_decrease", "food_increase",
                   "block_change", "approach", "container_open", "none"] = "observe"
    item: str = Field(default="", max_length=120)
    following: list[Annotated[str, Field(max_length=160)]] = Field(default_factory=list, max_length=3)


class LifePlan(Model):
    world_id: str = ""
    goal: str = Field(default="", max_length=1000)
    step: SmallGoal = Field(default_factory=SmallGoal)
    focus: Literal["hunger", "safety", "curiosity", "growth", "social", "achievement", "autonomy"] = "curiosity"
    stage: Literal["proposed", "running", "confirmed", "unobserved", "failed", "paused"] = "paused"
    reason: str = Field(default="", max_length=300)
    review: str = Field(default="", max_length=400)
    source_id: str = Field(default="", max_length=128)
    request_id: str = ""
    revisions: int = Field(default=0, ge=0)


class PlaceMemory(Model):
    key: str
    world_id: str
    dimension: str
    x: float
    y: float
    z: float
    visited: bool = False
    names: list[str] = Field(default_factory=list, max_length=16)
    risks: list[str] = Field(default_factory=list, max_length=8)
    observation_id: str
    tick: int
    at: float = Field(ge=0)


class TrialMemory(Model):
    key: str
    world_id: str
    dimension: str = ""
    action: str
    target: str = ""
    held: str = ""
    trials: int = Field(default=0, ge=0)
    effects: int = Field(default=0, ge=0)
    goal_checks: int = Field(default=0, ge=0)
    last_source: str = ""
    last_detail: str = Field(default="", max_length=400)


class StorageMemory(Model):
    world_id: str
    dimension: str
    target_id: str
    x: float
    y: float
    z: float
    items: dict[str, int] = Field(default_factory=dict, max_length=90)
    source_id: str
    tick: int


class PendingAttempt(Model):
    request_id: str
    world_id: str
    session_id: str
    dimension: str
    observation_id: str
    kind: str
    target_id: str = ""
    target_name: str = ""
    held: str = ""
    recipe_id: str = ""
    step: SmallGoal = Field(default_factory=SmallGoal)
    life_goal_id: str = ""
    choice: str = Field(default="", max_length=200)
    signature: str = Field(default="", max_length=400)
    known_visited: list[str] = Field(default_factory=list, max_length=192)


class LifeLearning(Model):
    plan: LifePlan = Field(default_factory=LifePlan)
    pending: PendingAttempt | None = None
    places: list[PlaceMemory] = Field(default_factory=list, max_length=192)
    storage: list[StorageMemory] = Field(default_factory=list, max_length=32)
    trials: list[TrialMemory] = Field(default_factory=list, max_length=96)
    episodes: list[dict] = Field(default_factory=list, max_length=64)
    consecutive_no_effect: int = Field(default=0, ge=0)


def held_item(observation):
    return next((i.name for i in observation.inventory if i.selected), "empty hand")


def counts(items):
    result = Counter()
    for item in items:
        if 0 <= item.get("slot", -1) < 41:
            result[item["name"]] += item["count"]
    return result


def place_key(world_id, dimension, x, y, z):
    return f"{world_id}:{dimension}:{math.floor(x/4)},{math.floor(y/4)},{math.floor(z/4)}"


def remember_places(learning, observation, at):
    """A cell records only targets actually returned by the visibility-limited sensor."""
    actor = observation.actor
    points = [(actor.x, actor.y, actor.z, None)] + [
        (t.x, t.y, t.z, t) for t in observation.targets if t.kind != "waypoint"]
    by_key = {p.key: p for p in learning.places}
    for x, y, z, target in points:
        key = place_key(observation.world_id, actor.dimension, x, y, z)
        place = by_key.get(key)
        if place is None:
            place = PlaceMemory(key=key, world_id=observation.world_id, dimension=actor.dimension,
                                x=x, y=y, z=z, observation_id=observation.id, tick=observation.tick, at=at)
            by_key[key] = place
        if target is None:
            place.visited = True
        else:
            place.names = list(dict.fromkeys(place.names + [target.name]))[-16:]
            if target.name in {"minecraft:lava", "minecraft:fire", "minecraft:cactus", "minecraft:magma_block", "minecraft:campfire"}:
                place.risks = list(dict.fromkeys(place.risks + ["見えた危険候補: "+target.name]))[-8:]
        place.observation_id, place.tick, place.at = observation.id, observation.tick, at
    learning.places = sorted(by_key.values(), key=lambda p: p.at)[-192:]
    container = observation.container
    if container and all(getattr(container, axis) is not None for axis in "xyz"):
        items = Counter()
        for item in container.items:
            items[item.name] += item.count
        memory = StorageMemory(world_id=observation.world_id, dimension=actor.dimension,
                               target_id=container.target_id, x=container.x, y=container.y, z=container.z,
                               items=dict(items), source_id=observation.id, tick=observation.tick)
        learning.storage = ([s for s in learning.storage if (s.world_id, s.dimension, s.target_id) !=
                            (memory.world_id, memory.dimension, memory.target_id)]+[memory])[-32:]


def check_result(pending, evidence, observation):
    """Check an intended outcome against the MOD's immediate before/after snapshots."""
    before, after = evidence.get("before", {}), evidence.get("after", {})
    if (not before or not after or before.get("dimension") != pending.dimension
            or after.get("dimension") != pending.dimension or before.get("actor_id") != observation.actor.id
            or after.get("actor_id") != observation.actor.id or after.get("tick", -1) < before.get("tick", 0)):
        return None
    old, new = counts(before.get("inventory", [])), counts(after.get("inventory", []))
    gains, losses = dict(new-old), dict(old-new)
    moved = math.sqrt(sum((after.get(axis, 0)-before.get(axis, 0))**2 for axis in "xyz"))
    block_changed = before.get("target_block") is not None and before.get("target_block") != after.get("target_block")
    food = after.get("food", 0)-before.get("food", 0)
    saturation = after.get("saturation", 0)-before.get("saturation", 0)
    health = after.get("health", 0)-before.get("health", 0)
    camera_changed = (abs((after.get("yaw", 0)-before.get("yaw", 0)+180) % 360-180) > .1
                      or abs(after.get("pitch", 0)-before.get("pitch", 0)) > .1)
    wear = {}
    for old_item in before.get("inventory", []):
        new_item = next((i for i in after.get("inventory", []) if i.get("slot") == old_item.get("slot")
                         and i.get("name") == old_item.get("name")), None)
        if new_item and old_item.get("durability") is not None and new_item.get("durability") is not None:
            used = old_item["durability"]-new_item["durability"]
            if used > 0:
                wear[old_item["name"]] = used
    expected = pending.step
    effect = bool(gains or losses or wear or moved > .15 or block_changed or food or saturation or health
                  or before.get("held") != after.get("held") or before.get("container_id") != after.get("container_id")
                  or camera_changed)
    checks = {
        "observe": camera_changed if pending.kind == "look" else effect,
        "held": after.get("held") == (expected.item or before.get("selected_item")),
        "inventory_gain": gains.get(expected.item, 0) > 0 if expected.item else bool(gains),
        "inventory_decrease": losses.get(expected.item, 0) > 0 if expected.item else bool(losses),
        "food_increase": food > 0 or saturation > 0,
        "block_change": block_changed,
        "approach": moved > .15 and (not pending.target_id or after.get("target_distance", 100) < before.get("target_distance", 100)),
        "container_open": bool(after.get("container_id")) and after.get("container_id") != before.get("container_id"),
        "none": False,
    }
    if pending.kind == "select_slot":
        detail = f"手持ちが{before.get('held')}から{after.get('held')}へ変化。所持品の総数とは別に確認。"
    elif before.get("target_block") == after.get("target_block") and pending.kind == "mine":
        detail = "対象のブロックの変化を確認できなかった。原因はまだ確定していない。"
    elif pending.kind == "mine" and block_changed and not gains:
        detail = "ブロックの変化を確認。所持品の増加は未確認で、取得したとは言えない。"
    else:
        detail = f"所持品増加 {gains or 'なし'} / 減少 {losses or 'なし'} / 満腹度 {food:+} / 体力 {health:+g}"
    if wear:
        detail += f" 道具の耐久度消費 {wear}。"
    if pending.kind in {"move", "go_to"}:
        detail = f"位置が約{moved:.1f}ブロック移動。"+detail
    elif pending.kind == "look":
        detail = ("視線の向きが変わった。" if camera_changed else "視線の変化は確認できなかった。")+detail
    return {"gains": gains, "losses": losses, "movement": round(moved, 2), "food_delta": food,
            "health_delta": health, "durability_used": wear, "block_changed": block_changed, "effect_observed": effect,
            "camera_changed": camera_changed,
            "goal_confirmed": bool(checks[expected.check]), "detail": detail,
            "before": before, "after": after}


def learning_context(state):
    seen, learning = state.observation, state.learning
    if not seen:
        return {}
    places = [p for p in learning.places if p.world_id == seen.world_id and p.dimension == seen.actor.dimension]
    places.sort(key=lambda p: sum((getattr(p, a)-getattr(seen.actor, a))**2 for a in "xyz"))
    trials = [t for t in learning.trials if (t.world_id, t.dimension) == (seen.world_id, seen.actor.dimension)][-4:]
    plan = learning.plan.model_dump(exclude={"source_id", "request_id", "world_id", "reason", "review", "goal"})
    plan["same_goal"] = learning.plan.goal == state.goal
    events = [e for e in learning.episodes if e.get("world_id") == seen.world_id]
    # A recent glance should not erase what was actually acquired or learned on
    # a visit when answering "what did you learn?". These are past observations.
    meaningful = [e for e in events if e.get("gains") or e.get("losses") or e.get("movement", 0) > .15
                  or e.get("kind") != "look" and e.get("effect_observed")]
    return {"plan": plan if learning.plan.world_id == seen.world_id else {},
            "past_observed_places_not_current_sight": [
                {"position": [round(p.x), round(p.y), round(p.z)], "names": p.names[:4], "risks": p.risks,
                 "visited": p.visited, "last_tick": p.tick} for p in places[:4]],
            "trials_not_universal_rules": [t.model_dump(exclude={"key", "world_id", "last_source", "dimension"}) for t in trials],
            "past_opened_storage_not_current_contents": [
                {"target_id": s.target_id, "position": [s.x, s.y, s.z], "items": dict(list(s.items.items())[:5]), "last_tick": s.tick}
                for s in learning.storage if (s.world_id, s.dimension) == (seen.world_id, seen.actor.dimension)][-2:],
            "recent_observed_changes": [{k: e.get(k) for k in ("kind", "goal_confirmed", "movement", "novel_visit", "detail", "source_observation")}
                                        for e in (meaningful or events)[-2:]]}
