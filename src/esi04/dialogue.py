"""Conversation uses pretrained knowledge; tool receipts retain their measured source."""

import re

from esi04.conversation import POLICY, Conversation
from esi04.legacy_dialogue import DialogueLLM as ReceiptDialogue
from esi04.legacy_dialogue import compact, intent_of  # noqa: F401 (dormant plugin compatibility)
from esi04.speaking import simple_reply


def polite(text):
    """Style fixed application receipts without changing quoted text or model facts."""
    replacements = {
        "聞いているよ": "聞いています", "聞いているね": "聞いていますね",
        "うん、": "はい、", "どうしたの？": "どうしましたか？",
        "今日は何をして過ごす？": "今日は何をして過ごしますか？",
        "どうしたらいい？": "どうすればよいでしょうか？", "どんなことが気になった？": "どんなことが気になりましたか？",
        "もらえる？": "もらえますか？", "は何？": "は何でしょうか？", "なの？": "なのでしょうか？",
        "教えてもらえる？": "教えてもらえますか？", "合っている？": "合っていますか？",
        "覚えておくね": "覚えておきますね", "受け取るね": "受け取りますね",
        "試してみるね": "試してみますね", "確かめるよ": "確かめます",
        "止まるよ": "止まります", "できるよ": "できます", "読み取れなかったよ": "読み取れませんでした",
        "見えるよ": "見えます", "教わったよ": "教わりました", "停止したよ": "停止しました",
        "試せるよ": "試せます", "話せるよ": "話せます", "おかえり。": "おかえりなさい。",
        "選べるよ": "選べます", "確かめたいな": "確かめたいです",
        "って何？": "とは何でしょうか？", "あるの？": "ありますか？", "どう思う？": "どう思いますか？",
        "停止しているよ": "停止しています", "覚えたよ": "覚えました", "分かった": "分かりました",
        "待って、自分でできることを考えているね": "待って、自分でできることを考えていますね",
        "保留にするね": "保留にしますね", "知らない": "知りません", "気になるよ": "気になります",
        "なんだね": "のですね", "という話だね": "というお話ですね", "なんだ": "のです",
        "だよ": "です", "だね": "ですね", "したいな": "したいです", "欲しいな": "欲しいです",
        "見たいな": "見たいです", "聞きたいな": "聞きたいです", "止めるね": "止めますね",
        "おはよう。": "おはようございます。", "ありがとう。": "ありがとうございます。",
    }
    pieces = re.split(r"(「[^」]*」)", text)
    for i in range(0, len(pieces), 2):
        value = pieces[i]
        for source, target in replacements.items():
            value = value.replace(source, target)
        for source, target in {"置いたよ": "置きました", "移動したよ": "移動しました",
                               "取り除いたよ": "取り除きました", "食べたよ": "食べました",
                               "確かめたよ": "確かめました", "使ったよ": "使いました", "遊んだよ": "遊びました"}.items():
            value = value.replace(source, target)
        pieces[i] = value
    return "".join(pieces)


def polite_endings(text):
    pieces = re.split(r'(「[^」]*」|『[^』]*』|"[^"]*")', text)
    for i in range(0, len(pieces), 2):
        pieces[i] = re.sub(r"ありがとう(?=[。！？!?、,\s]|$)", "ありがとうございます", pieces[i])
        endings = {"感じた": "感じました", "思った": "思いました", "考えた": "考えました",
                   "覚えた": "覚えました", "分かった": "分かりました", "わかった": "わかりました",
                   "感じる": "感じます", "思う": "思います", "考える": "考えます",
                   "分からない": "分かりません", "わからない": "わかりません",
                   "重要だ": "重要です", "大切だ": "大切です", "必要だ": "必要です",
                   "好きだ": "好きです", "不安だ": "不安です"}
        for source, target in endings.items():
            pieces[i] = re.sub(re.escape(source)+r"(?=[。！？!?]|$)", target, pieces[i])
    return "".join(pieces)


class DialogueLLM(ReceiptDialogue):
    def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
        context = {**(context or {}), "user_text": text}
        progress = context.pop("_progress", lambda kind: None)
        dialogue = context.get("dialogue", {})
        # Command receipts describe actual application results, never a model's claimed success.
        receipt = (any(dialogue.get(k) for k in ("scene_result", "screen", "screen_error", "computer_request",
                   "computer_stopped", "plugin_request")) or context.get("environment", {}).get("active"))
        # Only unambiguous greetings and application receipts bypass model inference.
        # The legacy natural_dialogue=False setting no longer imposes a knowledge limit.
        immediate = simple_reply(text, history, context.get("character", {}).get("style", "precise"))
        if immediate and not receipt:
            return self._emit_answer(immediate, cancellation, on_delta, on_sentence,
                inference_performed=False, request={"knowledge_policy": POLICY},
                intent="brief_conversation", response_effort="brief",
                affect_appraisal={"emotion": "pleased" if "ありがとう" in text else "neutral",
                                  "reason": "短い挨拶や応答を受け取った", "recovering": "ありがとう" in text},
                grounding={"policy": POLICY, "refs": [], "model_output_is_evidence": False})
        if receipt or intent_of(text, context) == "calling":
            style = context.get("character", {}).get("style", "precise")
            def delta(chunk):
                on_delta(polite(chunk) if style != "casual" else chunk)
            def sentence(chunk):
                on_sentence(polite(chunk) if style != "casual" else chunk)
            result = super().stream(text, history, cancellation, delta, sentence, context)
            if style != "casual":
                result["text"] = polite(result["text"])
            result["request"]["knowledge_policy"] = POLICY
            return result
        engine = Conversation(self.settings, self.client_factory)
        reply, request = engine.generate(text, history, cancellation, {**context, "_progress": progress})
        candidate = dialogue.get("question_candidate")
        binding = None
        if candidate and reply.reply_relation != "none":
            binding = {"question_id": candidate["id"], "relation": reply.reply_relation, "quote": text,
                       "answer_quote": reply.answer_quote}
        followup = reply.followup.strip()
        answer = reply.answer.strip()
        if context.get("character", {}).get("style", "precise") != "casual":
            answer, followup = polite_endings(answer), polite_endings(followup)
        if not context.get("character", {}).get("allow_followup") and not reply.needs_clarification:
            followup = ""
        if candidate and reply.reply_relation not in {"answered", "declined", "deferred", "other"}:
            if not reply.needs_clarification:
                followup = ""
        if followup and followup not in answer:
            answer += followup
        return self._emit_answer(answer, cancellation, on_delta, on_sentence,
            inference_performed=True, request={**request, "knowledge_policy": POLICY},
            intent="conversation", grounding={"policy": POLICY, "refs": [], "model_output_is_evidence": False},
            response_effort=reply.response_effort,
            question_binding=binding, asked_followup=bool(followup), followup_text=followup,
            affect_appraisal={"emotion": reply.emotion, "reason": reply.emotion_reason, "recovering": reply.recovering})
