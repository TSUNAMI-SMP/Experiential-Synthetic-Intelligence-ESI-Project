"""Choose observable actions and reflect on sources without rewarding generated claims."""

import hashlib

from esi.models import uid
from esi04.state import Summary


class Agency:
    def __init__(self, session):
        self.session = session

    def summarize(self, activity, subject, reason, evidence=(), alternatives=(), outcome="", certainty="planned"):
        session = self.session
        entry = Summary(id=uid(), at=session.world.elapsed, activity=activity, subject=subject[:160],
                        reason=reason[:400], evidence_ids=list(evidence)[:24], alternatives=list(alternatives)[:8],
                        outcome=outcome[:400], certainty=certainty)
        session.cognition.summaries = (session.cognition.summaries+[entry])[-128:]
        session.cognition.activity = activity
        return entry

    def remember_processed(self, key):
        self.session.cognition.processed = (self.session.cognition.processed+[key])[-512:]

    def observe(self):
        session = self.session
        for identifier in session.world.discover():
            item = session.world.get(identifier)
            self.summarize("発見", item.label, "近くの空間に、まだ見ていなかった物がある", ["world:"+identifier],
                           outcome=f"{item.label}を視野内で確認", certainty="observed")
            session.experience.state.last_novelty = session.world.elapsed
            if item.kind not in {"bed", "water", "window", "desk"}:
                session.experience.interest(item.label, f"{item.label}はどんなふうに使えるのかな？",
                                            "空間で見つけた物を自分で確かめたい", "world", "world:"+identifier, 0.6)
                session.cognition.emotion = "interested"
                session.cognition.emotion_strength = 0.65

    def advance_object_action(self):
        session, cognition = self.session, self.session.cognition
        if not cognition.active_object:
            return False
        item = next((o for o in session.world.objects if o.id == cognition.active_object), None)
        if item is None or item.quantity <= 0:
            self.summarize("再検討", "物体への行動", "対象が移動・撤去・消費されたので計画を取り消した")
            cognition.active_object = cognition.object_action = None
            return False
        if session.world.destination:
            return True
        if not session.world.near_object(item.id, 0.9):
            try:
                session.world.move_to(session.world.approach(item).model_dump())
            except ValueError as exc:
                self.summarize("再検討", item.label, str(exc), certainty="observed")
                self.remember_processed(f"blocked:{item.id}:{session.world.revision}")
                cognition.active_object = cognition.object_action = None
                return False
            return True
        duration = {"inspect": 1.5, "eat": 3.0, "play": 6.0, "use": 3.0}[cognition.object_action]
        if session.world.elapsed-cognition.object_started < duration:
            return True
        source = "object-action:"+uid()
        action = cognition.object_action
        cognition.active_object = cognition.object_action = None
        session.scene_operation(action, {"id": item.id}, source)
        return False

    def choose_object_action(self):
        session, cognition = self.session, self.session.cognition
        world, state = session.world, session.experience.state
        if (not state.preferences.idle_activity or world.conversation_active or not session.brain.state.enabled
                or session.brain.active or world.destination or world.body.fatigue > 0.65 or world.body.energy < 0.25):
            return False
        candidates = []
        for item in world.objects:
            if not item.discovered or item.quantity <= 0 or f"blocked:{item.id}:{world.revision}" in cognition.processed:
                continue
            if not item.inspected:
                candidates.append((0.25+0.35*state.needs.curiosity, item, "inspect", "まだ使い方を確かめていない"))
            elif item.kind == "snack" and state.habits["play"].satiation < 0.7:
                score = 0.35*state.needs.maintenance+0.25*state.needs.pleasure-0.2*state.habits["play"].satiation
                if score > 0.05:
                    candidates.append((score, item, "eat", "食べ物への関心と満足・活動余力を考えた"))
            elif item.kind == "ball" and state.needs.pleasure > 0.35 and state.habits["play"].satiation < 0.6:
                candidates.append((0.3*state.needs.pleasure, item, "play", "気分転換したいという欲求がある"))
            elif item.kind in {"book", "computer"} and item.used == 0:
                candidates.append((0.2*state.needs.growth+0.15*state.needs.competence, item, "use",
                                   "調べた物を使って、できることを試したい"))
        if not candidates:
            return False
        score, item, action, reason = max(candidates, key=lambda c: c[0])
        physical = session.brain.candidates(world, allow_requests=True)
        if physical and max(c["score"] for c in physical) > score:
            return False
        cognition.active_object, cognition.object_action = item.id, action
        cognition.object_started = world.elapsed
        cognition.attention_object, cognition.attention = item.id, item.label
        cognition.intention = "inspect" if action == "inspect" else "play" if action == "play" else "enjoy"
        self.summarize("行動を選ぶ", item.label, reason, ["world:"+item.id],
                       alternatives=[c[2]+":"+c[1].label for c in candidates[:6]])
        if not world.near_object(item.id, 0.9):
            try:
                world.move_to(world.approach(item).model_dump())
            except ValueError as exc:
                self.summarize("再検討", item.label, str(exc), certainty="observed")
                self.remember_processed(f"blocked:{item.id}:{world.revision}")
                cognition.active_object = cognition.object_action = None
                return False
        return True

    def reflect(self):
        """Cross-reference actual stored beliefs, preserving conflicting alternatives."""
        session, cognition = self.session, self.session.cognition
        entries = session.knowledge.entries()
        signature = hashlib.sha256(repr([(i["id"], i.get("confidence"), i.get("conflicted"))
                                        for i in entries]).encode()).hexdigest()[:24]
        key = "belief-review:"+signature
        if not entries or key in cognition.processed:
            return
        self.remember_processed(key)
        conflicts = [i for i in entries if i.get("conflicted")]
        uncertain = [i for i in entries if i.get("confidence", 1) < 0.55]
        for entry in (conflicts+uncertain)[:3]:
            session.experience.interest(entry["label"], f"{entry['label']}について、別の資料でも確かめたい。",
                                        "保存した説明に食い違い、または未確認の出典がある", "conflict" if entry.get("conflicted") else "growth",
                                        "review:"+entry["id"], 0.85 if entry.get("conflicted") else 0.65)
        self.summarize("記憶を整理", f"{len(entries)}件の教わった内容", "出典と食い違いを見直し、確かめる対象を選んだ",
                       [i["id"] for i in (conflicts+uncertain or entries)[:4]],
                       outcome=f"食い違い{len(conflicts)}件、未確認{len(uncertain)}件を区別", certainty="observed")
        cognition.deliberations += 1

    def step(self):
        self.observe()
        self.reflect()
        occupied = self.advance_object_action()
        if not occupied:
            occupied = self.choose_object_action()
        session, cognition = self.session, self.session.cognition
        if session.world.body.fatigue > 0.6:
            cognition.emotion, cognition.emotion_strength = "tired", session.world.body.fatigue
            if not occupied:
                cognition.intention = "rest"
        elif session.experience.state.needs.comfort > 0.45:
            cognition.emotion = "concerned"
        return occupied
