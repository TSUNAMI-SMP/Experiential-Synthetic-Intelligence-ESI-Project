"""Source-restricted idle deliberation; outputs propose actions, never new factual evidence."""

import hashlib
import json
from typing import Literal

from pydantic import Field

from esi02.world import Model
from esi04.speaking import recursive_question
from esi04.structured import request


class FocusPlan(Model):
    reason: str = Field(max_length=240)
    alternatives: list[str] = Field(max_length=5)
    action: Literal["speak", "inspect", "research", "practice", "observe_screen", "rest", "review", "none"]
    target_id: str = Field(max_length=100)
    emotion: Literal["neutral", "interested", "pleased", "uncertain", "surprised", "tired", "concerned"]
    utterance: str = Field(default="", max_length=300)


SYSTEM = """ESI-04として、今ある体験・欲求・人格の軸から、会話がない間に次にしたいことを自分で選ぶ。
まずreasonに今したいことの短い説明、alternativesに比べた活動を書く。それからactionを一つ選ぶ。
出力は構造化した次の行動。知識や観測した事実を新しく作らない。
target_idは提示されたobjects/interests/skillsのIDのみ。rest/review/noneは空文字。speakは関連対象がなければ空文字。
speakならutteranceに実際に口にする短い自然な日本語を書く。質問に限らず、発見の共有、独り言、感想、自分の希望も選べる。話さず他の活動をしてもよい。
recent_activityに未共有の新しい気づきがあり、人と話すsocialの欲求や自分の関心が高い時には、その感想をspeakで共有できる。何を話すかは自分で選び、相手の返答を要求しない。
質問は今の話題に自然な具体的な一問だけ。「質問文について教えて」のように文章を対象にしない。自分で考えられる内容を毎回相手に尋ねない。
recent_dialogueの相手と自分を取り違えず、recent_speechと同じ内容は繰り返さない。相手が忙しい時やcan_speakがfalseならspeakを選ばない。
character.styleはpreciseが敬語、gentleがやわらかい敬語、casualだけ常体。身体の数値や処理状況を無関係に報告しない。
speak以外ではutteranceは空文字。質問・お願い専用の手続きや返答ボタンはない。発話を事実の証拠や成長の報酬にはしない。
practiceは練習を許可された実演だけ。observe_screenは提示されたscreenのIDがある時だけ。
自分の休息・好奇心・成長と相手の都合を比べる。
bodily_actions_enabledがfalseなら身体を動かすinspect/restを選ばない。会話の開始はcan_speakに従う。
reasonは選択の短い説明、alternativesは比べた行動名。詳細な思考の書き起こしは不要。
"""


def context(session, skills=(), screen=None):
    with session.lock:
        state, world = session.experience.state, session.world
        items = [i for i in state.interests if i.stage in {"open", "paused"}
                 and i.source_id != "initial:growth"
                 and not any(mark in i.topic for mark in ("？", "?", "教えていただけ", "教えてもらえ"))][:6]
        value = {"availability": state.availability, "needs": state.needs.model_dump(),
                 "can_speak": session.can_initiate_speech(),
                 "bodily_actions_enabled": session.brain.state.enabled,
                 "can_rest": bool(session.brain.state.enabled and world.place("rest")),
                 "can_research": bool(state.preferences.exploration and
                                      (session.research.policy.web_enabled or session.research.policy.read_roots)),
                 "character": {"name": state.character.name, "style": state.character.style,
                               "traits": state.character.traits.model_dump()},
                 "recent_dialogue": session.history("")[-6:],
                 "recent_speech": [u.text for u in session.cognition.utterances[-6:]],
                 "conversation_epoch": session.epoch,
                 "recent_activity": [{"activity": a.activity, "subject": a.subject, "outcome": a.outcome,
                                      "evidence": a.evidence_ids, "certainty": a.certainty}
                                     for a in [a for a in session.cognition.summaries if a.certainty == "observed"][-3:]],
                 "body": world.body.model_dump(), "values": state.character.values.model_dump(),
                 "interests": [{"id": i.id, "topic": i.topic, "reason": i.reason[:100],
                                "evidence": i.evidence_ids[:3]} for i in items],
                 "objects": [{"id": o.id, "name": o.label, "kind": o.kind, "inspected": o.inspected}
                             for o in world.objects if o.discovered and o.quantity > 0][:12],
                 "skills": [{"id": s.id, "name": s.name} for s in skills if s.complete and s.practice_allowed][:8],
                 "beliefs": [{"id": b["id"], "label": b["label"], "conflicted": b.get("conflicted", False)}
                             for b in session.knowledge.entries()[:6]], "screen": screen}
        # Speaking alone must not trigger another deliberation and another utterance.
        signature_data = {k: v for k, v in value.items() if k not in {"body", "needs", "recent_speech", "recent_dialogue"}}
        signature_data["body_band"] = [int(world.body.energy*3), int(world.body.fatigue*3)]
        signature_data["need_bands"] = [int(getattr(state.needs, key)*4)
                                        for key in ("social", "curiosity", "growth", "rest")]
        signature = hashlib.sha256(json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        return "idle-plan:"+signature, value


def deliberate(settings, value, cancellation):
    schema = FocusPlan.model_json_schema()
    available = ["none"]
    if value.get("can_speak"):
        available.insert(0, "speak")
    if value.get("bodily_actions_enabled", True):
        if value.get("can_rest", True):
            available.append("rest")
        if value["objects"]:
            available.append("inspect")
    if value["interests"] and value.get("can_research"):
        available.append("research")
    if value["skills"]:
        available.append("practice")
    if value.get("screen"):
        available.append("observe_screen")
    if value.get("beliefs") or value.get("recent_dialogue"):
        available.append("review")
    schema["properties"]["action"]["enum"] = available
    targets = [i["id"] for group in ("interests", "objects", "skills") for i in value[group]]
    if value.get("screen"):
        targets.append(value["screen"]["id"])
    schema["properties"]["target_id"]["enum"] = ["", *targets]
    plan = request(settings, SYSTEM, value, FocusPlan, cancellation, 384, schema_override=schema)
    if plan.action not in available:
        raise ValueError("現在利用できない活動は開始しませんでした。")
    allowed = {i["id"] for group in ("interests", "objects", "skills") for i in value[group]}
    if value.get("screen"):
        allowed.add(value["screen"]["id"])
    if plan.action not in {"none", "review", "rest", "speak"} and plan.target_id not in allowed:
        raise ValueError("保存された体験にない対象を選んだので、行動に適用しませんでした。")
    if plan.action == "research" and plan.target_id not in {i["id"] for i in value["interests"]}:
        raise ValueError("実際の関心にない質問・調査は開始しませんでした。")
    if plan.action == "practice" and plan.target_id not in {i["id"] for i in value["skills"]}:
        raise ValueError("練習を許可していない実演は開始しませんでした。")
    if plan.action == "inspect" and plan.target_id not in {i["id"] for i in value["objects"]}:
        raise ValueError("まだ見ていない物体は調べに行きませんでした。")
    if plan.action == "observe_screen" and (not value.get("screen") or plan.target_id != value["screen"]["id"]):
        raise ValueError("共有されていない画面は観察しませんでした。")
    if plan.action == "speak" and (not value.get("can_speak") or not plan.utterance.strip()
                                   or recursive_question(plan.utterance)
                                   or (plan.target_id and plan.target_id not in allowed)):
        raise ValueError("現在の話題や発話設定に沿わない発言は送信しませんでした。")
    if plan.action != "speak":
        plan.utterance = ""
    if plan.action in {"none", "review", "rest"}:
        plan.target_id = ""
    return plan
