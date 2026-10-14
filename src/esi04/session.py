"""ESI-04 state owner: dialogue, objects and summaries commit as one durable change."""

import copy
import re
from typing import Literal

from pydantic import Field

from esi.models import uid
from esi03.session import SavedState as PreviousSavedState
from esi03.session import Session as PreviousSession
from esi03.storage import atomic
from esi04.agency import Agency
from esi04.beliefs import Beliefs
from esi04.config import Settings
from esi04.experience import YES, Experience
from esi04.motivation import Brain
from esi04.state import Cognition, ExperienceState, Preferences
from esi04.world import World, scene_command


class SavedState(PreviousSavedState):
    release: Literal["ESI-04"]
    world: World
    experience: ExperienceState
    cognition: Cognition = Field(default_factory=Cognition)


class Session(PreviousSession):
    release_name = "ESI-04"
    settings_class, state_class = Settings, SavedState
    world_class, experience_class, brain_class = World, Experience, Brain
    beliefs_class = Beliefs
    preferences_class = Preferences
    settings_key, live_key = "esi04_settings", "esi04_live"

    def __init__(self, *args, **kwargs):
        self.cognition = Cognition()
        super().__init__(*args, **kwargs)

    @property
    def agency(self):
        return Agency(self)

    def dump(self):
        return {**super().dump(), "cognition": self.cognition.model_dump()}

    def restore_additional(self, state):
        self.cognition = state.cognition.model_copy(deep=True)

    def _remember(self, turn):
        super()._remember(turn)
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return
        text = turn["input"]
        command = scene_command(text, self.world)
        if command:
            if command.get("error"):
                turn["scene_result"] = {"error": command["error"]}
            else:
                try:
                    turn["scene_result"] = self.scene_operation(command["operation"], command["values"], turn["id"])
                except ValueError as exc:
                    turn["scene_result"] = {"error": str(exc)}
        relation = turn.get("question_reply")
        if relation and relation["relation"] == "answered" and len(relation["quote"]) >= 2:
            question = next(q for q in self.experience.state.dialogue_questions if q.id == relation["question_id"])
            source = self.turns.get(relation["source_turn"])
            if source and not YES.fullmatch(relation["quote"].strip()):
                if self.knowledge.from_answer(question, source):
                    changed = [i for i in self.knowledge.entries(include_old=True) if i["source_turn_id"] == source["id"]]
                    self.experience.learned(changed, source["id"])
        if re.search(r"(?:知識|覚えたこと|学び方|確かめ).*(?:ネット|検索|調べ)", text):
            self.experience.interest("教わった内容の確かめ方", "教わった内容を別の資料でも確かめてみたい。",
                                     "相手が、ネットで調べて確かめる方法を提案した", "user", turn["id"], 0.8)
            self.agency.summarize("提案を受け取る", "自分で知識を確かめる方法", "相手の提案を今後の調査に結び付けた",
                                  [turn["id"]], certainty="observed")
        self.cognition.intention = "listen"
        self.brain.state.next_decision = self.world.elapsed

    @atomic
    def scene_operation(self, operation, values, source_id=None):
        source_id = source_id or "ui:"+uid()
        self.store.db.execute("CREATE TABLE IF NOT EXISTS esi04_scene_effects (source_id TEXT PRIMARY KEY)")
        if self.store.db.execute("SELECT 1 FROM esi04_scene_effects WHERE source_id=?", (source_id,)).fetchone():
            return None
        event = self.world.object_operation(operation, values, source_id)
        if not event:
            return None
        self.store.db.execute("INSERT INTO esi04_scene_effects VALUES (?)", (source_id,))
        obj = event["object"]
        description = {"place": "物を置いた", "move": "物を移動した", "remove": "物を取り除いた",
                       "inspect": "近くで物を確かめた", "eat": "実際に食べ物を一個食べた",
                       "use": "物を使った", "play": "物を使って遊んだ"}[operation]
        self.agency.summarize(description, obj["label"] if obj else values["id"], "仮想空間の状態変化を確認した",
                              [event["id"]], outcome=description, certainty="observed")
        if operation in {"inspect", "eat", "play", "use"}:
            activity = "explore" if operation == "inspect" else "play"
            reward = 0.1 if operation == "inspect" else 0.18 if operation == "eat" else 0.12
            self.experience.outcome("scene:"+event["id"], activity, reward, "virtual_world_observation",
                                    event["id"], event)
            self.cognition.emotion, self.cognition.emotion_strength = "pleased", min(1, reward*3)
            self.cognition.intention = "enjoy"
        self.experience.advance(self.world, 0)
        self.brain.state.next_decision = self.world.elapsed
        self._write([("scene_operation", event)])
        return event

    @atomic
    def advance_world(self, seconds, conversing=False, autonomy=False, allow_requests=False):
        with self.lock:
            self.world.conversation_active = conversing
            self.world.advance(seconds)
            self.experience.advance(self.world, seconds)
            busy = self.agency.step() if autonomy and not conversing else False
            events = self.brain.tick(self.world, seconds, autonomy and not busy, allow_requests and not busy)
            self._write([("world_step", {"seconds": seconds, "conversation_active": conversing,
                                         "autonomy": autonomy, "allow_requests": allow_requests})]
                        + [(e["kind"], e["payload"]) for e in events])

    def dialogue_context(self, query):
        with self.lock:
            context = super().dialogue_context(query)
            character = self.experience.context()
            candidate = self.experience.question_candidate(self.world)
            character["allow_followup"] = bool(candidate)
            character["askable_interests"] = [candidate["interest"].model_dump()] if candidate else []
            current = next(reversed(self.turns.values()), {})
            context.update(character=character, virtual_world=self.world.context(),
                           dialogue={"reply_link": current.get("question_reply", {}),
                                     "scene_result": current.get("scene_result"),
                                     "cognition": self.cognition.model_dump(exclude={"processed"})})
            return context

    @atomic
    def dialogue_result(self, turn_id, result):
        turn = self.turns[turn_id]
        if turn.get("experience_processed"):
            return
        result = copy.deepcopy(result)
        interest_id = result.pop("asked_interest", None)
        followup = result.pop("asked_followup", False)
        super().dialogue_result(turn_id, result)
        if interest_id:
            self.experience.asked(interest_id, "chat:"+turn_id)
        elif followup and result.get("followup_text"):
            self.experience.open_question(result["followup_text"], "共有してくれた体験", "相手の体験の続きを知りたい",
                                          action_id="chat:"+turn_id)
            self.experience.state.questions += 1
        if result.get("interest_topic"):
            topic = result["interest_topic"]
            self.experience.interest(topic, f"{topic}を実際に確かめてみたい。", "対話で自分の関心を選んだ",
                                     "user", turn_id, 0.6)
        self.cognition.intention = "ask" if interest_id or followup else "listen"
        self._write()

    def view(self):
        with self.lock:
            value = super().view()
            value["cognition"] = self.cognition.model_dump(exclude={"processed"})
            value["questions"] = [q.model_dump() for q in self.experience.state.dialogue_questions[-20:]]
            return value


inspect_database = Session.inspect_save
