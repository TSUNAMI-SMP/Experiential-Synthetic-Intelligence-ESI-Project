"""Cancellable environment decisions with independent state polling and session-only leases."""

import re
import time
from concurrent.futures import ThreadPoolExecutor

from esi02.dialogue import Cancellation, Cancelled
from esi04.minecraft import action_menu, current_action, decide
from esi04.plugins import Connection, EnvironmentClient


class EnvironmentRuntime:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pools["environment"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi-environment")
        self.environment_client = None
        self.environment_generation = 0
        self.environment_io = None
        self.environment_task = None
        self.environment_next_poll, self.environment_next_decision = 0, 0

    def _cancel_environment_decision(self):
        if self.environment_task:
            self.environment_task["cancellation"].set()

    def _environment_job(self, kind, function, *args, **extra):
        self.environment_io = kind
        self._job("environment", kind, function, *args, environment_generation=self.environment_generation, **extra)

    def _disconnect_environment(self):
        self._cancel_environment_decision()
        self.environment_task = None
        self.environment_generation += 1
        client, self.environment_client = self.environment_client, None
        self.session.plugins.autonomous = False
        self._status("plugin_connected", False)
        self._status("plugin_session", "")
        self._status("plugin_phase", "disconnected")
        self.environment_io = None
        if client:
            try:
                client.disconnect()
            except Exception:
                pass  # MOD leases also expire; a lost connection never restores room autonomy.
        return client

    def _new_turn(self, text, *args, **kwargs):
        active = bool(self.session.plugins.active_id)
        if active:
            self._cancel_environment_decision()
            if self.environment_client:
                self._environment_job("environment_stop", self.environment_client.stop)
        super()._new_turn(text, *args, **kwargs)
        if not active or not self.current:
            return
        turn = self.session.turns[self.current["turn_id"]]
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return
        if re.search(r"(?:操作|マイクラ|Minecraft|行動).*(?:止めて|停止|やめて)|^(?:もう)?(?:止まって|止めて|やめて|停止して)[。！!\s]*$", text, re.I):
            self.session.plugin_options(autonomous=False)
            self.session.update_turn(turn["id"], plugin_request="Minecraftでの自律活動を停止した")
        elif re.search(r"(?:掘って|採掘して|食べて|歩いて|移動して|調べて|集めて|拾って|見てきて|暮らして|生活して|持ち替えて|装備して|手に持って|ジャンプして|使って|置いて)(?:みて|ほしい|ほしいです|ください|くれる|くれない)?(?:ね|よ)?[。？！!?\s]*$", text):
            self.session.plugin_options(goal=text)
            self.session.update_turn(turn["id"], plugin_request=text)

    def _handle(self, name, data):
        if name != "plugin":
            return super()._handle(name, data)
        operation, values = data
        try:
            if operation in {"connect", "virtual", "disconnect"}:
                self._cancel_computer()
                self._cancel_idle()
                if self.computer:
                    self.computer.revoke()
                self._disconnect_environment()
            if operation == "connect":
                manifest = next(m for m in self.session.plugin_catalog.manifests() if m.id == values["id"])
                self.session.environment_mode(manifest.id)
                client = EnvironmentClient(manifest, Connection.model_validate(values["connection"]))
                self._status("plugin_error", "")
                self._status("plugin_phase", "connecting")
                self._environment_job("environment_connect", client.connect, self.session.agent_id,
                                      self.session.experience.state.character.name, client=client)
            elif operation == "virtual":
                self.session.environment_mode("")
            elif operation == "options":
                if values.get("autonomous") and not self.environment_client:
                    raise RuntimeError("Minecraftに接続してから自律活動を開始してください。")
                if (values.get("autonomous") and self.session.plugins.observation
                        and self.session.plugins.observation.action.get("paused")):
                    raise RuntimeError("Minecraft側で停止中です。再接続してから自律活動を開始してください。")
                self._cancel_environment_decision()
                self.session.plugin_options(**values)
                if not self.session.plugins.autonomous and self.environment_client:
                    self._environment_job("environment_stop", self.environment_client.stop)
                self.environment_next_decision = 0
            elif operation == "manual":
                if not self.environment_client or self.environment_io or self.environment_task:
                    raise RuntimeError("接続と現在の処理を確認してから操作してください。")
                menu = action_menu(self.session.active_manifest(), self.session.plugins.observation)
                action = menu.get(values["id"])
                if action is None:
                    raise ValueError("観察にない操作です。更新して選び直してください。")
                self._environment_job("environment_action", self.environment_client.act, action.model_dump())
            elif operation == "stop":
                self._cancel_environment_decision()
                self.session.plugin_options(autonomous=False)
                if self.environment_client:
                    self._environment_job("environment_stop", self.environment_client.stop)
            self._status("plugin_error", "")
        except Exception as exc:
            self._status("plugin_error", str(exc))

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done() or not job["kind"].startswith("environment_"):
                continue
            del self.jobs[future]
            generation = job["environment_generation"]
            if generation != self.environment_generation:
                if job["kind"] == "environment_connect":
                    # A canceled connection may finish spawning after the user has left the mode.
                    try:
                        job["client"].disconnect()
                    except Exception:
                        pass
                continue
            if job["kind"] == "environment_decision":
                if self.environment_task and self.environment_task["cancellation"] is job["cancellation"]:
                    self.environment_task = None
            else:
                pending = [j["kind"] for j in self.jobs.values() if j["kind"].startswith("environment_")
                           and j["kind"] != "environment_decision" and j["environment_generation"] == generation]
                self.environment_io = pending[-1] if pending else None
            try:
                value = future.result()
                if job["kind"] == "environment_connect":
                    self.environment_client = job["client"]
                    self._status("plugin_session", value.session_id)
                    self._status("plugin_connected", True)
                    self.session.plugin_observed(value.model_dump())
                elif job["kind"] == "environment_observe":
                    self.session.plugin_observed(value.model_dump())
                elif job["kind"] == "environment_decision":
                    if job["cancellation"].event.is_set() or not self.session.plugins.autonomous:
                        continue
                    observation = self.session.plugins.observation
                    if not observation or (value["world_id"], value["session_id"]) != (observation.world_id, observation.session_id):
                        continue
                    self.session.agency.summarize("Minecraftで次の行動を選ぶ", value["choice"], value["reason"],
                                                  [value["observation_id"]], certainty="planned")
                    if value["action"]:
                        action = current_action(value, observation, self.session.active_manifest())
                        if action:
                            self._environment_job("environment_action", self.environment_client.act, action)
                        else:
                            self._status("plugin_phase", "状況が変わったため観察し直しています")
                    elif value["choice"] == "ask":
                        self.session.plugin_question(value["question"], value["reason"])
                    self.session._write()
                    self.environment_next_decision = time.monotonic()+2
                self._status("plugin_phase", "connected")
                self._status("plugin_error", "")
            except Cancelled:
                pass
            except Exception as exc:
                self._status("plugin_error", str(exc))
                self.environment_next_decision = time.monotonic()+5
                if job["kind"] in {"environment_connect", "environment_observe"}:
                    self._disconnect_environment()
        super()._finish_jobs()

    def schedule_environment(self):
        now = time.monotonic()
        if not self.environment_client:
            return
        if not self.environment_io and now >= self.environment_next_poll:
            self._environment_job("environment_observe", self.environment_client.observe)
            self.environment_next_poll = now+0.8
        state = self.session.plugins
        busy = bool(self.current and self._is_current(self.current["turn_id"]))
        observation = state.observation
        if (not state.autonomous or not observation or observation.actor.dead or busy or self.environment_task
                or self.environment_io not in {None, "environment_observe"} or now < self.environment_next_decision
                or observation.action.get("stage") == "running" or self._voice_busy()
                or self.session.status.get("audio_input", {}).get("asr_busy")
                or self.session.experience.pending_question()
                or any(job["kind"] in {"reply", "memory", "deliberation"} for job in self.jobs.values())):
            return
        self._cancel_memory()
        cancellation = Cancellation()
        self.environment_task = {"cancellation": cancellation}
        self._status("plugin_phase", "deciding")
        taught = [{k: entry.get(k) for k in ("label", "value", "source_turn_id", "confidence")}
                  for entry in self.session.knowledge.context(state.goal, limit=4, char_limit=700)]
        self._job("llm", "environment_decision", decide, self.settings, self.session.active_manifest(),
            state.model_copy(deep=True), {**self.session.experience.context(), "availability": self.session.experience.state.availability},
            self.session.plugin_recent(), cancellation, taught,
            environment_generation=self.environment_generation, cancellation=cancellation)

    def stop(self):
        self._disconnect_environment()
        return super().stop()
