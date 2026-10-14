"""Connect bodily maintenance with personality-driven curiosity and observed habits."""

from esi02.motivation import Brain as PreviousBrain


class Brain(PreviousBrain):
    def __init__(self, agent_id, world, experience, state=None):
        self.experience = experience
        super().__init__(agent_id, world, state)

    def candidates(self, world, allow_requests=True):
        choices = [item for item in super().candidates(world, allow_requests) if item["kind"] != "ask_talk"]
        state = self.experience.state
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
        return action

    def _resolve(self, world, reward, stage, reason, provenance=None):
        identifier = self.active.id
        kind = self.active.kind
        event = super()._resolve(world, reward, stage, reason, provenance)
        self.experience.finish_question(identifier, stage, identifier)
        if reward is not None and provenance:
            activity = {"rest": "rest", "explore": "explore", "ask_talk": "question"}.get(kind)
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
        return super().user_turn(world, value)

    def context(self, world):
        return {**super().context(world), "experience": self.experience.context()}
