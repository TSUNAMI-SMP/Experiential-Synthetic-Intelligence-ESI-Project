"""Bodily actions compete with questions; spoken replies do not require a button."""

from esi03.motivation import Brain as PreviousBrain


class Brain(PreviousBrain):
    def __init__(self, agent_id, world, experience, state=None):
        super().__init__(agent_id, world, experience, state)
        if state is None:
            self.state.next_decision = world.elapsed

    def candidates(self, world, allow_requests=True):
        self.state.cooldowns.pop("ask_talk", None)
        choices = super().candidates(world, allow_requests)
        choices = [c for c in choices if c["kind"] not in {"rest", "drink"}
                   or world.place("rest" if c["kind"] == "rest" else "water") is not None]
        # A previously reachable cell can become occupied after furniture placement.
        if not any(world.free(x+0.5, z+0.5) for x in range(6) for z in range(5)):
            choices = [c for c in choices if c["kind"] != "explore"]
        return choices

    def _begin(self, world, kind, explored):
        action = super()._begin(world, kind, explored)
        if kind == "explore":
            action.target = world.path[-1] if world.path else world.destination
        elif kind == "drink":
            action.target = world.place("water").position.model_copy(deep=True)
        if kind.startswith("ask_"):
            self.state.cooldowns.pop(kind, None)
            if kind == "ask_talk":
                self.state.request_latches.pop(kind, None)
        return action

    def user_turn(self, world, turn):
        if (turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition"}
                or turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True)):
            return []
        relation = self.experience.link_reply(turn)
        if relation:
            turn["question_reply"] = relation
        if (self.active and self.active.kind == "ask_talk" and relation
                and relation["relation"] in {"answered", "deferred", "declined"}):
            answer = {"answered": "accept", "deferred": "later", "declined": "decline"}[relation["relation"]]
            return self.respond(world, self.active.id, answer,
                                {"type": "explicit_user_response", "turn_id": turn["id"], "quote": turn["input"]})
        return super().user_turn(world, turn)
