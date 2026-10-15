"""Event-driven curiosity and attributable answers; silence never means rejection."""

import hashlib
import re

from esi.models import uid
from esi02.memory import grams
from esi03.experience import Experience as PreviousExperience
from esi04.state import ExperienceState, Question

YES = re.compile(r"(?:うん|はい|そう|そうだよ|その通り|そうです|いいよ|合ってる|あってる)[。！!、\s]*")
NO = re.compile(r"(?:いいえ|違う|違います|そうじゃない|いや)[。！!、\s]*")
LATER = re.compile(r"(?:ごめん[、\s]*)?(?:今は|いまは|ちょっと)?(?:忙しい|あとで|後で|話せない|考え中|待って)(?:です|ね|ください)?[。！!\s]*")
DECLINE = re.compile(r"(?:その質問は)?(?:答えたくない|答えられない|質問しないで|話しかけないで)[。！!\s]*")


def reply_relation(text, question):
    text = text.strip()
    if LATER.fullmatch(text):
        return "deferred"
    if DECLINE.fullmatch(text):
        return "declined"
    if not text or re.search(r"[？?]|ですか|ますか|教えて|何が|何か|何を|どうです|興味ある", text):
        return "other"
    if YES.fullmatch(text) or NO.fullmatch(text):
        return "answered"
    shared = grams(text) & grams(question.topic + question.text)
    if len(shared) >= 2:
        return "answered"
    if re.search(r"どうしたら|どうすれば|確かめ|学び方", question.text) and re.search(r"調べ|検索|見比べ|確かめ|試す|試して", text):
        return "answered"
    if re.search(r"いつ|昼|夜|時間", question.text) and re.search(r"昼|夜|朝|夕方|時|いつも", text):
        return "answered"
    if re.search(r"なぜ|どうして|理由", question.text) and re.search(r"から|ので|ため|理由", text):
        return "answered"
    if re.search(r"何色", question.text) and re.fullmatch(r"(?:赤|青|緑|黄色|白|黒|紫|ピンク)(?:だよ|です|色だよ|色です)?[。！!\s]*", text):
        return "answered"
    if re.search(r"私は|僕は|今日は|ところで|話を変|疲れ|困って|置いて|配置して|操作して", text):
        return "other"
    if len(text) <= 160 and re.search(r"とは|って何|は何|について.*教えて", question.text):
        return "uncertain"
    return "other"


class Experience(PreviousExperience):
    outcome_sources = PreviousExperience.outcome_sources | {"computer_observation"}
    state_class = ExperienceState
    initial_question_delay = 0

    def outcome(self, identifier, activity, reward, provenance, source_id, details=None):
        if provenance == "computer_observation" and (
            not isinstance(details, dict) or details.get("id") != source_id
            or details.get("status") != "verified" or not details.get("after") or not details.get("effect_observed")
            or details.get("before") == details.get("after")
        ):
            raise ValueError("PC操作の報酬には、実行後に確認した結果が必要です。")
        return super().outcome(identifier, activity, reward, provenance, source_id, details)

    def context_signature(self, interest):
        # Passage of a fixed interval is not new evidence to repeat a question.
        value = (interest.source_id, tuple(interest.evidence_ids), interest.origin)
        return hashlib.sha256(repr(value).encode()).hexdigest()[:24]

    def pending_question(self):
        return next((q for q in reversed(self.state.dialogue_questions) if q.stage in {"pending", "uncertain"}), None)

    def open_question(self, text, topic, reason, action_id=None, interest_id=None):
        if self.pending_question():
            return None
        question = Question(id=uid(), text=text[:300], topic=topic[:120], reason=reason[:300],
                            interest_id=interest_id, action_id=action_id, asked_at=self.state.elapsed)
        self.state.dialogue_questions = (self.state.dialogue_questions + [question])[-64:]
        return question

    def asked(self, identifier, action_id):
        state = self.state
        item = next(i for i in state.interests if i.id == identifier)
        item.stage, item.last_asked = "asking", state.elapsed
        item.attempts = min(50, item.attempts+1)
        state.last_question = state.elapsed
        state.questions += 1
        state.active_interest = item.id
        state.question_actions = dict(list({**state.question_actions, action_id: identifier}.items())[-64:])
        state.question_contexts = dict(list({**state.question_contexts,
                                          identifier: self.context_signature(item)}.items())[-96:])
        self.open_question(item.question, item.topic, item.reason, action_id, item.id)
        self.trace("question", item.question, action_id, interest_id=item.id, reason=item.reason,
                   timing="compared_with_other_actions", needs=state.needs.model_dump())

    def question_candidate(self, world):
        state = self.state
        if (not state.preferences.proactive or world.conversation_active or self.pending_question()
                or state.availability == "busy" or world.elapsed < state.preferences.quiet_until):
            return None
        if world.body.energy < 0.2 or world.body.fatigue > 0.85:
            return None
        candidates = [i for i in state.interests if i.stage in {"open", "paused"}
                      and state.question_contexts.get(i.id) != self.context_signature(i)]
        if not candidates:
            return None
        item = max(candidates, key=lambda i: i.priority + (0.1 if i.origin == "conflict" else 0))
        need, character = state.needs, state.character
        benefit = (0.42*character.traits.curiosity*need.curiosity + 0.3*character.values.growth*need.growth
                   + 0.12*need.competence + 0.1*need.social)
        interruption = (0.15 if state.availability in {"unknown", "unobserved"} else 0.02)
        cost = interruption + 0.3*need.rest + 0.25*need.comfort
        score = benefit*(0.7+0.3*item.priority)-cost
        return {"interest": item, "score": max(0, score), "reason": item.reason} if score > 0 else None

    def link_reply(self, turn):
        question = self.pending_question() or next(
            (q for q in reversed(self.state.dialogue_questions) if q.stage == "unobserved"), None)
        if question is None:
            return None
        relation = reply_relation(turn["input"], question)
        if question.stage == "uncertain" and YES.fullmatch(turn["input"].strip()):
            return {"question_id": question.id, "relation": "answered", "quote": question.reply_quote,
                    "source_turn": question.reply_turn, "confirmed_by": turn["id"]}
        if question.stage == "uncertain" and NO.fullmatch(turn["input"].strip()):
            return {"question_id": question.id, "relation": "clarification_rejected", "quote": turn["input"],
                    "source_turn": turn["id"]}
        return {"question_id": question.id, "relation": relation, "quote": turn["input"],
                "source_turn": turn["id"]}

    def apply_reply(self, turn, relation):
        if not relation or relation["relation"] == "other":
            return None
        question = next(q for q in self.state.dialogue_questions if q.id == relation["question_id"])
        if question.stage not in {"pending", "uncertain", "unobserved"}:
            return None
        stage = relation["relation"]
        if stage == "clarification_rejected":
            question.stage = "pending"
            question.reply_turn, question.reply_quote = None, ""
            self.trace("question_reply", "別の発言として受け取り、質問への回答にはしなかった", turn["id"],
                       question_id=question.id, relation=stage)
            return question
        question.stage = stage
        question.reply_turn, question.reply_quote = relation["source_turn"], relation["quote"]
        interest = next((i for i in self.state.interests if i.id == question.interest_id), None)
        if interest and stage != "uncertain":
            interest.stage = "resolved" if stage == "answered" else "paused"
            self.state.active_interest = None
        if stage == "answered":
            self.outcome("answer:"+question.id, "question", 0.15, "explicit_user_response", turn["id"],
                         {"question_id": question.id, "quote": question.reply_quote, "relation": stage})
        self.trace("question_reply", "質問と次の発言の対応を保存", turn["id"],
                   question_id=question.id, relation=stage, reply_source=question.reply_turn)
        return question

    def user_turn(self, turn):
        super().user_turn(turn)
        state = self.state
        if (turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition"}
                or not turn.get("use_in_dialogue_history", True) or turn.get("recognition_needs_confirmation")):
            return
        text = turn["input"].strip()
        if LATER.fullmatch(text):
            state.availability = "busy"
        elif re.fullmatch(r"(?:もう|今なら|今は)?(?:話せる|大丈夫|話そう|戻った|ただいま)(?:よ|です)?[。！!\s]*", text):
            state.availability = "present"
            state.preferences.quiet_until = 0
        elif not DECLINE.fullmatch(text):
            state.availability = "present"
            state.preferences.quiet_until = 0
        if turn.get("question_reply"):
            self.apply_reply(turn, turn["question_reply"])

    def finish_question(self, action_id, stage, source_id):
        question = next((q for q in self.state.dialogue_questions if q.action_id == action_id
                         and q.stage in {"pending", "uncertain"}), None)
        if stage not in {"unknown", "cancelled", "declined"}:
            return
        if question:
            question.stage = "unobserved" if stage == "unknown" else stage
            if question.interest_id:
                interest = next(i for i in self.state.interests if i.id == question.interest_id)
                interest.stage = "paused"
            self.state.active_interest = None
            self.state.availability = "unobserved" if stage == "unknown" else self.state.availability
            self.trace("question_result", "返答を観測できないため保留" if stage == "unknown" else "質問を保留",
                       source_id, question_id=question.id, stage=question.stage)

    def recover(self):
        super().recover()
        for question in self.state.dialogue_questions:
            if question.stage in {"pending", "uncertain"}:
                question.stage = "cancelled"

    def context(self):
        result = super().context()
        result.update(pending_question=self.pending_question().model_dump() if self.pending_question() else None,
                      availability=self.state.availability, adaptive_questions=True)
        return result
