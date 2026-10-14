"""User-taught world knowledge; pretrained weights and assistant replies are not evidence."""

import re

from esi02.memory import Candidate, Memory, normalize


def teaching_candidates(text):
    if re.search(r"[？?]|例えば|たとえば|仮に|もし|かもしれ|という文|[「『\"]", text):
        return []
    result = []
    for sentence in re.split(r"[。\n]", text):
        sentence = sentence.strip()
        sentence = re.sub(r"^(?:覚えて(?:ください|ね)?[、, ]*|教える(?:ね|よ)[、, ]*)", "", sentence)
        match = re.fullmatch(r"(.{1,60}?)(?:というのは|とは|は)(.{1,120}?)(?:です|だよ|だね|である|だ)[！!]?", sentence)
        if not match:
            continue
        subject, value = (part.strip() for part in match.groups())
        if re.match(r"私|僕|ぼく|俺|わたし|あなた|君|きみ|それ|これ|今日|今|ここ", subject):
            continue
        # Explicit replacement keeps the correction's new value, with the full
        # original quote still preserved. No external 'right answer' is applied.
        value = re.split(r"ではなく|じゃなく", value)[-1].strip()
        if subject and value:
            result.append(Candidate(
                kind="knowledge", key="knowledge:" + normalize(subject), label=subject,
                value=value, quote=sentence,
            ))
    return result[:6]


class Knowledge(Memory):
    # Separate tables also let the previous ESI-02 read its personal memories.
    TABLE = "esi02_knowledge"
    PENDING = "esi02_knowledge_pending"
    BLOCKED = "esi02_knowledge_blocked"
    REVISION = "esi02_knowledge_revision"
    HISTORY_FLOOR = "esi02_knowledge_history_floor"
    KINDS = {"knowledge"}

    def context(self, query, limit=8, char_limit=1400):
        # A random unrelated fact must not be mistaken for knowledge of the question.
        relevant = self.search(query, limit=limit)
        if not relevant and re.search(r"覚えたこと|教わったこと|何を覚え|記憶の内容", query):
            relevant = self.entries()[:limit]
        result, size = [], 0
        for item in relevant:
            data = {key: item[key] for key in (
                "id", "kind", "key", "label", "value", "trust", "source", "source_turn_id", "epoch",
            )}
            data["edited_in_ui"] = item.get("edited_in_ui", False)
            size += len(str(data))
            if size > char_limit:
                break
            result.append(data)
        return result
