"""Bodily maintenance remains independent of ordinary, freely initiated speech."""

from esi03.motivation import Brain as PreviousBrain
from esi04.dialogue import polite


class Brain(PreviousBrain):
    def __init__(self, agent_id, world, experience, state=None):
        super().__init__(agent_id, world, experience, state)
        if state is None:
            self.state.next_decision = world.elapsed

    def candidates(self, world, allow_requests=True):
        self.state.cooldowns.pop("ask_talk", None)
        choices = super().candidates(world, allow_requests)
        if getattr(self.experience, "free_speech", False):
            choices = [c for c in choices if not c["kind"].startswith("ask_")]
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
            if action.text and self.experience.state.character.style != "casual":
                action.text = polite(action.text)
                pending = self.experience.pending_question()
                if pending and pending.action_id == action.id:
                    pending.text = action.text
            self.state.cooldowns.pop(kind, None)
            if kind == "ask_talk":
                self.state.request_latches.pop(kind, None)
        return action

    def user_turn(self, world, turn):
        if (turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition"}
                or turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True)):
            return []
        if getattr(self.experience, "free_speech", False):
            self.state.last_contact, self.state.last_contact_epoch = world.elapsed, turn["epoch"]
            self.state.has_contact = True
            return []
        relation = self.experience.link_reply(turn)
        if relation:
            turn["question_reply"] = relation
        if (self.active and self.active.kind == "ask_talk" and relation
                and relation["relation"] in {"answered", "deferred", "declined"}):
            answer = {"answered": "accept", "deferred": "later", "declined": "decline"}[relation["relation"]]
            return self.respond(world, self.active.id, answer,
                                {"type": "explicit_user_response", "turn_id": turn["id"], "quote": turn["input"]})
        if self.active and self.active.kind == "ask_talk" and relation and relation.get("needs_semantic"):
            # The old contact detector accepts any YES as an answer. Let the contextual
            # reply resolve this question instead, while still recording actual contact.
            self.state.last_contact, self.state.last_contact_epoch = world.elapsed, turn["epoch"]
            self.state.has_contact = True
            return []
        return super().user_turn(world, turn)
