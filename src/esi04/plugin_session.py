"""One life environment at a time, with durable, source-linked Minecraft experience."""

import hashlib
import json
import re

from esi02.motivation import Brain as BodyBrain
from esi03.storage import atomic
from esi04.minecraft import embodied_world
from esi04.plugins import EnvironmentObservation, PluginCatalog


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
        if goal is not None:
            self.plugins.goal = str(goal).strip()[:1000]
        self._write()

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
                self.plugins.attempts += 1
                changed = bool(result.get("changed") and result.get("stage") == "completed")
                self.plugins.confirmed_changes += int(changed)
                self.agency.summarize("Minecraftで結果を観察", result.get("kind", "操作"), "MODが確認した実際の結果を記録した",
                    [observation.id, identifier], outcome=str(result.get("detail", ""))[:400], certainty="observed")
                self.experience.trace("minecraft_result", str(result.get("detail", ""))[:400], identifier,
                                      observed_change=changed, action=result.get("kind"))
                self.store.db.execute("DELETE FROM esi04_plugin_results WHERE rowid NOT IN "
                                      "(SELECT rowid FROM esi04_plugin_results ORDER BY rowid DESC LIMIT 512)")
        self.experience.advance(self.environment_world(), 0)
        self._write()

    def plugin_recent(self):
        self.store.db.execute("CREATE TABLE IF NOT EXISTS esi04_plugin_results (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        return [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM esi04_plugin_results ORDER BY rowid DESC LIMIT 6")][::-1]

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
                "last_result": self.plugins.last_result}
