"""Virtual needs and observed-outcome learning. Model prose cannot award itself a reward."""

import hashlib
import math
import re
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from esi02.world import PLACES, Model, Point

Kind = Literal["rest", "drink", "explore", "ask_cool", "ask_warm", "ask_quiet", "ask_talk"]
KINDS = ("rest", "drink", "explore", "ask_cool", "ask_warm", "ask_quiet", "ask_talk")
REQUESTS = {"ask_cool", "ask_warm", "ask_quiet", "ask_talk"}
LABELS = {
    "rest": "休む",
    "drink": "水分を補給",
    "explore": "部屋を調べる",
    "ask_cool": "温度を下げてほしい",
    "ask_warm": "温度を上げてほしい",
    "ask_quiet": "静かにしてほしい",
    "ask_talk": "話したい",
}
FEATURES = 10


def clip(value, low=0.0, high=1.0):
    return max(low, min(high, value))


class Needs(Model):
    maintenance: float = Field(default=0, ge=0, le=1)
    comfort: float = Field(default=0, ge=0, le=1)
    rest: float = Field(default=0, ge=0, le=1)
    curiosity: float = Field(default=0, ge=0, le=1)
    social: float = Field(default=0, ge=0, le=1)


class Modulators(Model):
    dopamine: float = Field(default=0.25, ge=0, le=1)
    serotonin: float = Field(default=0.5, ge=0, le=1)
    noradrenaline: float = Field(default=0.1, ge=0, le=1)
    oxytocin: float = Field(default=0.2, ge=0, le=1)


class Measurement(Model):
    elapsed: float = Field(ge=0)
    energy: float = Field(ge=0, le=1)
    fatigue: float = Field(ge=0, le=1)
    hydration: float = Field(ge=0, le=1)
    condition: float = Field(ge=0, le=1)
    thermal: float = Field(ge=0, le=1)
    noise: float = Field(ge=0, le=1)
    body_discomfort: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0)
    last_contact: float = Field(ge=0)


class Estimate(Model):
    weights: list[float] = Field(
        default_factory=lambda: [0.0] * FEATURES, min_length=FEATURES, max_length=FEATURES
    )
    precision_inverse: list[list[float]] = Field(
        default_factory=lambda: np.eye(FEATURES).tolist(), min_length=FEATURES, max_length=FEATURES
    )
    count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_matrix(self):
        matrix = np.asarray(self.precision_inverse, dtype=float)
        weights = np.asarray(self.weights, dtype=float)
        if (
            matrix.shape != (FEATURES, FEATURES)
            or not np.isfinite(matrix).all()
            or not np.isfinite(weights).all()
        ):
            raise ValueError("行動予測の行列が有限値ではありません。")
        if not np.allclose(matrix, matrix.T, atol=1e-8) or np.min(np.linalg.eigvalsh(matrix)) <= 0:
            raise ValueError("行動予測の行列が正定値ではありません。")
        return self

    def predict(self, features):
        return clip(float(np.dot(self.weights, features)), -1, 1)

    def learn(self, features, reward, provenance):
        if provenance not in {"virtual_world_observation", "explicit_user_response"}:
            raise ValueError("自己出力、想像、言語モデルの評価で行動予測を学習できません。")
        x = np.asarray(features, dtype=float)
        if x.shape != (FEATURES,) or not np.isfinite(x).all() or np.max(np.abs(x)) > 1:
            raise ValueError("行動予測の特徴が正しくありません。")
        if (
            not isinstance(reward, (int, float))
            or isinstance(reward, bool)
            or not math.isfinite(reward)
            or not -1 <= reward <= 1
        ):
            raise ValueError("観測した報酬は-1〜1の有限値です。")
        inverse = np.asarray(self.precision_inverse)
        gain = inverse @ x / (1 + x @ inverse @ x)
        before = self.predict(x)
        weights = np.asarray(self.weights) + gain * (reward - before)
        updated = inverse - np.outer(gain, x @ inverse)
        checked = Estimate(
            weights=weights.tolist(),
            precision_inverse=((updated + updated.T) / 2).tolist(),
            count=self.count + 1,
        )
        self.weights, self.precision_inverse, self.count = (
            checked.weights,
            checked.precision_inverse,
            checked.count,
        )
        return before


class Action(Model):
    id: str
    kind: Kind
    label: str
    before: Measurement
    features: list[float] = Field(min_length=FEATURES, max_length=FEATURES)
    predicted_reward: float = Field(ge=-1, le=1)
    deadline: float = Field(ge=0)
    target: Point | None = None
    stage: Literal["running", "awaiting", "complete", "unknown", "declined", "cancelled"] = "running"
    text: str | None = None
    delivered: Literal[
        "not_sent", "synthesizing", "playing", "played", "file_only", "interrupted", "failed"
    ] = "not_sent"
    audio: dict | None = None
    reward: float | None = Field(default=None, ge=-1, le=1)
    contributions: dict[str, float] = Field(default_factory=dict)
    learned: bool = False
    evidence: dict | None = None
    confounded: bool = False
    explored: bool = False
    reason: str | None = None
    after: Measurement | None = None


class State(Model):
    version: Literal[1] = 1
    enabled: bool = True
    needs: Needs = Field(default_factory=Needs)
    modulators: Modulators = Field(default_factory=Modulators)
    estimates: dict[Kind, Estimate] = Field(default_factory=lambda: {k: Estimate() for k in KINDS})
    sequence: int = Field(default=0, ge=0)
    rng: int = Field(default=1, ge=0, le=0xFFFFFFFF)
    next_decision: float = Field(default=30, ge=0)
    last_contact: float = Field(default=0, ge=0)
    last_contact_epoch: int = Field(default=0, ge=0)
    has_contact: bool = False
    last_novelty: float = Field(default=0, ge=0)
    novelty: float = Field(default=0, ge=0)
    visits: dict[str, int] = Field(default_factory=dict, max_length=30)
    last_cell: str | None = None
    active_id: str | None = None
    actions: list[Action] = Field(default_factory=list, max_length=64)
    cooldowns: dict[Kind, float] = Field(default_factory=dict)
    request_latches: dict[Kind, float] = Field(default_factory=dict)
    last_request: float | None = Field(default=None, ge=0)
    manual_until: float = Field(default=0, ge=0)
    last_drink: float | None = Field(default=None, ge=0)
    prediction_error: float = Field(default=0, ge=-2, le=2)

    @model_validator(mode="after")
    def consistent(self):
        if set(self.estimates) != set(KINDS):
            raise ValueError("行動予測の種類が一致しません。")
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("行動IDが重複しています。")
        if self.active_id and not any(
            a.id == self.active_id and a.stage in {"running", "awaiting"} for a in self.actions
        ):
            raise ValueError("進行中の行動を確認できません。")
        if any(not re.fullmatch(r"[0-5],[0-4]", cell) or count < 1 for cell, count in self.visits.items()):
            raise ValueError("部屋の訪問記録が正しくありません。")
        if any(not math.isfinite(t) or t < 0 for t in self.cooldowns.values()):
            raise ValueError("お願いの待ち時間が正しくありません。")
        if any(
            kind not in REQUESTS or not math.isfinite(value) for kind, value in self.request_latches.items()
        ):
            raise ValueError("お願いの条件記録が正しくありません。")
        return self


class Brain:
    def __init__(self, agent_id, world, state=None):
        self.agent_id = agent_id
        self.state = (
            State.model_validate(state)
            if state is not None
            else State(
                next_decision=world.elapsed + 30, last_contact=world.elapsed, last_novelty=world.elapsed
            )
        )
        self.refresh(world, 0)

    @property
    def active(self):
        return next((a for a in self.state.actions if a.id == self.state.active_id), None)

    @staticmethod
    def cell(point):
        return f"{min(5, int(point.x))},{min(4, int(point.z))}"

    def measurement(self, world):
        body, stimulus = world.body, world.observe()
        return Measurement(
            elapsed=world.elapsed,
            energy=body.energy,
            fatigue=body.fatigue,
            hydration=body.hydration,
            condition=body.condition,
            thermal=stimulus.thermal_discomfort,
            noise=stimulus.noise_discomfort,
            body_discomfort=stimulus.body_discomfort,
            novelty=self.state.novelty,
            last_contact=self.state.last_contact,
        )

    def refresh(self, world, seconds):
        state, body, stimulus = self.state, world.body, world.observe()
        # An unanswered request stays latched until the condition resolves or materially worsens.
        # This is updated only by observed events, never by a UI read of candidates().
        for kind, trigger in list(state.request_latches.items()):
            reset = {
                "ask_cool": stimulus.temperature <= 26 or stimulus.temperature >= trigger + 3,
                "ask_warm": stimulus.temperature >= 20 or stimulus.temperature <= trigger - 3,
                "ask_quiet": stimulus.noise_db <= 50 or stimulus.noise_db >= trigger + 10,
                "ask_talk": state.last_contact_epoch > trigger,
            }[kind]
            if reset:
                del state.request_latches[kind]
        cell = self.cell(world.position)
        if cell != state.last_cell:
            visits = state.visits.get(cell, 0)
            state.visits[cell] = min(1_000_000, visits + 1)
            increment = 1 / ((visits + 1) ** 2)
            state.novelty += increment
            if visits == 0:
                state.last_novelty = world.elapsed
            state.last_cell = cell
        state.needs = Needs(
            maintenance=max(1 - body.energy, 1 - body.hydration, 1 - body.condition),
            comfort=max(stimulus.thermal_discomfort, stimulus.noise_discomfort, stimulus.body_discomfort),
            rest=body.fatigue,
            curiosity=clip((world.elapsed - state.last_novelty) / 180),
            social=clip((world.elapsed - state.last_contact) / 300),
        )
        need, modulators = state.needs, state.modulators
        targets = {
            "dopamine": clip(0.25 + state.prediction_error * 0.5),
            "serotonin": clip(0.25 + 0.5 * (1 - max(need.comfort, need.maintenance))),
            "noradrenaline": clip(0.1 + 0.8 * max(need.comfort, need.maintenance, need.rest)),
            "oxytocin": clip(
                0.15
                + (
                    0.55 * math.exp(-max(0, world.elapsed - state.last_contact) / 60)
                    if state.has_contact
                    else 0
                )
            ),
        }
        alpha = 1 - math.exp(-seconds / 15)
        for name, target in targets.items():
            setattr(
                modulators, name, getattr(modulators, name) + alpha * (target - getattr(modulators, name))
            )
        state.prediction_error *= math.exp(-seconds / 30)

    def features(self, world):
        body, stimulus, need = world.body, world.observe(), self.state.needs
        return [
            1.0,
            1 - body.energy,
            body.fatigue,
            1 - body.hydration,
            1 - body.condition,
            stimulus.thermal_discomfort,
            stimulus.noise_discomfort,
            need.curiosity,
            need.social,
            float(world.conversation_active),
        ]

    def _random(self):
        self.state.rng = (1664525 * self.state.rng + 1013904223) & 0xFFFFFFFF
        return self.state.rng / 0x100000000

    def candidates(self, world, allow_requests=True):
        state, body, needs, stimulus = self.state, world.body, self.state.needs, world.observe()
        scores = {}
        if body.fatigue >= 0.30 or body.energy < 0.55:
            scores["rest"] = max(body.fatigue, 1 - body.energy)
        if body.hydration <= 0.60 and (state.last_drink is None or world.elapsed - state.last_drink >= 60):
            scores["drink"] = 1 - body.hydration
        if needs.curiosity >= 0.25 and body.energy > 0.45 and body.fatigue < 0.60 and needs.comfort < 0.40:
            scores["explore"] = 0.25 * needs.curiosity
        if (
            allow_requests
            and not world.conversation_active
            and (state.last_request is None or world.elapsed - state.last_request >= 60)
        ):
            if stimulus.temperature > 27 and stimulus.thermal_discomfort >= 0.25:
                scores["ask_cool"] = stimulus.thermal_discomfort
            if stimulus.temperature < 18 and stimulus.thermal_discomfort >= 0.25:
                scores["ask_warm"] = stimulus.thermal_discomfort
            if stimulus.noise_discomfort >= 0.25:
                scores["ask_quiet"] = stimulus.noise_discomfort
            if needs.social >= 0.65:
                scores["ask_talk"] = 0.35 * needs.social
        features = self.features(world)
        mod = state.modulators
        modulation = {
            "rest": 0.95 + 0.10 * (1 - mod.serotonin) + 0.10 * mod.noradrenaline,
            "drink": 1 + 0.10 * mod.noradrenaline,
            "explore": 0.90 + 0.40 * mod.dopamine,
            "ask_cool": 1 + 0.15 * mod.noradrenaline,
            "ask_warm": 1 + 0.15 * mod.noradrenaline,
            "ask_quiet": 1 + 0.15 * mod.noradrenaline,
            "ask_talk": 1 + 0.15 * mod.oxytocin,
        }
        return [
            {
                "kind": kind,
                "innate": value,
                "prediction": state.estimates[kind].predict(features),
                "modulation": modulation[kind],
                "score": value * modulation[kind] + 0.25 * state.estimates[kind].predict(features),
            }
            for kind, value in scores.items()
            if world.elapsed >= state.cooldowns.get(kind, 0) and kind not in state.request_latches
        ]

    def tick(self, world, seconds, allow_actions=False, allow_requests=False):
        self.refresh(world, seconds)
        if (
            self.active
            and self.active.kind not in REQUESTS
            and bool(self.active.features[-1]) != world.conversation_active
        ):
            self.active.confounded = True
        events = self._observe_action(world)
        state = self.state
        if (
            self.active
            and self.active.kind in REQUESTS
            and (world.body.energy < 0.10 or world.body.fatigue > 0.90 or world.body.hydration < 0.15)
        ):
            events.append(self._resolve(world, None, "cancelled", "身体の緊急の維持を優先した"))
            state = self.state
        if (
            not allow_actions
            or not state.enabled
            or self.active
            or world.elapsed < max(state.next_decision, state.manual_until)
        ):
            return events
        # User-directed travel is not replaced by an autonomous decision.
        if world.destination is not None:
            return events
        state.next_decision = world.elapsed + 5
        candidates = self.candidates(world, allow_requests)
        if not candidates:
            if world.body.activity in {"resting", "exploring"}:
                world.set_activity("idle")
            return events
        candidates.sort(key=lambda c: c["score"], reverse=True)
        critical = world.body.energy < 0.10 or world.body.fatigue > 0.90 or world.body.hydration < 0.15
        exploration_rate = clip(
            0.04 + 0.08 * state.modulators.dopamine - 0.035 * state.modulators.noradrenaline, 0.01, 0.10
        )
        explored = not critical and self._random() < exploration_rate and len(candidates) > 1
        choice = candidates[int(self._random() * len(candidates))] if explored else candidates[0]
        if critical:
            urgent = "drink" if world.body.hydration < 0.15 else "rest"
            choice = next((c for c in candidates if c["kind"] == urgent), choice)
        action = self._begin(world, choice["kind"], explored)
        events.append(
            {
                "kind": "motivation_action",
                "payload": {"action": action.model_dump(), "candidates": candidates, "simulation_only": True},
            }
        )
        return events

    def _begin(self, world, kind, explored):
        state = self.state
        state.sequence += 1
        identifier = hashlib.sha256(f"{self.agent_id}:{state.sequence}".encode()).hexdigest()[:32]
        horizon = 120 if kind in REQUESTS else 30 if kind == "rest" else 15
        features = self.features(world)
        action = Action(
            id=identifier,
            kind=kind,
            label=LABELS[kind],
            before=self.measurement(world),
            features=features,
            predicted_reward=state.estimates[kind].predict(features),
            deadline=world.elapsed + horizon,
            stage="awaiting" if kind in REQUESTS else "running",
            explored=explored,
        )
        if kind == "rest":
            world.set_activity("resting")
        elif kind == "drink":
            world.go_to("water")
            action.target = Point.model_validate(PLACES["water"]["position"])
        elif kind == "explore":
            points = [Point(x=x + 0.5, z=z + 0.5) for x in range(6) for z in range(5)]
            points.sort(key=lambda p: (state.visits.get(self.cell(p), 0), world.position.distance(p)))
            point = next((p for p in points if world.position.distance(p) > 0.8), points[0])
            action.target = point
            world.move_to(point.model_dump(), "exploring")
        else:
            if world.body.activity in {"resting", "exploring"}:
                world.set_activity("idle")
            state.last_request = world.elapsed
            state.cooldowns[kind] = world.elapsed + 300
            state.request_latches[kind] = float(
                {
                    "ask_cool": world.observe().temperature,
                    "ask_warm": world.observe().temperature,
                    "ask_quiet": world.observe().noise_db,
                    "ask_talk": state.last_contact_epoch,
                }[kind]
            )
            action.text = {
                "ask_cool": f"仮想の部屋が{world.observe().temperature:.0f}度になっています。温度を少し下げてもらえますか。",
                "ask_warm": f"仮想の部屋が{world.observe().temperature:.0f}度になっています。温度を少し上げてもらえますか。",
                "ask_quiet": "仮想の部屋の音が大きくなっています。少し静かにしてもらえますか。",
                "ask_talk": "よかったら、少しお話ししませんか。",
            }[kind]
        # Modify the list and active ID together to retain referential integrity under validation.
        payload = state.model_dump()
        payload["actions"] = (payload["actions"] + [action.model_dump()])[-64:]
        payload["active_id"] = identifier
        self.state = State.model_validate(payload)
        return self.active

    def _observe_action(self, world):
        action = self.active
        if not action:
            return []
        if action.kind == "drink" and world.near("water") and world.destination is None:
            added_before = world.body.hydration
            world.drink()
            self.state.last_drink = world.elapsed
            if world.body.hydration - added_before < 0.05:
                return [self._resolve(world, None, "unknown", "補給量が小さすぎるため学習しない")]
            action.evidence = {
                "type": "virtual_world_observation",
                "hydration_added": world.body.hydration - added_before,
            }
            return [self._resolve_measured(world)]
        if action.kind in REQUESTS:
            if (
                action.evidence
                and action.evidence.get("type") == "environment_controls"
                and world.elapsed - action.before.elapsed >= 15
            ):
                return [self._resolve_measured(world)]
            if world.elapsed >= action.deadline:
                return [self._resolve(world, None, "unknown", "結果を確認できないまま待ち時間が終了")]
        elif world.elapsed >= action.deadline:
            if world.destination is not None:
                return [self._resolve(world, None, "unknown", "目的地へ到着していない")]
            return [self._resolve_measured(world)]
        return []

    def _resolve_measured(self, world):
        action, after = self.active, self.measurement(world)
        if action.confounded:
            return self._resolve(world, None, "unknown", "途中に別の介入があり、結果をこの行動に帰属できない")
        before = action.before
        contributions = {
            "energy": 0.8 * (after.energy - before.energy),
            "fatigue": 0.8 * (before.fatigue - after.fatigue),
            "hydration": 0.7 * (after.hydration - before.hydration),
            "condition": after.condition - before.condition,
            "thermal": 0.5 * (before.thermal - after.thermal),
            "noise": 0.4 * (before.noise - after.noise),
            "body_discomfort": 0.3 * (before.body_discomfort - after.body_discomfort),
        }
        if action.kind == "explore":
            contributions["novelty"] = min(0.15, 0.05 * (after.novelty - before.novelty))
        contributions["cost"] = -0.01 - (0.05 if action.kind in REQUESTS else 0)
        action.contributions = contributions
        reward = clip(sum(contributions.values()), -1, 1)
        return self._resolve(
            world, reward, "complete", "実行前後の仮想身体・環境の観測差", "virtual_world_observation"
        )

    def _resolve(self, world, reward, stage, reason, provenance=None):
        action, state = self.active, self.state
        action.after = self.measurement(world)
        action.reward, action.reason = reward, reason
        if reward is not None:
            prediction = state.estimates[action.kind].learn(action.features, reward, provenance)
            state.prediction_error = clip(reward - prediction, -2, 2)
            action.learned = True
        payload = state.model_dump()
        # The active reference is cleared in the same validation step as its terminal stage.
        payload["active_id"] = None
        for item in payload["actions"]:
            if item["id"] == action.id:
                item["stage"] = stage
        if action.kind not in REQUESTS:
            payload["cooldowns"][action.kind] = world.elapsed + (30 if action.kind == "rest" else 60)
        self.state = State.model_validate(payload)
        resolved = next(a for a in self.state.actions if a.id == action.id)
        return {
            "kind": "motivation_outcome",
            "payload": {"action": resolved.model_dump(), "provenance": provenance, "simulation_only": True},
        }

    def user_turn(self, world, turn):
        if (
            turn["source"] not in {"text", "microphone", "recording"}
            or not turn["input"].strip()
            or turn["epoch"] <= self.state.last_contact_epoch
        ):
            return []
        self.state.last_contact, self.state.last_contact_epoch = world.elapsed, turn["epoch"]
        self.state.has_contact = True
        active = self.active
        if not active:
            return []
        if active.kind not in REQUESTS:
            if not world.conversation_active:
                active.confounded = True
            return []
        normalized = re.sub(r"[\s。！!、,]", "", turn["input"])
        if re.fullmatch(
            r"(?:ごめん(?:ね|なさい)?|すみません)?(?:今は|いまは|ちょっと)?(?:無理|あとで|後で|話したくない|話しかけないで|お願いはやめて|やめて)(?:です|ください|ね)?",
            normalized,
        ):
            if re.search(r"お願いはやめて|話しかけないで|やめて", normalized):
                for kind in REQUESTS:
                    self.state.cooldowns[kind] = world.elapsed + 900
            return self.respond(
                world,
                active.id,
                "decline",
                {"type": "explicit_user_response", "turn_id": turn["id"], "quote": turn["input"]},
            )
        if active.kind == "ask_talk" and re.fullmatch(
            r"(?:うん|はい|いいよ|話そう|話しましょう|お話ししよう|こんにちは)(?:、?話そう|お話ししましょう)?",
            normalized,
        ):
            return self.respond(
                world,
                active.id,
                "accept",
                {"type": "explicit_user_response", "turn_id": turn["id"], "quote": turn["input"]},
            )
        return []

    def respond(self, world, action_id, answer, evidence=None):
        action = self.active
        if not action or action.id != action_id or action.kind not in REQUESTS:
            raise ValueError("このお願いはすでに終了しています。")
        if answer not in {"accept", "decline", "later"}:
            raise ValueError("お願いへの返答が正しくありません。")
        action.evidence = evidence or {
            "type": "explicit_user_response",
            "source": "request-buttons",
            "answer": answer,
        }
        if answer in {"decline", "later"}:
            self.state.cooldowns[action.kind] = world.elapsed + 900
            if action.kind == "ask_talk":
                self.state.request_latches[action.kind] = float(self.state.last_contact_epoch)
            return [
                self._resolve(
                    world, -0.15, "declined", "本人がお願いを断った・後にした", "explicit_user_response"
                )
            ]
        if action.kind == "ask_talk":
            self.state.last_contact = world.elapsed
            self.state.has_contact = True
            self.state.cooldowns[action.kind] = world.elapsed + 600
            action.contributions = {"explicit_acceptance": 0.08, "request_cost": -0.06}
            return [
                self._resolve(world, 0.02, "complete", "本人の明示的な対話への返答", "explicit_user_response")
            ]
        # Agreement alone does not prove the room changed. Await actual environmental evidence.
        return []

    def intervention(self, world, operation, values):
        action = self.active
        if action and operation == "environment" and action.kind in REQUESTS:
            relevant = {
                "ask_cool": ("temperature", -1),
                "ask_warm": ("temperature", 1),
                "ask_quiet": ("noise_db", -1),
            }.get(action.kind)
            if (
                relevant
                and relevant[0] in values
                and (values[relevant[0]] - getattr(world.room, relevant[0])) * relevant[1] > 0
            ):
                action.evidence = {
                    "type": "environment_controls",
                    "field": relevant[0],
                    "before": getattr(world.room, relevant[0]),
                    "after": values[relevant[0]],
                }
                return []
        if action:
            event = self._resolve(world, None, "cancelled", "本人の操作を優先したため終了")
            events = [event]
        else:
            events = []
        if operation in {"place", "move", "activity", "drink"}:
            self.state.manual_until = world.elapsed + 60
        return events

    def set_enabled(self, world, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("自発的な行動はON/OFFで選んでください。")
        events = []
        if not enabled and self.active:
            events.append(self._resolve(world, None, "cancelled", "自発的な行動をOFFにした"))
            if world.body.activity in {"moving", "resting", "exploring"}:
                world.set_activity("idle")
        self.state.enabled = enabled
        return events

    def restart(self, world):
        if self.active and self.active.kind in REQUESTS:
            return [self._resolve(world, None, "cancelled", "再開時に古いお願いを自動で発声しない")]
        return []

    def delivery(self, action_id, stage, audio=None):
        action = next((a for a in self.state.actions if a.id == action_id), None)
        if not action or action.kind not in REQUESTS:
            raise ValueError("お願いの発声記録を確認できません。")
        if stage not in {"synthesizing", "playing", "played", "file_only", "interrupted", "failed"}:
            raise ValueError("お願いの発声状態が正しくありません。")
        if action.delivered in {"played", "file_only", "interrupted", "failed"}:
            return
        action.delivered = stage
        if audio is not None:
            action.audio = audio

    def context(self, world):
        active = self.active
        return {
            "simulation_only": True,
            "enabled": self.state.enabled,
            "needs": self.state.needs.model_dump(),
            "modulators": self.state.modulators.model_dump(),
            "active_action": {
                "id": active.id,
                "kind": active.kind,
                "label": active.label,
                "stage": active.stage,
                "request": active.text,
                "response_observed": bool(
                    active.evidence and active.evidence.get("type") == "environment_controls"
                ),
            }
            if active
            else None,
            "requests_are_optional": True,
            "recent_requests": self.recent_requests(world),
        }

    def recent_requests(self, world):
        return [
            {
                "request": a.text,
                "stage": a.stage,
                "delivery": a.delivered,
                "reason": a.reason,
                "result_observed": a.learned,
            }
            for a in self.state.actions
            if a.kind in REQUESTS and world.elapsed - a.before.elapsed <= 300
        ][-2:]

    def view(self, world):
        return {
            **self.context(world),
            "actions": [a.model_dump() for a in self.state.actions[-12:]],
            "counts": {k: estimate.count for k, estimate in self.state.estimates.items()},
            "candidates": self.candidates(world),
            "last_reward": next((a.reward for a in reversed(self.state.actions) if a.learned), None),
        }
