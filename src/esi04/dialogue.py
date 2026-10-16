"""Meaningful response frames; factual values still come from cited observations."""

import re
from typing import Literal

from pydantic import Field

from esi02.grounding import Decision, relevant, render
from esi03.dialogue import DISCOURSE, PROMPT, Plan, alternate, compact_context, grounded_fact, unknown_topic
from esi03.dialogue import DialogueLLM as PreviousDialogue
from esi04.environment_dialogue import speak as environment_speak
from esi04.environment_life import observation_facts
from esi04.minecraft import answer as environment_answer


class ResponsePlan(Plan):
    intent: Literal["chat", "teaching", "recall", "self_interest", "self_desire", "comfort", "proposal",
                    "answer", "recognition_problem", "clarify"]
    answer_to: str = Field(max_length=64)


EXTRA = re.compile(
    r"(?:そういうことなんだね|そういうことなんですね|なるほどね|聞いているよ|聞いています|"
    r"もう少し詳しく聞きたい|もう少し詳しく聞きたいです|その方法を試してみたい|その方法を試してみたいです|"
    r"自分でも調べてみたい|自分でも調べてみたいです|それは気になるね|それは気になります|"
    r"そう教えてくれたんだね|そう教えてくれたんですね|少し迷っている|少し迷っています|"
    r"今の話を確かめたい|今の話を確かめたいです|考えを整理したい|考えを整理したいです|"
    r"自分で選んでみたい|自分で選んでみたいです|まだ説明しきれない|まだ説明しきれません|"
    r"言い直してくれてありがとう|一緒に確かめたい|一緒に確かめたいです)[。！!\s]*"
)


def checked_expression(text, context):
    if not text:
        return ""
    # Quoted spans can be echoed, but cannot turn into uncited new statements.
    allowed = [context.get("user_text", "")]
    allowed += [i["label"]+"\n"+i["value"] for i in context.get("knowledge", [])+context.get("memories", [])]
    def quote(match):
        value = match.group(1)
        return "そのこと" if value and any(value in source for source in allowed) else "\x00"
    probe = re.sub(r"「([^「」]{1,100})」", quote, text)
    if "\x00" in probe or re.search(r"https?://|何かお手伝い|システムプロンプト|<think>", probe):
        return ""
    sentences = re.findall(r"[^。！!]+[。！!]?", probe)
    if sentences and all(DISCOURSE.fullmatch(s.strip()) or EXTRA.fullmatch(s.strip()) for s in sentences):
        return text if text[-1] in "。！!" else text+"。"
    return ""


def compact(context):
    result = compact_context(context)
    result.update(dialogue=context.get("dialogue", {}), intent_hint=context.get("intent_hint"),
                  body=context.get("virtual_world", {}).get("body", {}))
    result["character"]["pending_question"] = context.get("character", {}).get("pending_question")
    environment = context.get("environment", {})
    if environment.get("active"):
        result["environment"] = {"connected": environment.get("connected"),
            "life": environment.get("life", {}), "facts": observation_facts(environment),
            "activity": environment.get("activity_reason", "")}
    return result


def intent_of(text, context):
    if re.fullmatch(r"(?:こんにちは|こんばんは|おはよう|やあ|ただいま|おかえり|ありがとう)(?:ございます|ね)?[。！!\s]*", text):
        return "greeting"
    if re.search(r"(?:今|いま|何を|何|なに).*(?:考えて|考え中|してる|している)|気持ちは|どんな気持ち", text):
        return "self_activity"
    query = bool(re.search(r"[？?]|ですか|ますか|ある|どう|どんな|何|なに|教えて", text))
    if re.search(r"(?:何か|何が|何を|なにか|なにが).*(?:欲しい|ほしい|したい)|欲しいもの|やりたいこと|成長の目標", text):
        return "self_desire"
    if re.search(r"(?:興味|気になる|好き|楽しい).*(?:ある|あるの|ですか|[？?])|(?:あなた|君|エル).*(?:興味|関心)", text):
        return "self_interest"
    if query and not re.search(r"(?:私|僕|わたし)(?:は|の)", text) and re.search(
        r"居心地|いごごち|いごこち|体調|調子|元気|疲れ|暑い|寒い", text
    ):
        return "comfort"
    if re.search(r"(?:活動エネルギー|疲労|水分|体温|部屋の温度).*(?:何|いくつ|数値|教えて|[？?])|(?:数値|何[%％]).*(?:体|状態|疲労|エネルギー)", text):
        return "metrics"
    if re.search(r"(?:音声認識|聞き取り|聞き取|認識).*(?:違|うまく|でき|失敗|間違)", text):
        return "recognition_problem"
    if re.search(r"(?:調べ|検索|学|確かめ|試し).*(?:方が|ほうが|良い|いい|してみて|すると|できるよ)|(?:ネット|検索|見比べ).*(?:方法|手段|良い|いい)", text):
        return "proposal"
    if context.get("dialogue", {}).get("reply_link", {}).get("relation") in {"answered", "uncertain", "deferred", "declined", "clarification_rejected"}:
        return "answer"
    if re.search(r"(?:あなた|君|きみ|AI|ESI|エル).*(?:名前|誰|なまえ)|名前.*(?:あなた|君)", text):
        return "identity"
    if re.fullmatch(r"(?:ねえ[、\s]*)?(?:エル|凛音エル|ESI)(?:[・、\s]*(?:エル|ESI))*[。！!\s]*", text):
        return "calling"
    if re.search(r"\d{1,6}\s*(?:足す|たす|引く|ひく|[+＋−-])\s*\d{1,6}", text):
        return "calculation"
    items = [m for m in context.get("knowledge", [])+context.get("memories", []) if relevant(m, text)]
    if items:
        return "teaching" if any(m.get("source_turn_id") == context.get("current_turn_id") for m in items) else "recall"
    if re.search(r"とは|って何|は何|どこ|誰|意味|について.*(?:教えて|説明)|を知って|知ってる|[？?]$", text):
        return "unknown"
    return "chat"


def self_desire(context):
    if context.get("environment", {}).get("active"):
        return environment_answer("何がしたい？", context["environment"])
    character, world = context.get("character", {}), context.get("virtual_world", {})
    need, body = character.get("needs", {}), world.get("body", {})
    if body.get("hydration", 1) < 0.4:
        return "今は水を飲みたい。身体を落ち着けてから、気になることの続きを考えたいな。"
    if need.get("rest", 0) > 0.6 or body.get("energy", 1) < 0.3:
        return "今は少し休みたい。自分の身体も大切にして、そのあとで続きを調べたいな。"
    objects = [o for o in world.get("objects", []) if o.get("discovered") and o.get("quantity", 0) > 0]
    snack = next((o for o in objects if o["kind"] == "snack"), None)
    if snack and need.get("pleasure", 0) >= 0.2:
        return f"そこにある「{snack['label']}」が気になる。近くで見て、どんなものか確かめてみたいな。"
    interests = character.get("interests", [])
    if interests:
        return f"今は「{interests[0]['topic']}」をもっと知りたい。自分でも確かめて、少しずつできることを増やしたいな。"
    return "気になるものを自分で見つけて、試してみたい。自分のペースで学べる時間が欲しいな。"


def comfort(context, metrics=False, text=""):
    if context.get("environment", {}).get("active"):
        return environment_answer(text, context["environment"]) or environment_answer("体調はどう？", context["environment"])
    world = context.get("virtual_world", {})
    body, stimulus = world.get("body", {}), world.get("stimulus", {})
    if metrics:
        if "温度" in text:
            return f"部屋の温度は{stimulus.get('temperature', 23):.1f}度だよ。"
        return f"今の活動エネルギーは{body.get('energy', 0):.0%}、疲労は{body.get('fatigue', 0):.0%}だよ。"
    if stimulus.get("thermal_discomfort", 0) > 0.2:
        return "今は少し暑く感じる状態なんだ。" if stimulus.get("temperature", 23) > 23 else "今は少し寒く感じる状態なんだ。"
    if stimulus.get("noise_discomfort", 0) > 0.2:
        return "今は少し騒がしくて、落ち着きにくい状態だよ。静かなところで考えたいな。"
    if body.get("fatigue", 0) > 0.6:
        return "部屋の居心地は悪くないけれど、今は少し疲れているから休みたいな。"
    return "今は落ち着いて過ごせているよ。部屋を見たり、気になることを考えたりできそう。"


class DialogueLLM(PreviousDialogue):
    plan_class = ResponsePlan
    context_compactor = staticmethod(compact)
    plan_prompt = PROMPT.replace("ESI-03", "ESI-04") + """
intentには、相手の発言の意味に合う種類を指定する。自己の興味・欲求の質問はself_interest/self_desire、
居心地の質問はcomfort、学び方などの提案はproposal、認識の不調はrecognition_problem、
直前の質問への答えはanswer、知識の説明はrecall、教えはteaching、その他の対話はchat。
「仮想」という単語だけで身体報告にしない。answer_toは対応する保存済み質問ID、他は空文字。
opening/reflectionは短い相づち、気になる・試してみたい・迷っている等の意図を自然に組み合わせる。
intent_hintがchatなら、相手の体験の話として受け止める。「私は〜」は相手の話。
相手が体験を話しただけなら、自分の欲求や身体の状態に話題を変えない。
尋ねたいことはopeningに書かず、questionとinterest_idで選ぶ。
"""

    def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
        context = {**(context or {}), "user_text": text}
        character = context.get("character", {})
        intent = intent_of(text, context)
        link = context.get("dialogue", {}).get("reply_link", {})
        metadata = {"inference_performed": False, "intent": intent,
                    "grounding": {"policy": "experience_only_v3", "refs": []},
                    "request": {"knowledge_policy": "experience_only_v3"}}
        def emit(answer, **extra):
            return self._emit_answer(answer, cancellation, on_delta, on_sentence, **(metadata | extra))
        dialogue = context.get("dialogue", {})
        environment = context.get("environment", {})
        if dialogue.get("plugin_request"):
            return emit("Minecraftでの自律活動を停止したよ。" if dialogue["plugin_request"] == "Minecraftでの自律活動を停止した" else
                        "Minecraftで、そのことを目標にして試してみるね。結果は実際の世界で確かめるよ。"
                        if environment.get("autonomous") else "目標を覚えたよ。Minecraftの自律活動は今は停止しているよ。", intent="environment")
        answer = environment_answer(text, environment)
        if answer:
            source = (environment.get("observation") or {}).get("id")
            return emit(answer, intent="environment", grounding={"policy": "game_observation", "refs": [source] if source else []})
        if dialogue.get("computer_stopped"):
            return emit("画面の観察とPC操作を停止したよ。")
        if dialogue.get("screen_error"):
            return emit("今の画面を読み取れなかったよ。"+dialogue["screen_error"], intent="screen_error")
        if dialogue.get("screen"):
            observation = dialogue["screen"]
            result = observation["result"]
            words = result.get("visible_text", [])[:8]
            answer = "画面では、"+"、".join(f"「{w[:100]}」" for w in words)+"という文字が見えるよ。" if words else "画像からは、"+result["description"][:900]
            if result.get("uncertain"):
                answer += "細かい部分はまだ確信がないので、一緒に確かめたいな。"
            return emit(answer, intent="screen", grounding={"policy": "visual_inference", "refs": [observation["id"]]})
        if dialogue.get("screen_teachings") and re.search(r"覚えて|教えた|説明|何だった|どれだった", text):
            entry = dialogue["screen_teachings"][0]
            return emit(f"あの画面では、「{entry['user_text'][:700]}」と教わったよ。",
                        intent="screen_recall", grounding={"policy": "user_explanation_of_screen", "refs": [entry["id"], entry["frame_id"]]})
        if dialogue.get("computer_request"):
            return emit("画面を確かめながら操作してみるね。途中で対象や結果が分からなくなったら止まるよ。" if dialogue.get("computer_available")
                        else "PC操作は今は停止しているよ。画面・PC操作タブで許可すると、画面を確かめながら試せるよ。", intent="computer")
        if environment.get("active") and intent not in {"greeting", "identity", "calling", "calculation", "recognition_problem"}:
            answer, extra = environment_speak(self.settings, text, history, context, cancellation)
            return emit(answer, **extra)
        if intent == "greeting":
            return emit("おはよう。今日は何をして過ごす？" if "おはよう" in text else "おかえり。話せるときに続きを聞きたいな。" if "ただいま" in text
                        else "うん、こちらこそ。" if "ありがとう" in text else "こんばんは。今は落ち着いて話せるよ。" if "こんばんは" in text else "こんにちは。話を聞いているよ。")
        if intent == "self_activity":
            cognition = dialogue.get("cognition", {})
            summaries = cognition.get("summaries", [])
            if summaries:
                summary = summaries[-1]
                return emit(f"今は「{summary['subject']}」について、{summary['activity']}ところだよ。"+summary["reason"][:180])
            return emit("今は、次に何を知ったり試したりしたいか考えているよ。")
        if context.get("dialogue", {}).get("scene_result"):
            result = context["dialogue"]["scene_result"]
            if result.get("error"):
                return emit(result["error"], intent="scene_command")
            obj = result.get("object")
            operation = result.get("operation", "place")
            if obj:
                phrase = {"place": "空間に置いた", "move": "移動した", "remove": "取り除いた", "eat": "一個食べた",
                          "inspect": "近くで確かめた", "use": "使った", "play": "使って遊んだ"}.get(operation, "操作した")
                return emit(f"「{obj['label']}」を{phrase}よ。", intent="scene_command")
            if operation == "remove":
                return emit("指定した物を空間から取り除いたよ。", intent="scene_command")
        if intent == "metrics":
            return emit(comfort(context, True, text))
        if intent == "comfort":
            return emit(comfort(context))
        if intent == "self_desire":
            return emit(self_desire(context))
        if intent == "self_interest":
            topic = re.split(r"(?:には|に|は|って)?(?:興味|関心|気になる|好き)", text, maxsplit=1)[0].strip("。？?、 ")
            if not topic or topic in {"何か", "あなた", "エル"}:
                return emit(self_desire(context))
            traits = character.get("traits", {})
            interests = character.get("interests", [])
            known = any(relevant(i, topic) for i in context.get("knowledge", []))
            interested = traits.get("curiosity", 0.5) > 0.35 or any(topic in i["topic"] for i in interests)
            answer = f"うん、「{topic}」は気になるよ。どんなものか、自分でも確かめてみたいな。" if interested else f"「{topic}」は、今は少し様子を見ながら知っていきたいな。"
            return emit(answer, interest_topic=topic[:100], known_topic=known)
        if intent == "identity":
            return emit(f"私は{character.get('name', '凛音エル')}。ESI-04として、自分で考えて経験から学んでいるよ。")
        if intent == "calling":
            return emit("うん、聞いているよ。どうしたの？")
        if intent == "calculation":
            return emit(render(Decision(intent="calculation", refs=[], phrase="none"), text, context, history))
        if intent == "recognition_problem":
            answer = "聞き取りがうまくいかなかったんだね。言葉を確かめながら話したい。"
            if re.search(r"ネット|検索|調べ", text):
                answer += "知識はネットでも調べて確かめる、という提案も受け取ったよ。"
            return emit(answer)
        if intent == "proposal":
            return emit("ネットで調べて確かめる、という方法だね。自分の疑問を調べるときに試してみたい。" if re.search(r"ネット|検索|調べ", text) else "その方法を試してみたい。実際にできたかも確かめながら覚えたいな。")
        if intent == "answer":
            relation = link.get("relation")
            if relation == "uncertain":
                return emit("今の言葉は、さっき私が尋ねたことへの答え、という受け取り方で合っている？")
            if relation == "deferred":
                return emit("分かった。今は待って、自分でできることを考えているね。")
            if relation == "declined":
                return emit("分かった。その質問はここで保留にするね。")
            if relation == "clarification_rejected":
                return emit("分かった。今の発言は、別の話として受け取るね。")
            return emit("教えてくれてありがとう。さっき気になって尋ねたことへの答えとして覚えておくね。")
        if intent == "unknown":
            return emit(alternate(("そのことはまだ知らない。教えてもらえる？", "まだ自分では説明できない。知っていきたいな。"), history), unknown_topic=unknown_topic(text))
        available = {i["id"]: i for i in context.get("knowledge", [])+context.get("memories", []) if relevant(i, text)}
        acknowledgement = "教えてくれてありがとう" if intent == "teaching" else "そうなんだ"
        if intent == "chat" and re.match(r"(?:私|僕|わたし)は", text) and not re.search(r"[？?]", text):
            detail = re.sub(r"^(?:私|僕|わたし)は", "", text).strip("。！! ")[:100]
            acknowledgement = f"「{detail}」という話だね"
        fallback = ResponsePlan(opening=acknowledgement,
                                reflection="", refs=list(available)[:3], question="none", interest_id="",
                                intent=intent, answer_to="")
        context["intent_hint"] = intent
        plan, model = self._plan(text, history, cancellation, context, fallback) if self.settings.natural_dialogue else (fallback, {})
        metadata.update(model)
        pending = character.get("pending_question")
        if (pending and plan.answer_to == pending["id"] and plan.intent == "answer"
                and link.get("relation") == "other" and not re.search(r"[？?]|教えて|何|どこ|誰", text)):
            return emit("今の言葉は、さっき私が尋ねたことへの答え、という受け取り方で合っている？",
                        question_binding={"question_id": pending["id"], "relation": "uncertain", "quote": text})
        # A model-selected self-intent must not redirect a story about the user to ESI's body or desires.
        opening = checked_expression(plan.opening, context)
        reflection = checked_expression(plan.reflection, context)
        selected = [available[i] for i in dict.fromkeys(plan.refs) if i in available]
        selected = selected or list(available.values())[:3]
        facts = "".join(grounded_fact(i, character.get("style", "casual")) for i in selected)
        if intent == "teaching" and facts:
            # Acknowledgement is sufficient during teaching; save retrieval for actual recall.
            facts = ""
        answer = (opening or fallback.opening+"。") + facts + reflection
        followup, interest_id = "", None
        if character.get("allow_followup") and not character.get("pending_question"):
            item = next((i for i in character.get("askable_interests", []) if i["id"] == plan.interest_id), None)
            if plan.question == "interest" and item:
                followup, interest_id = item["question"], item["id"]
            elif plan.question == "user_detail" and intent == "chat":
                followup = "その中で、どんなことが気になった？"
        metadata.update(grounding={"policy": "experience_only_v3", "refs": [i["id"] for i in selected],
                                   "checked_discourse": True}, asked_interest=interest_id,
                        asked_followup=bool(followup), followup_text=followup,
                        rejected_discourse=[k for k, v in (("opening", opening), ("reflection", reflection))
                                            if getattr(plan, k) and not v])
        return emit((answer+followup)[:1800])
