"""Public utterances and honest work announcements, distinct from private reasoning."""

import copy
import json
import re
from difflib import SequenceMatcher

from esi.models import uid
from esi03.storage import atomic
from esi04.state import Utterance


def simple_reply(text, history, style="precise"):
    """Only unambiguous social signals bypass inference. Never guess what YES means."""
    text = text.strip().rstrip("。！! ")
    casual = style == "casual"
    if re.fullmatch(r"(?:それについて)?(?:教えて|説明して|話して|手伝って|聞いて)(?:くれて|くださって)ありがとう(?:ございます|ございました)?", text):
        return "どういたしまして。"
    phrases = {
        "ありがとう": "どういたしまして。", "ありがとうございます": "どういたしまして。",
        "なるほど": "はい。" if not casual else "うん。",
        "わかりました": "はい。" if not casual else "うん。",
        "分かりました": "はい。" if not casual else "うん。",
        "了解": "はい。" if not casual else "うん。",
        "はい": "はい。" if not casual else "うん。",
        "うん": "はい。" if not casual else "うん。",
        "そうなんですね": "はい。" if not casual else "うん。",
        "こんにちは": "こんにちは。話を聞いています。" if not casual else "こんにちは。聞いているよ。",
        "こんばんは": "こんばんは。" ,
        "おはよう": "おはようございます。" if not casual else "おはよう。",
        "おはようございます": "おはようございます。" if not casual else "おはよう。",
        "おやすみ": "おやすみなさい。", "おやすみなさい": "おやすみなさい。",
    }
    # A bare YES after a question may be ambiguous; leave its meaning to the model.
    if text in {"はい", "うん", "そうなんですね"} and history and history[-1]["role"] == "assistant" and re.search(
            r"[？?]|ですか|ますか|教えて|聞かせて|ください", history[-1]["content"]):
        return None
    return phrases.get(text)


def effort(text):
    """Conservative routing; simple replies never enter the long planning path."""
    if re.search(r"(?:短く|ひとこと|一言|簡潔に|要点だけ)", text):
        return "brief"
    if (re.search(r"じっくり|よく考えて|深く考|時間をかけ|詳しく(?:比較|検討|分析|説明)|長考", text)
            or len(text) >= 420 or len(re.findall(r"[？?]", text)) >= 3
            or re.search(r"(?:比較|両立|計画|設計|分析|整理).*(?:理由|手順|条件|利点|欠点|メリット|デメリット)", text)):
        return "deep"
    return "standard"


def recursive_question(text):
    """Reject the reported question-as-topic wrapper, including nested punctuation."""
    return bool(re.search(r"[？?][」』\"]?について|教えて(?:いただけ|もらえ)ますか.{0,8}について", text))


def work_text(kind, style):
    phrases = {
        "organizing": ("情報を整理しています。", "情報を整理しているよ。"),
        "deliberating": ("少し時間をかけて考えます。", "少し時間をかけて考えるね。"),
        "revising": ("答えを確認しています。", "答えを確認しているよ。"),
        "web_search": ("検索しています。", "検索しているよ。"),
        "read_page": ("資料を読んでいます。", "資料を読んでいるよ。"),
        "fetch": ("資料を読んでいます。", "資料を読んでいるよ。"),
        "read_folder": ("資料を読んでいます。", "資料を読んでいるよ。"),
        "observe": ("画面を確認しています。", "画面を確認しているよ。"),
    }
    pair = phrases.get(kind)
    return pair[style == "casual"] if pair else ""


class SpeakingSession:
    """Mixed ordinary dialogue with durable, assistant-only spontaneous entries."""

    def migrate_speaking(self):
        if self.cognition.speech_revision:
            return
        for question in self.experience.state.dialogue_questions:
            if question.stage in {"pending", "uncertain"}:
                question.stage = "cancelled"
        self.experience.state.active_interest = None
        for item in self.experience.state.interests:
            if item.stage == "asking":
                item.stage = "paused"
        if self.brain.active and self.brain.active.kind.startswith("ask_"):
            self._record_motivation([self.brain._resolve(self.world, None, "cancelled", "通常の自由な発話へ移行")])
        self.cognition.speech_revision = 1
        self._write()

    def can_initiate_speech(self):
        state = self.experience.state
        return (state.preferences.proactive and state.availability != "busy"
                and self.world.elapsed >= state.preferences.quiet_until)

    @atomic
    def utter(self, text, source_key, evidence=()):
        text = text.strip()
        if (not self.can_initiate_speech() or not text or len(text) > 400 or recursive_question(text)
                or "<think>" in text or any(u.source_key == source_key for u in self.cognition.utterances)):
            return None
        def norm(value):
            return re.sub(r"[\W_]", "", value)
        if any(norm(text) == norm(u.text) or (len(norm(text)) >= 10
               and SequenceMatcher(None, norm(text), norm(u.text)).ratio() >= .85)
               for u in self.cognition.utterances[-12:]):
            return None
        entry = Utterance(id=uid(), at=self.world.elapsed, after_epoch=self.epoch, text=text,
                          source_key=source_key[:100], evidence_ids=list(evidence)[:16])
        self.cognition.utterances = (self.cognition.utterances+[entry])[-64:]
        self._archive_utterance(entry)
        self._write([("utterance", {**entry.model_dump(), "is_factual_evidence": False})])
        return entry

    def _archive_utterance(self, entry):
        self.store.db.execute("CREATE TABLE IF NOT EXISTS esi04_utterances "
                              "(id TEXT PRIMARY KEY, after_epoch INTEGER NOT NULL, payload TEXT NOT NULL)")
        self.store.db.execute("INSERT OR REPLACE INTO esi04_utterances VALUES (?,?,?)",
                              (entry.id, entry.after_epoch, entry.model_dump_json()))

    @atomic
    def utterance_delivery(self, identifier, stage, audio=None):
        entry = next((u for u in self.cognition.utterances if u.id == identifier), None)
        if entry:
            entry.stage = stage
            if audio is not None:
                entry.audio = copy.deepcopy(audio)
            self._archive_utterance(entry)
            self._write()

    def history(self, before_id):
        with self.lock:
            before = self.turns[before_id]["epoch"] if before_id in self.turns else self.epoch+1
            floor = max(self.memory.history_floor, self.knowledge.history_floor)
            superseded = {m["source_turn_id"] for m in self.memory.entries(include_old=True)
                          + self.knowledge.entries(include_old=True) if m["status"] == "superseded"}
            pairs = []
            for turn in self.turns.values():
                if turn["epoch"] >= before:
                    break
                if (turn["epoch"] <= floor or turn["id"] in superseded
                        or not turn.get("use_in_dialogue_history", True)):
                    continue
                pairs.append(((turn["epoch"], 0), {"role": "user", "content": turn["input"] or "（音声を聞き取れなかった）"}))
                speech = turn["reply"] if turn["stage"] == "complete" else ""
                if not speech:
                    delivered = "".join(c["text"] for c in turn["chunks"] if c.get("played"))
                    speech = delivered+"（この発声は中断された）" if delivered else ""
                if speech:
                    pairs.append(((turn["epoch"], 1), {"role": "assistant", "content": speech}))
            pairs.extend(((u.after_epoch, 2+n/1000), {"role": "assistant", "content": u.text})
                         for n, u in enumerate(self.cognition.utterances)
                         if (u.after_epoch > floor or floor == 0) and u.after_epoch < before)
            return [m for _, m in sorted(pairs, key=lambda item: item[0])][-self.settings.history_pairs*2:]

    def speech_archive(self, limit=100):
        with self.store.lock:
            exists = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='esi04_utterances'").fetchone()
            if not exists:
                return []
            return [json.loads(row[0]) for row in self.store.db.execute(
                "SELECT payload FROM esi04_utterances ORDER BY rowid DESC LIMIT ?", (limit,))][::-1]
