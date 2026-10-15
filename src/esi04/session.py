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
from esi04.computer_models import ScreenTeaching
from esi04.config import Settings
from esi04.experience import YES, Experience
from esi04.motivation import Brain
from esi04.state import Cognition, ExperienceState, Preferences
from esi04.vision import Observation as ScreenObservation
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
            cognition = self.cognition.model_dump(exclude={"processed", "screen_teachings", "last_screen"})
            cognition["summaries"] = cognition["summaries"][-2:]
            context.update(character=character, virtual_world=self.world.context(),
                           dialogue={"reply_link": current.get("question_reply", {}),
                                     "scene_result": current.get("scene_result"),
                                     "cognition": cognition,
                                     "screen": current.get("screen_observation"),
                                     "screen_error": current.get("screen_error"),
                                     "computer_request": current.get("computer_request"),
                                     "computer_available": self.status.get("computer_access", False)})
            return context

    @atomic
    def screen_observed(self, result, turn_id=None):
        observation = ScreenObservation.model_validate(result["observation"])
        self.cognition.last_screen = observation
        self.cognition.intention = "inspect"
        self.cognition.attention = "共有された画面"
        self.cognition.emotion, self.cognition.emotion_strength = "interested", 0.65
        if turn_id:
            self.turns[turn_id]["screen_observation"] = observation.model_dump()
        self.agency.summarize("画面を観察", observation.question, "許可された画面の画像を読み取った",
                              [observation.frame_id, observation.id], outcome=observation.result.description[:400],
                              certainty="inferred")
        self._write([("screen_observation", observation.model_dump())])

    @atomic
    def screen_taught(self, value):
        entry = ScreenTeaching.model_validate(value)
        if any(i.id == entry.id for i in self.cognition.screen_teachings):
            return
        self.cognition.screen_teachings = (self.cognition.screen_teachings+[entry])[-64:]
        self.agency.summarize("画面の説明を教わる", entry.user_text[:160], "相手の説明と、その時の画像を対応付けた",
                              [entry.id, entry.frame_id], outcome="相手から教わった説明として保持", certainty="observed")
        self._write([("screen_teaching", entry.model_dump())])

    @atomic
    def apply_focus(self, key, plan):
        if key in self.cognition.processed:
            return False
        self.agency.remember_processed(key)
        self.cognition.deliberations += 1
        self.cognition.emotion = plan.emotion
        self.cognition.intention = "think"
        self.agency.summarize("次の活動を考える", plan.action, plan.reason, [plan.target_id] if plan.target_id else [],
                              alternatives=plan.alternatives, certainty="inferred")
        if plan.action == "inspect":
            obj = next((o for o in self.world.objects if o.id == plan.target_id and o.discovered and o.quantity > 0), None)
            if obj and not self.brain.active and not self.cognition.active_object:
                self.cognition.active_object, self.cognition.object_action = obj.id, "inspect"
                self.cognition.object_started = self.world.elapsed
                self.cognition.attention_object, self.cognition.attention = obj.id, obj.label
                self.cognition.intention = "inspect"
        elif plan.action == "ask":
            interest = next((i for i in self.experience.state.interests if i.id == plan.target_id), None)
            if interest and self.experience.state.availability != "busy":
                interest.priority = max(0.95, interest.priority)
                self.brain.state.next_decision = self.world.elapsed
        elif plan.action == "rest" and not self.brain.active:
            self.brain._begin(self.world, "rest", False)
            self.cognition.intention = "rest"
        self._write([("idle_decision", {"context": key, "plan": plan.model_dump(), "is_factual_evidence": False})])
        return True

    @atomic
    def computer_finished(self, result):
        self.cognition.computer_result = str(result.get("summary", ""))[:1000]
        self.cognition.pending_computer_task = ""
        self.cognition.intention = "think"
        self.agency.summarize("PC操作の結果を確認", str(result.get("goal", "実演の再現")),
                              "入力後の画面と、達成条件の確認を区別した", outcome=self.cognition.computer_result,
                              evidence=[r["id"] for r in result.get("results", [])][-8:],
                              certainty="observed" if result.get("stage") == "verified" else "inferred")
        # No reward for a language-model declaration or an unverified screen change.
        for record in result.get("results", []):
            if record.get("status") == "verified" and record.get("effect_observed"):
                self.experience.outcome("computer:"+record["id"], "explore", 0.1,
                                        "computer_observation", record["id"], record)
        self._write([("computer_result", result)])

    @atomic
    def dialogue_result(self, turn_id, result):
        turn = self.turns[turn_id]
        if turn.get("experience_processed"):
            return
        result = copy.deepcopy(result)
        binding = result.pop("question_binding", None)
        pending = self.experience.pending_question()
        if (binding and pending and binding.get("question_id") == pending.id
                and binding.get("relation") == "uncertain" and binding.get("quote") == turn["input"]
                and not turn.get("recognition_needs_confirmation") and turn.get("use_in_dialogue_history", True)):
            relation = {**binding, "source_turn": turn_id}
            turn["question_reply"] = relation
            self.experience.apply_reply(turn, relation)
            self._persist(turn)
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
