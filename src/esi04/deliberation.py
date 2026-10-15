"""Source-restricted idle deliberation; outputs propose actions, never new factual evidence."""

import hashlib
import json
from typing import Literal

from pydantic import Field

from esi02.world import Model
from esi04.structured import request


class FocusPlan(Model):
    action: Literal["inspect", "ask", "research", "practice", "rest", "review", "none"]
    target_id: str = Field(max_length=100)
    reason: str = Field(max_length=240)
    alternatives: list[str] = Field(max_length=5)
    emotion: Literal["neutral", "interested", "pleased", "uncertain", "surprised", "tired", "concerned"]


SYSTEM = """ESI-04として、会話がない間に今ある体験を見直し、次にしたい活動を一つ選ぶ。
出力は構造化した行動の候補だけ。知識の説明や事実を新しく作らない。
target_idは提示されたobjects/interests/skillsのIDのみ。rest/review/noneは空文字。
同じ証拠から同じ質問を繰り返さない。相手が忙しい時は質問を控える。
practiceは練習を許可された実演だけ。自分の休息・好奇心・成長と相手の都合を比べる。
reasonは選択の短い説明、alternativesは比べた行動名。詳細な思考の書き起こしは不要。
"""


def context(session, skills=()):
    with session.lock:
        state, world = session.experience.state, session.world
        items = [i for i in state.interests if i.stage in {"open", "paused"}][:6]
        value = {"availability": state.availability, "needs": state.needs.model_dump(),
                 "body": world.body.model_dump(), "values": state.character.values.model_dump(),
                 "interests": [{"id": i.id, "topic": i.topic, "reason": i.reason[:100],
                                "question": i.question[:120], "evidence": i.evidence_ids[:3]} for i in items],
                 "objects": [{"id": o.id, "name": o.label, "kind": o.kind, "inspected": o.inspected}
                             for o in world.objects if o.discovered and o.quantity > 0][:12],
                 "skills": [{"id": s.id, "name": s.name} for s in skills if s.complete and s.practice_allowed][:8],
                 "beliefs": [{"id": b["id"], "label": b["label"], "conflicted": b.get("conflicted", False)}
                             for b in session.knowledge.entries()[:6]]}
        signature_data = {k: v for k, v in value.items() if k not in {"body", "needs"}}
        signature_data["body_band"] = [int(world.body.energy*3), int(world.body.fatigue*3)]
        signature = hashlib.sha256(json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        return "idle-plan:"+signature, value


def deliberate(settings, value, cancellation):
    plan = request(settings, SYSTEM, value, FocusPlan, cancellation, 384)
    allowed = {i["id"] for group in ("interests", "objects", "skills") for i in value[group]}
    if plan.action not in {"none", "review", "rest"} and plan.target_id not in allowed:
        raise ValueError("保存された体験にない対象を選んだので、行動に適用しませんでした。")
    if plan.action in {"ask", "research"} and plan.target_id not in {i["id"] for i in value["interests"]}:
        raise ValueError("実際の関心にない質問・調査は開始しませんでした。")
    if plan.action == "practice" and plan.target_id not in {i["id"] for i in value["skills"]}:
        raise ValueError("練習を許可していない実演は開始しませんでした。")
    if plan.action == "inspect" and plan.target_id not in {i["id"] for i in value["objects"]}:
        raise ValueError("まだ見ていない物体は調べに行きませんでした。")
    return plan
