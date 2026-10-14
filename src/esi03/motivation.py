"""Connect bodily maintenance with personality-driven curiosity and observed habits."""

import re

from esi02.motivation import Brain as PreviousBrain


class Brain(PreviousBrain):
    def __init__(self, agent_id, world, experience, state=None):
        self.experience = experience
        super().__init__(agent_id, world, state)

    def refresh(self, world, seconds):
        super().refresh(world, seconds)
        signals = self.experience.state.modulators
        for name in ("dopamine", "serotonin", "noradrenaline", "oxytocin"):
            setattr(self.state.modulators, name, getattr(signals, name))
        self.state.needs.curiosity = self.experience.state.needs.curiosity
        self.state.needs.social = self.experience.state.needs.social

    def candidates(self, world, allow_requests=True):
        choices = [item for item in super().candidates(world, allow_requests) if item["kind"] != "ask_talk"]
        state = self.experience.state
        if (
            not any(item["kind"] == "explore" for item in choices)
            and state.needs.pleasure >= 0.45
            and world.body.energy > 0.45
            and world.body.fatigue < 0.6
            and state.needs.comfort < 0.4
            and world.elapsed >= self.state.cooldowns.get("explore", 0)
        ):
            choices.append(
                {
                    "kind": "explore",
                    "innate": 0.2 * state.needs.pleasure,
                    "prediction": self.state.estimates["explore"].predict(self.features(world)),
                    "modulation": 1.0,
                    "score": 0.2 * state.needs.pleasure,
                }
            )
        for item in choices:
            if item["kind"] == "rest":
                item["score"] *= 1 + 0.4 * state.character.values.self_respect
            elif item["kind"] == "explore":
                habit = state.habits["explore"]
                item["score"] += (
                    0.1 * state.needs.novelty
                    + 0.08 * state.needs.pleasure * state.character.traits.playfulness
                    + 0.08 * habit.strength * (1 - habit.satiation)
                    - 0.1 * habit.tolerance
                )
                enjoyment = state.habits["play"]
                item["score"] += (
                    0.08 * enjoyment.strength * (1 - enjoyment.satiation) - 0.08 * enjoyment.tolerance
                )
        topic = self.experience.question_candidate(world) if allow_requests else None
        if topic and world.elapsed >= self.state.cooldowns.get("ask_talk", 0):
            choices.append(
                {
                    "kind": "ask_talk",
                    "innate": topic["score"],
                    "prediction": self.state.estimates["ask_talk"].predict(self.features(world)),
                    "modulation": 1.0,
                    "score": topic["score"],
                    "interest_id": topic["interest"].id,
                    "reason": topic["reason"],
                }
            )
        return choices

    def _begin(self, world, kind, explored):
        topic = self.experience.question_candidate(world) if kind == "ask_talk" else None
        action = super()._begin(world, kind, explored)
        if topic:
            interest = topic["interest"]
            action.text = interest.question
            action.label = "気になっていることを尋ねる"
            self.experience.asked(interest.id, action.id)
        elif kind == "explore":
            state = self.experience.state
            mode = "play" if state.needs.pleasure > state.needs.curiosity else "explore"
            if mode == "play":
                action.label = "部屋で気分転換を楽しむ"
            state.body_action_modes = dict(list({**state.body_action_modes, action.id: mode}.items())[-64:])
        return action

    def _resolve(self, world, reward, stage, reason, provenance=None):
        identifier = self.active.id
        kind = self.active.kind
        event = super()._resolve(world, reward, stage, reason, provenance)
        self.experience.finish_question(identifier, stage, identifier)
        if reward is not None and provenance:
            activity = {"rest": "rest", "explore": "explore", "ask_talk": "question"}.get(kind)
            if kind == "explore":
                activity = self.experience.state.body_action_modes.get(identifier, "explore")
                observed = event["payload"]["action"]
                if observed["after"]["novelty"] > observed["before"]["novelty"]:
                    self.experience.state.last_novelty = world.elapsed
            if activity:
                self.experience.outcome(
                    "body:" + identifier, activity, reward, provenance, identifier, event["payload"]["action"]
                )
        return event

    def user_turn(self, world, turn):
        # Confirmed ASR text counts as real contact; uncertain ASR never does.
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return []
        value = {**turn, "source": "microphone"} if turn["source"] == "confirmed_recognition" else turn
        if (
            self.active
            and self.active.kind == "ask_talk"
            and re.fullmatch(
                r"(?:今は|いまは)?(?:忙しい|話せない|話したくない|あとで|後で)[。！! ]*",
                turn["input"].strip(),
            )
        ):
            return self.respond(
                world,
                self.active.id,
                "later",
                {"type": "explicit_user_response", "turn_id": turn["id"], "quote": turn["input"]},
            )
        return super().user_turn(world, value)

    def context(self, world):
        return {**super().context(world), "experience": self.experience.context()}
