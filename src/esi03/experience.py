"""Observed outcomes change preferences; generated speech cannot award itself a reward."""

import math
import re

from esi.models import uid
from esi02.memory import normalize
from esi02.motivation import clip
from esi03.personality import Goal, Interest, Needs, State, Trace


class Experience:
    outcome_sources = {"explicit_user_response", "virtual_world_observation", "retrieved_document"}
    state_class = State
    initial_question_delay = 30

    def __init__(self, state=None):
        self.state = self.state_class.model_validate(state) if state is not None else self.state_class()
        self.seen_outcome = None
        if not self.state.interests:
            initial = self.interest(
                "覚えたことの確かめ方",
                "覚えたことが合っているか、自分で確かめるにはどうしたらいい？",
                "自分の学び方を育てたい",
                "growth",
                "initial:growth",
            )
            initial.next_at = self.state.elapsed + self.initial_question_delay

    def trace(self, kind, description, source_id, **details):
        state = self.state
        state.sequence += 1
        entry = Trace(
            seq=state.sequence,
            at=state.elapsed,
            kind=kind,
            description=description[:500],
            source_id=source_id[:128],
            details=details,
        )
        state.traces = (state.traces + [entry])[-128:]
        return entry

    def interest(self, topic, question, reason, origin, source_id, priority=0.6):
        topic = topic.strip()[:120]
        if not topic:
            return None
        existing = next((i for i in self.state.interests if normalize(i.topic) == normalize(topic)), None)
        if existing:
            if origin == "conflict" and existing.source_id != source_id:
                existing.origin = "conflict"
                existing.reason = reason[:240]
                existing.question = question[:240]
                existing.source_id = source_id[:128]
                existing.attempts = 0
            if existing.source_id != source_id and existing.stage in {"resolved", "paused"}:
                existing.stage = "open"
                existing.reason = reason[:240]
                existing.source_id = source_id[:128]
                existing.attempts = 0
                existing.next_at = self.state.elapsed + 30
            existing.priority = max(existing.priority, clip(priority))
            return existing
        if len(self.state.interests) >= 96:
            # Evict only finished topics without goal/action references.
            protected = {g.interest_id for g in self.state.goals} | set(self.state.question_actions.values())
            removable = next(
                (i for i in self.state.interests if i.stage == "resolved" and i.id not in protected), None
            )
            if removable is None:
                return None
            self.state.interests = [i for i in self.state.interests if i.id != removable.id]
        item = Interest(
            id=uid(),
            topic=topic,
            question=question[:240],
            reason=reason[:240],
            origin=origin,
            source_id=source_id[:128],
            priority=clip(priority),
            created_at=self.state.elapsed,
        )
        self.state.interests = self.state.interests + [item]
        self.trace("interest", f"気になっていること: {topic}", source_id, interest_id=item.id, reason=reason)
        return item

    def advance(self, world, seconds):
        if not math.isfinite(seconds) or not 0 <= seconds <= 60:
            raise ValueError("身体と欲求の更新間隔は0〜60秒です。")
        state = self.state
        state.elapsed = world.elapsed
        traits, values = state.character.traits, state.character.values
        stimulus, body = world.observe(), world.body
        for habit in state.habits.values():
            habit.satiation *= math.exp(-seconds / 180)
            habit.tolerance *= math.exp(-seconds / 3600)
            habit.strength *= math.exp(-seconds / (8 * 3600))
        state.approval_balance = clip(state.approval_balance * math.exp(-seconds / 7200))
        state.relationship = min(state.preferences.max_attachment, state.relationship)
        lonely = clip((state.elapsed - state.last_contact) / 900)
        learning_gap = clip((state.elapsed - state.last_learning) / 600)
        novelty_gap = clip((state.elapsed - state.last_novelty) / 300)
        pending = sum(i.stage in {"open", "investigating"} for i in state.interests)
        busy = float(world.conversation_active)
        state.needs = Needs(
            maintenance=max(1 - body.energy, 1 - body.hydration, 1 - body.condition),
            comfort=max(stimulus.thermal_discomfort, stimulus.noise_discomfort, stimulus.body_discomfort),
            rest=body.fatigue,
            curiosity=clip(traits.curiosity * (0.25 + 0.5 * learning_gap + 0.05 * min(4, pending))),
            novelty=traits.curiosity * novelty_gap,
            growth=clip(values.growth * (0.3 + 0.6 * learning_gap)),
            competence=clip((1 - state.self_efficacy) * (0.4 + 0.6 * values.growth)),
            autonomy=clip(values.independence * (0.25 + 0.5 * busy)),
            meaning=clip((1 - state.meaningful_progress) * values.growth),
            social=clip(traits.sociability * lonely),
            belonging=clip((0.25 + 0.5 * lonely) * (1 - state.relationship)),
            approval=clip(traits.approval_sensitivity * (1 - state.approval_balance)),
            attachment=min(
                state.preferences.max_attachment, traits.attachment_sensitivity * state.relationship * lonely
            ),
            pleasure=clip(
                (0.2 + 0.4 * novelty_gap + 0.2 * traits.playfulness) * (1 - state.habits["play"].satiation)
            ),
        )
        need, mod = state.needs, state.modulators
        alpha = 1 - math.exp(-seconds / 20)
        targets = {
            "dopamine": clip(0.2 + 0.25 * need.curiosity + 0.45 * mod.reward_prediction_error),
            "serotonin": clip(
                0.25 + 0.4 * (1 - max(need.maintenance, need.comfort)) + 0.15 * state.self_efficacy
            ),
            "noradrenaline": clip(0.1 + 0.7 * max(need.maintenance, need.comfort, need.rest)),
            "oxytocin": clip(
                0.15 + 0.5 * state.relationship * math.exp(-max(0, state.elapsed - state.last_contact) / 180)
            ),
        }
        for name, target in targets.items():
            setattr(mod, name, getattr(mod, name) + alpha * (target - getattr(mod, name)))
        mod.reward_prediction_error *= math.exp(-seconds / 30)

    def outcome(self, identifier, activity, reward, provenance, source_id, details=None):
        if provenance not in self.outcome_sources:
            raise ValueError("生成した文章は報酬や学習結果にできません。")
        if activity not in self.state.habits or not math.isfinite(reward) or not -1 <= reward <= 1:
            raise ValueError("観測された活動と報酬の範囲が正しくありません。")
        if not identifier or not source_id:
            raise ValueError("報酬には一意な観測と出典が必要です。")
        state = self.state
        if identifier in state.recent_outcomes or (
            self.seen_outcome is not None and self.seen_outcome(identifier)
        ):
            return False
        habit = state.habits[activity]
        prediction = habit.expected_reward
        # Repetition brings satiation and tolerance instead of unbounded reinforcement.
        effective = (
            reward * (1 - 0.65 * habit.satiation) * (1 - 0.5 * habit.tolerance) if reward > 0 else reward
        )
        habit.trials += 1
        rate = max(0.03, 1 / (habit.trials + 2))
        habit.expected_reward = clip(prediction + rate * (effective - prediction), -1, 1)
        habit.strength = clip(habit.strength + 0.06 * effective, 0, 0.65)
        habit.tolerance = clip(habit.tolerance + max(0, reward) * 0.08, 0, 0.8)
        habit.satiation = clip(habit.satiation + max(0, reward) * 0.3)
        habit.last_at = state.elapsed
        state.modulators.reward_prediction_error = clip(effective - prediction, -1, 1)
        state.recent_outcomes = (state.recent_outcomes + [identifier])[-512:]
        changes = {}
        trait = {
            "research": "curiosity",
            "question": "sociability",
            "conversation": "trust",
            "rest": "caution",
            "explore": "persistence",
            "play": "playfulness",
        }[activity]
        current = getattr(state.character.traits, trait)
        initial = getattr(state.character.initial_traits, trait)
        updated = clip(
            current + 0.015 * state.character.plasticity * effective,
            max(0, initial - 0.2),
            min(1, initial + 0.2),
        )
        if abs(updated - current) > 1e-8:
            setattr(state.character.traits, trait, updated)
            changes[trait] = {"before": current, "after": updated}
        if activity in {"research", "question"} and reward > 0:
            state.self_efficacy = clip(state.self_efficacy + 0.03 * effective)
            state.meaningful_progress = clip(state.meaningful_progress + 0.04 * effective)
            state.last_learning = state.elapsed
        self.trace(
            "outcome",
            f"{activity}の経験から活動の好みを更新",
            source_id,
            outcome_id=identifier,
            provenance=provenance,
            reward=reward,
            effective_reward=effective,
            prediction=prediction,
            trait_changes=changes,
            observations=details or {},
        )
        return True

    def user_turn(self, turn):
        state = self.state
        if turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition"}:
            return
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return
        if turn["epoch"] <= state.last_user_epoch or not turn["input"].strip():
            return
        state.last_user_epoch = turn["epoch"]
        state.last_contact = state.elapsed
        state.relationship = clip(state.relationship + 0.002, 0, state.preferences.max_attachment)
        text = turn["input"].strip()
        if re.fullmatch(
            r"(?:もう)?(?:話しかけないで|話しかけるのをやめて|自発的な質問をやめて)[。！! ]*", text
        ):
            state.preferences.proactive = False
            state.preferences.quiet_until = state.elapsed + 900
            self.pause_question("自発的な質問を停止するという相手の意思", turn["id"])
        elif re.fullmatch(r"(?:今は|いまは)?(?:忙しい|話せない|話したくない|あとで|後で)[。！! ]*", text):
            state.preferences.quiet_until = state.elapsed + 900
            self.pause_question("後で話したいという相手の意思", turn["id"])
        if re.fullmatch(
            r"(?:(?:教えて|話して|手伝って)くれて)?(?:ありがとう(?:ございます|ございました|ね)?|よく(?:できた|出来た|頑張った)(?:ね)?|役に立った(?:よ)?|助かった(?:よ|ね)?)[。！!\s]*",
            text,
        ):
            state.approval_balance = clip(state.approval_balance + 0.12)
            self.outcome(
                "approval:" + turn["id"],
                "conversation",
                0.12,
                "explicit_user_response",
                turn["id"],
                {"quote": text[:200]},
            )
        elif re.fullmatch(r"(?:違います|違う|間違っています|間違ってる|それは誤り)[。！! ]*", text):
            state.approval_balance = clip(state.approval_balance - 0.08)
            self.outcome(
                "correction-feedback:" + turn["id"],
                "conversation",
                -0.06,
                "explicit_user_response",
                turn["id"],
                {"quote": text},
            )

    def learned(self, entries, source_id, correction=False):
        state = self.state
        if not entries:
            return
        state.acquired_claims += len(entries)
        state.corrections += int(correction)
        state.last_learning = state.last_novelty = state.elapsed
        for item in entries:
            topic = item["label"]
            existing = [i for i in state.interests if normalize(i.topic) == normalize(topic)]
            for interest in existing:
                if item["id"] not in interest.evidence_ids:
                    interest.evidence_ids = (interest.evidence_ids + [item["id"]])[-24:]
                if interest.origin in {"unknown", "conflict"} and not item.get("conflicted"):
                    interest.stage = "resolved"
                    if state.active_interest == interest.id:
                        state.active_interest = None
            if not existing and len(topic) <= 100:
                self.interest(
                    topic,
                    f"{topic}について、もっと詳しく教えてもらえる？",
                    "教わったことの続きを知りたい",
                    "teaching",
                    source_id,
                    0.55,
                )
        self.trace(
            "correction" if correction else "learning",
            "教わった内容と出典を保存",
            source_id,
            claim_ids=[i["id"] for i in entries],
        )
        self.outcome(
            "learning:" + source_id,
            "question",
            0.18,
            "explicit_user_response",
            source_id,
            {"claim_ids": [i["id"] for i in entries]},
        )

    def pause_question(self, reason, source_id):
        state = self.state
        active = next((i for i in state.interests if i.id == state.active_interest), None)
        if active:
            active.stage = "paused"
            active.next_at = state.elapsed + 900
            state.active_interest = None
            self.trace("pause", reason, source_id, interest_id=active.id)

    def question_candidate(self, world):
        state, preferences = self.state, self.state.preferences
        if not preferences.proactive or world.conversation_active or state.elapsed < preferences.quiet_until:
            return None
        if (
            state.last_question is not None
            and state.elapsed - state.last_question < preferences.min_question_interval
        ):
            return None
        if world.body.energy < 0.45 or world.body.fatigue > 0.65 or state.needs.comfort > 0.5:
            return None
        if state.active_interest is not None:
            return None
        candidates = [
            i
            for i in state.interests
            if i.stage in {"open", "paused"} and i.next_at <= state.elapsed and i.attempts < 3
        ]
        if not candidates:
            return None
        habit = state.habits["question"]
        traits, values, need = state.character.traits, state.character.values, state.needs
        drive = (
            0.32 * traits.curiosity * need.curiosity
            + 0.25 * values.growth * need.growth
            + 0.12 * traits.sociability * need.social
            + 0.10 * need.competence
            + 0.05 * need.approval
            + 0.04 * need.attachment
            + 0.08 * habit.strength * (1 - habit.satiation)
        )
        candidate = max(
            candidates,
            key=lambda i: i.priority + min(0.15, (state.elapsed - i.created_at) / 36000) - i.attempts * 0.1,
        )
        return {
            "interest": candidate,
            "score": drive * (0.6 + 0.4 * candidate.priority),
            "reason": candidate.reason,
        }

    def asked(self, identifier, action_id):
        state = self.state
        item = next(i for i in state.interests if i.id == identifier)
        item.stage = "asking"
        item.last_asked = state.last_question = state.elapsed
        item.attempts += 1
        item.next_at = state.elapsed + max(600, state.preferences.min_question_interval * (2**item.attempts))
        state.questions += 1
        state.active_interest = item.id
        state.question_actions = dict(list({**state.question_actions, action_id: identifier}.items())[-64:])
        self.trace(
            "question",
            item.question,
            action_id,
            interest_id=identifier,
            reason=item.reason,
            needs=state.needs.model_dump(),
        )

    def finish_question(self, action_id, stage, source_id):
        identifier = self.state.question_actions.get(action_id)
        item = next((i for i in self.state.interests if i.id == identifier), None)
        if item and item.stage == "asking":
            item.stage = "paused" if stage in {"declined", "cancelled"} else "open"
            if stage in {"declined", "cancelled"}:
                item.next_at = max(item.next_at, self.state.elapsed + 900)
            if self.state.active_interest == identifier:
                self.state.active_interest = None
            self.trace("question_result", "質問の結果を保存", source_id, interest_id=identifier, stage=stage)

    def plan_research(self, tool="web_search", interest_id=None, manual=False):
        state = self.state
        candidates = [
            i
            for i in state.interests
            if (manual or i.stage in {"open", "paused"} and i.attempts < 3 and i.next_at <= state.elapsed)
            and not any(
                g.interest_id == i.id
                and (
                    g.stage in {"pending", "running"}
                    or not manual
                    and g.source_id == i.source_id
                    and g.stage == "complete"
                )
                for g in state.goals
            )
        ]
        item = (
            next((i for i in candidates if i.id == interest_id), None)
            if interest_id
            else max(candidates, key=lambda i: i.priority, default=None)
        )
        if not item or any(
            g.interest_id == item.id and g.stage in {"pending", "running"} for g in state.goals
        ):
            return None
        if not manual and any(
            g.interest_id == item.id and g.source_id == item.source_id and g.stage == "complete"
            for g in state.goals
        ):
            return None
        if len(state.goals) >= 64:
            state.goals = [g for g in state.goals if g.stage in {"pending", "running"}][-63:]
        goal = Goal(
            id=uid(),
            interest_id=item.id,
            source_id=item.source_id,
            kind="verify" if item.origin == "conflict" else "learn",
            tool=tool,
            next_at=state.elapsed,
        )
        state.goals = state.goals + [goal]
        self.trace("goal", f"自分で調べる目標: {item.topic}", item.source_id, goal_id=goal.id, tool=tool)
        return goal

    def recover(self):
        # Restart never repeats an old request or external operation automatically.
        for goal in self.state.goals:
            if goal.stage == "running":
                goal.stage = "cancelled"
                goal.error = "再起動前の操作結果を確認できないため自動再実行しません。"
        for interest in self.state.interests:
            if interest.stage in {"asking", "investigating"}:
                interest.stage = "paused"
                interest.next_at = self.state.elapsed + 900
        self.state.active_interest = None

    def context(self):
        state = self.state
        return {
            "simulation_only": True,
            "name": state.character.name,
            "style": state.character.style,
            "values": state.character.values.model_dump(),
            "traits": state.character.traits.model_dump(),
            "needs": state.needs.model_dump(),
            "modulators": state.modulators.model_dump(),
            "interests": [i.model_dump() for i in state.interests if i.stage in {"open", "asking"}][:4],
            "growth": {
                "acquired_claims": state.acquired_claims,
                "corrections": state.corrections,
                "questions": state.questions,
            },
            "goal": "自分を大切にし、疑問を調べ、経験から学び方を育てる",
        }
