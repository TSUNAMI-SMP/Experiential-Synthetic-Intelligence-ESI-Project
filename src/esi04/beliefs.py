"""Question-and-answer provenance may establish a subject omitted in a short reply."""

import re

from esi02.memory import Candidate, normalize
from esi03.beliefs import Beliefs as PreviousBeliefs


class Beliefs(PreviousBeliefs):
    def from_answer(self, question, turn):
        if question.stage != "answered" or question.reply_turn != turn["id"]:
            raise ValueError("実際の質問と回答が結び付いていません。")
        if (turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition"}
                or not turn.get("use_in_dialogue_history", True) or turn.get("recognition_needs_confirmation")):
            return False
        if (not question.reply_quote or question.reply_quote not in turn["input"]
                or question.topic not in question.text or turn["started_elapsed"] < question.asked_at):
            raise ValueError("回答の出典か、質問の対象・時系列を確認できません。")
        if re.search(r"[？?]|もし|例えば|仮に|という文", question.reply_quote):
            return False
        item = Candidate(kind="knowledge", key="knowledge:"+normalize(question.topic[:60]),
                         label=question.topic[:60], value=question.reply_quote[:120],
                         quote=question.reply_quote[:500], basis="explicit")
        evidence = {"id": turn["id"], "source": turn["source"], "quote": item.quote,
                    "at": turn["created_at"], "basis": "contextual_answer", "url": None,
                    "question_id": question.id, "question": question.text}
        with self.store.lock, self.store.db:
            if turn["epoch"] <= self.history_floor or self.store.db.execute(
                f"SELECT 1 FROM {self.BLOCKED} WHERE turn_id=?", (turn["id"],)
            ).fetchone():
                return False
            changed = self._observe(item, evidence, turn["epoch"], "question-linked-user-answer", source_weight=0.55)
            if changed:
                self._revision()
            return changed
