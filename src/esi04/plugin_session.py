"""One life environment at a time, with durable, source-linked Minecraft experience."""

import hashlib
import json
import re

from esi02.motivation import Brain as BodyBrain
from esi03.storage import atomic
from esi04.environment_learning import (
    LifePlan,
    PendingAttempt,
    SmallGoal,
    TrialMemory,
    check_result,
    held_item,
    learning_context,
    remember_places,
)
from esi04.minecraft import embodied_world
from esi04.plugins import EnvironmentAction, EnvironmentObservation, PluginCatalog


class EnvironmentSession:
    @property
    def plugin_catalog(self):
        return PluginCatalog(self.store.path.parent / "plugins")

    def active_manifest(self):
        return next((m for m in self.plugin_catalog.manifests() if m.id == self.plugins.active_id), None)

    def dump(self):
        return {**super().dump(), "plugins": self.plugins.model_dump()}

    def environment_world(self):
        return embodied_world(self.world, self.plugins, self.status.get("plugin_connected", False)) if self.plugins.active_id else self.world

    @atomic
    def environment_mode(self, identifier):
        if identifier and not any(m.id == identifier for m in self.plugin_catalog.manifests()):
            raise ValueError("そのプラグインはインストールされていません。")
        if self.brain.active:
            self._record_motivation([self.brain._resolve(self.world, None, "cancelled", "生活する環境を切り替えた")])
        self.world.destination = None
        self.world.path = []
        self.world.body.activity = "idle"
        self.cognition.active_object, self.cognition.object_action = None, None
        self.cognition.pending_computer_task = ""
        self.plugins.active_id, self.plugins.autonomous = identifier, False
        self.status.update(plugin_connected=False, plugin_error="", plugin_session="")
        self.experience.advance(self.environment_world(), 0)
        self._write([("environment_mode", {"id": identifier})])

    @atomic
    def plugin_options(self, autonomous=None, goal=None):
        if autonomous is not None:
            self.plugins.autonomous = bool(autonomous)
            if autonomous:
                self.plugins.learning.consecutive_no_effect = 0
        if goal is not None:
            text = str(goal).strip()[:1000]
            if text != self.plugins.goal:
                old = self.plugins.learning.plan
                self.agency.summarize("Minecraftの目標を切り替える", text, "新しい目標に合わせて、以前の未実行の手順を保留した",
                                      certainty="planned")
                self.plugins.learning.plan = LifePlan(goal=text,
                    world_id=self.plugins.observation.world_id if self.plugins.observation else "",
                    revisions=old.revisions, review="目標が変わったため、新しい小目標を考える。過去の操作は経験として残す。")
                self.plugins.learning.consecutive_no_effect = 0
            self.plugins.goal = text
        if autonomous is False:
            self.plugins.learning.plan.stage = "paused"
        self._write()

    @atomic
    def plugin_attempt(self, action, decision=None):
        action = EnvironmentAction.model_validate(action)
        observation = self.plugins.observation
        if not observation or action.observation_id != observation.id:
            raise ValueError("現在の観察に結び付いた操作だけを記録できます。")
        decision = decision or {}
        step = SmallGoal.model_validate(decision.get("small_goal", {"purpose": "利用者が選んだ操作を試す", "check": "observe"}))
        target = next((t for t in observation.targets if t.id == action.target_id), None)
        target_name = target.name if target else ""
        if action.kind in {"eat", "select_slot", "deposit"}:
            target_name = next((i.name for i in observation.inventory if i.slot == action.slot), "empty hand")
        elif action.kind == "craft":
            target_name = next((r.output for r in observation.recipes if r.id == action.recipe_id), action.recipe_id)
        elif action.kind == "withdraw" and observation.container:
            target_name = next((i.name for i in observation.container.items if i.slot == action.container_slot), "")
        self.plugins.learning.pending = PendingAttempt(request_id=action.request_id, world_id=observation.world_id,
            session_id=observation.session_id, dimension=observation.actor.dimension, observation_id=observation.id,
            kind=action.kind, target_id=action.target_id, target_name=target_name,
            held=held_item(observation), recipe_id=action.recipe_id, step=step)
        old = self.plugins.learning.plan
        self.plugins.learning.plan = LifePlan(world_id=observation.world_id, goal=self.plugins.goal, step=step, stage="running",
            focus=decision.get("focus", "curiosity"),
            reason=str(decision.get("reason", "利用者が選んだ操作"))[:300], review=old.review,
            source_id=observation.id, request_id=action.request_id, revisions=old.revisions+1)
        self.cognition.intention = "operate"
        self.cognition.attention = step.purpose or action.kind
        self.cognition.emotion, self.cognition.emotion_strength = "interested", .5
        self.agency.summarize("Minecraftで小さな目標を試す", step.purpose or action.kind,
            self.plugins.learning.plan.reason, [observation.id], alternatives=step.following, certainty="planned")
        self._write()

    @atomic
    def plugin_interrupted(self, reason, retain_pending=False):
        if not retain_pending:
            self.plugins.learning.pending = None
        self.plugins.learning.plan.stage = "unobserved"
        self.plugins.learning.plan.review = str(reason)[:400]
        self._write()

    def _learn_plugin_result(self, observation, identifier):
        learning, result = self.plugins.learning, observation.action
        pending = learning.pending
        # Unowned/replayed actions can be displayed, but never reinforce the character.
        if (not pending or pending.request_id != result.get("request_id") or pending.kind != result.get("kind")
                or (pending.world_id, pending.session_id, pending.dimension) !=
                (observation.world_id, observation.session_id, observation.actor.dimension)):
            return
        learning.pending = None
        verified = (check_result(pending, result.get("evidence", {}), observation)
                    if result.get("before_observation") == pending.observation_id else None)
        same_plan = learning.plan.request_id == pending.request_id
        if verified is None:
            if same_plan:
                learning.plan.stage, learning.plan.review = "unobserved", "操作前後の証拠が足りず、目標達成を確認できない。"
            return
        confirmed = verified["goal_confirmed"] and result.get("stage") == "completed"
        if same_plan:
            learning.plan.stage = "confirmed" if confirmed else "failed" if result.get("stage") == "failed" else "unobserved"
            learning.plan.review = verified["detail"][:400]
        episode = {"id": identifier, "kind": pending.kind, "world_id": observation.world_id,
                   "small_goal": pending.step.model_dump(),
                   "source_observation": observation.id, "goal_confirmed": confirmed,
                   "effect_observed": verified["effect_observed"], "detail": verified["detail"][:400],
                   "gains": verified["gains"], "losses": verified["losses"],
                   "interpretation": "前後の変化は観察。原因の一般化や次の手順はまだ推測。"}
        learning.episodes = (learning.episodes+[episode])[-64:]
        key = "|".join((observation.world_id, pending.dimension, pending.kind, pending.target_name, pending.held, pending.recipe_id))
        trial = next((t for t in learning.trials if t.key == key), None)
        if trial is None:
            trial = TrialMemory(key=key, world_id=observation.world_id, dimension=pending.dimension, action=pending.kind,
                                target=pending.target_name or pending.recipe_id, held=pending.held)
            learning.trials = (learning.trials+[trial])[-96:]
        trial.trials += 1
        trial.effects += int(verified["effect_observed"])
        trial.goal_checks += int(confirmed)
        trial.last_source, trial.last_detail = identifier, verified["detail"][:400]
        learning.consecutive_no_effect = 0 if verified["effect_observed"] else learning.consecutive_no_effect+1
        # Actual eating, acquisition, movement and injury affect preferences. Looking or an LLM success claim cannot.
        reward = -.2 if verified["health_delta"] < 0 else .12 if verified["gains"] or verified["food_delta"] > 0 else 0
        activity = "research" if pending.kind in {"mine", "craft", "use", "withdraw"} else "explore"
        if reward:
            self.experience.outcome(identifier, activity, reward, "game_observation", identifier,
                                    {"id": identifier, "status": "verified", **verified})
        self.cognition.emotion = "concerned" if verified["health_delta"] < 0 else "pleased" if reward > 0 else "uncertain" if not confirmed else "interested"
        self.cognition.emotion_strength = .65 if reward else .4
        self.cognition.intention = "think"
        self.agency.summarize("Minecraftの目標を見直す", pending.step.purpose, "操作前後の変化から、確認済みと未確認を区別した",
                              [pending.observation_id, observation.id, identifier], outcome=verified["detail"], certainty="observed")
        if learning.consecutive_no_effect >= 3:
            self.plugins.autonomous = False
            learning.plan.review += " 変化のない試行が続いたため、いったん停止して目標や操作を見直す。"

    def advance_plugin_clock(self, seconds, conversing):
        # The shared clock keeps social/learning state consistent; virtual physiology stays frozen.
        self.world.elapsed += seconds
        self.world.conversation_active = conversing
        self.experience.advance(self.environment_world(), seconds)
        self.brain.state.needs.maintenance = self.experience.state.needs.maintenance
        self.brain.state.needs.comfort = 0
        self.brain.state.needs.rest = 0
        self.brain.state.needs.curiosity = self.experience.state.needs.curiosity
        self.brain.state.needs.social = self.experience.state.needs.social
        if self.brain.active and self.world.elapsed >= self.brain.active.deadline:
            self._record_motivation([self.brain._resolve(self.world, None, "unknown", "返答を観測できないため質問を保留した")])
        self._write()

    @atomic
    def plugin_observed(self, value):
        observation = EnvironmentObservation.model_validate(value)
        if observation.plugin_id != self.plugins.active_id:
            raise ValueError("現在の生活環境とは違う観察です。")
        if observation.session_id != self.status.get("plugin_session"):
            raise ValueError("前の接続から届いた観察は使えません。")
        self.plugins.observation = observation
        remember_places(self.plugins.learning, observation, self.world.elapsed)
        if observation.action.get("paused"):
            self.plugins.autonomous = False
        for target in observation.targets:
            if target.kind == "waypoint":
                continue
            key = observation.world_id+":"+target.name
            if key not in self.plugins.discoveries:
                if len(self.plugins.discoveries) >= 256:
                    del self.plugins.discoveries[next(iter(self.plugins.discoveries))]
                self.plugins.discoveries[key] = 1
                self.experience.state.last_novelty = self.world.elapsed
        result = observation.action
        if result.get("stage") in {"completed", "failed", "stopped"} and result.get("request_id"):
            self.store.db.execute("CREATE TABLE IF NOT EXISTS esi04_plugin_results (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            identifier = observation.world_id+":"+result["request_id"]
            exists = self.store.db.execute("SELECT 1 FROM esi04_plugin_results WHERE id=?", (identifier,)).fetchone()
            if not exists:
                record = {"id": identifier, "plugin_id": observation.plugin_id, "world_id": observation.world_id,
                          "observation_id": observation.id, "actor_id": observation.actor.id, **result}
                self.store.db.execute("INSERT INTO esi04_plugin_results VALUES (?,?)", (identifier, json.dumps(record, ensure_ascii=False)))
                self.plugins.last_result = record
                self._learn_plugin_result(observation, identifier)
                self.plugins.attempts += 1
                changed = bool(result.get("changed") and result.get("stage") == "completed")
                self.plugins.confirmed_changes += int(changed)
                detail = str(result.get("detail") or f"{result.get('kind', '操作')}の{result.get('stage')}を観察した")[:400]
                self.agency.summarize("Minecraftで結果を観察", result.get("kind", "操作"), "MODが確認した実際の結果を記録した",
                    [observation.id, identifier], outcome=detail, certainty="observed")
                self.experience.trace("minecraft_result", detail, identifier,
                                      observed_change=changed, action=result.get("kind"))
                self.store.db.execute("DELETE FROM esi04_plugin_results WHERE rowid NOT IN "
                                      "(SELECT rowid FROM esi04_plugin_results ORDER BY rowid DESC LIMIT 512)")
        self.experience.advance(self.environment_world(), 0)
        self._write()

    def plugin_recent(self):
        self.store.db.execute("CREATE TABLE IF NOT EXISTS esi04_plugin_results (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        world = self.plugins.observation.world_id if self.plugins.observation else ""
        return [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT payload FROM esi04_plugin_results WHERE json_extract(payload,'$.world_id')=? ORDER BY rowid DESC LIMIT 6", (world,))][::-1]

    @atomic
    def plugin_question(self, text, reason):
        state = self.experience.state
        if (not text.strip() or not state.preferences.proactive or state.availability == "busy"
                or self.experience.pending_question() or self.brain.active):
            return
        observation = self.plugins.observation
        context = (observation.world_id, re.sub(r"\W", "", text), self.plugins.goal,
                   sorted({target.name for target in observation.targets if target.kind != "waypoint"}),
                   sorted((item.name, item.count) for item in observation.inventory))
        signature = hashlib.sha256(repr(context).encode()).hexdigest()
        if signature in self.plugins.asked_contexts:
            return
        self.plugins.asked_contexts = dict(list({**self.plugins.asked_contexts, signature: text[:160]}.items())[-32:])
        # Only a social request is created here; the virtual body's action scheduler stays stopped.
        old_activity = self.world.body.activity
        # Skip the room interest chooser: it would open a different question before this one.
        action = BodyBrain._begin(self.brain, self.world, "ask_talk", False)
        self.world.body.activity = old_activity
        action.text = text[:160]
        self.experience.open_question(action.text, action.text[:60], reason, action_id=action.id)
        self.agency.summarize("Minecraftについて尋ねる", action.text, reason, [self.plugins.observation.id], certainty="planned")
        self._write()

    def world_operation(self, *args, **kwargs):
        if self.plugins.active_id:
            raise ValueError("外部環境で生活している間は、仮想の部屋・身体の操作を停止しています。")
        return super().world_operation(*args, **kwargs)

    def environment_context(self):
        manifest = self.active_manifest()
        return {"active": bool(self.plugins.active_id), "id": self.plugins.active_id,
                "name": manifest.name if manifest else "", "connected": self.status.get("plugin_connected", False),
                "autonomous": self.plugins.autonomous, "goal": self.plugins.goal,
                "disabled_concepts": manifest.disabled_concepts if manifest else [],
                "observation": self.plugins.observation.model_dump() if self.plugins.observation else None,
                "last_result": self.plugins.last_result,
                "learning": learning_context(self.plugins)}
