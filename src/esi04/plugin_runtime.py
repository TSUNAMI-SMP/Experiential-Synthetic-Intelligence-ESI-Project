"""Cancellable environment decisions with independent state polling and session-only leases."""

import re
import time
from concurrent.futures import ThreadPoolExecutor

from esi02.dialogue import Cancellation, Cancelled
from esi04.environment_life import current_goal, decision_signature, situation_signature
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
        self.environment_outbox = {}

    def _queue_environment_chat(self, identifier, text):
        seen = self.session.plugins.observation
        if not seen or not self.environment_client or not getattr(self.environment_client, "chat_available", False):
            return
        self.environment_outbox[identifier] = {"text": text, "world": seen.world_id, "session": seen.session_id,
                                               "retry_at": 0, "attempts": 0}
        while len(self.environment_outbox) > 8:
            del self.environment_outbox[next(iter(self.environment_outbox))]

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
        self.environment_outbox.clear()
        client, self.environment_client = self.environment_client, None
        self.session.plugins.autonomous = False
        self.session.plugin_interrupted("接続を解除したため、中断した操作の成否は確定せず保留した。")
        self._status("plugin_connected", False)
        self._status("plugin_session", "")
        self._status("plugin_phase", "disconnected")
        self._status("plugin_wait_reason", "接続を解除しました。目的と経験は保持し、再接続後に周囲を確認します。")
        self.environment_io = None
        if client:
            try:
                client.disconnect()
            except Exception:
                pass  # MOD leases also expire; a lost connection never restores room autonomy.
        return client

    def _new_turn(self, text, source="text", observation=None):
        active = bool(self.session.plugins.active_id)
        from_minecraft = source == "minecraft"
        if active:
            self._cancel_environment_decision()
        # The modality is user text. Keep the channel separately so existing learning,
        # availability and question binding treat in-game speech like the same person's typing.
        super()._new_turn(text, "text" if from_minecraft else source, observation)
        if not active or not self.current:
            return
        turn = self.session.turns[self.current["turn_id"]]
        if from_minecraft:
            self.session.update_turn(turn["id"], input_channel="minecraft")
            for message in self.session.plugins.life.messages:
                if message.id == turn["id"] and message.speaker == "user":
                    message.channel = "minecraft"
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return
        if re.search(r"(?:操作|マイクラ|Minecraft|行動).*(?:止めて|停止|やめて)|^(?:もう)?(?:止まって|止めて|やめて|停止して)[。！!\s]*$", text, re.I):
            self.session.plugin_options(autonomous=False)
            self.session.update_turn(turn["id"], plugin_request="Minecraftでの自律活動を停止した")
            if self.environment_client:
                self._environment_job("environment_stop", self.environment_client.stop)
        observation = self.session.plugins.observation
        if observation:
            self.session.update_turn(turn["id"], plugin_world=observation.world_id,
                                     plugin_session=observation.session_id)
        # Ordinary statements, proposals and answers are already retained by Session._remember.
        # Do not overwrite a life purpose just because an utterance ends in an imperative.

    def _handle(self, name, data):
        if name != "plugin":
            return super()._handle(name, data)
        operation, values = data
        try:
            if operation in {"connect", "virtual", "disconnect"}:
                self._cancel_tools()
                self.pending_note = None
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
                changed_goal = values.get("goal") is not None and values["goal"].strip() != self.session.plugins.goal
                self.session.plugin_options(**values)
                if (not self.session.plugins.autonomous or changed_goal) and self.environment_client:
                    self._environment_job("environment_stop", self.environment_client.stop)
                self.environment_next_decision = 0
            elif operation == "manual":
                if not self.environment_client or self.environment_io or self.environment_task:
                    raise RuntimeError("接続と現在の処理を確認してから操作してください。")
                menu = action_menu(self.session.active_manifest(), self.session.plugins.observation)
                action = menu.get(values["id"])
                if action is None:
                    raise ValueError("観察にない操作です。更新して選び直してください。")
                self.session.plugin_attempt(action.model_dump())
                self._environment_job("environment_action", self.environment_client.act, action.model_dump())
            elif operation == "stop":
                self._cancel_environment_decision()
                self.session.plugin_options(autonomous=False)
                if self.environment_client:
                    self._environment_job("environment_stop", self.environment_client.stop)
            self._status("plugin_error", "")
        except Exception as exc:
            self._status("plugin_error", str(exc))

    def _reply_result(self, job, result):
        super()._reply_result(job, result)
        turn = self.session.turns[job["turn_id"]]
        seen = self.session.plugins.observation
        if (turn.get("minecraft_message_id") and self.environment_client and seen
                and (turn.get("plugin_world"), turn.get("plugin_session")) == (seen.world_id, seen.session_id)
                and turn["id"] not in self.session.plugins.life.sent_messages):
            self._queue_environment_chat(turn["id"], result["text"])

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
                    self._status("plugin_bridge_version", getattr(self.environment_client, "bridge_version", "未確認"))
                    self._status("plugin_chat_available", getattr(self.environment_client, "chat_available", False))
                    self.session.plugin_observed(value.model_dump())
                elif job["kind"] == "environment_observe":
                    self.session.plugin_observed(value.model_dump())
                    if (self.environment_task and "situation" in self.environment_task
                            and self.environment_task["situation"] != decision_signature(value)
                            and not self.environment_task["cancellation"].event.is_set()):
                        self._cancel_environment_decision()
                        self.environment_next_decision = 0
                        self.session.plugin_interrupted("考えている間に身体・所持品・作業条件が変わったため、新しい状態から考え直す。")
                elif job["kind"] == "environment_chat":
                    identifier = job["chat_id"]
                    self.environment_outbox.pop(identifier, None)
                    self.session.plugins.life.sent_messages = (self.session.plugins.life.sent_messages+[identifier])[-64:]
                elif job["kind"] == "environment_decision":
                    if job["cancellation"].event.is_set() or not self.session.plugins.autonomous:
                        continue
                    observation = self.session.plugins.observation
                    if (not observation or (value["world_id"], value["session_id"]) != (observation.world_id, observation.session_id)
                            or value["source_observation"]["actor"]["dimension"] != observation.actor.dimension):
                        continue
                    self.session.plugin_decided(value)
                    self.session.agency.summarize("Minecraftで次の行動を選ぶ", value["choice"], value["reason"],
                                                  [value["observation_id"]], certainty="planned")
                    if value["action"]:
                        action = current_action(value, observation, self.session.active_manifest())
                        if action:
                            self.session.plugin_attempt(action, value)
                            self._environment_job("environment_action", self.environment_client.act, action)
                        else:
                            self._status("plugin_phase", "状況が変わったため観察し直しています")
                            self.session.plugin_interrupted("推論中に対象・所持品・作業条件が変わったため、実行せず考え直す。")
                    elif value["choice"] == "ask":
                        text = self.session.plugin_question(value["question"], value["reason"])
                        if text:
                            self._queue_environment_chat("question:"+str(self.session.plugins.life.decisions), text)
                    self.session._write()
                    self.environment_next_decision = time.monotonic()+2
                if job["kind"] != "environment_observe" or self.session.status.get("plugin_error_kind") == "environment_observe":
                    self._status("plugin_error", "")
                    self._status("plugin_error_kind", "")
            except Cancelled:
                pass
            except Exception as exc:
                if job["kind"] == "environment_decision" and job["cancellation"].event.is_set():
                    continue  # Closing an in-flight HTTP socket can report OSError instead of Cancelled.
                if job["kind"] == "environment_chat" and job.get("chat_id") in self.environment_outbox:
                    outgoing = self.environment_outbox[job["chat_id"]]
                    outgoing["attempts"] += 1
                    outgoing["retry_at"] = time.monotonic()+min(30, 3*outgoing["attempts"])
                if job["kind"] == "environment_action":
                    # An HTTP error can still have a source-linked failed result on the MOD.
                    # Keep its ID until the next observation, so a real failure can be learned.
                    self.session.plugin_interrupted("操作の通信が完了せず、成否は未確認。現在の状態を観察し直す。", retain_pending=True)
                self._status("plugin_error", str(exc))
                self._status("plugin_error_kind", job["kind"])
                self.environment_next_decision = time.monotonic()+5
                if job["kind"] in {"environment_connect", "environment_observe"}:
                    self._disconnect_environment()
        super()._finish_jobs()

    def schedule_environment(self):
        now = time.monotonic()
        if not self.environment_client:
            return
        seen = self.session.plugins.observation
        if not self.environment_io and seen:
            for identifier, outgoing in list(self.environment_outbox.items()):
                if (outgoing["world"], outgoing["session"]) != (seen.world_id, seen.session_id):
                    del self.environment_outbox[identifier]
                elif now >= outgoing["retry_at"]:
                    self._environment_job("environment_chat", self.environment_client.say, identifier,
                                          outgoing["text"], chat_id=identifier)
                    break
        if not self.environment_io and now >= self.environment_next_poll:
            self._environment_job("environment_observe", self.environment_client.observe)
            self.environment_next_poll = now+0.8
        state = self.session.plugins
        busy = bool(self.current and self._is_current(self.current["turn_id"]))
        observation = state.observation
        # Route addressed in-game messages through exactly the same dialogue/voice/memory owner.
        if observation and not busy and not self._voice_busy():
            for message in observation.messages:
                identifier = message.id
                if identifier in state.life.seen_messages:
                    continue
                state.life.seen_messages = (state.life.seen_messages+[identifier])[-64:]
                self._new_turn(message.text, "minecraft")
                self.session.update_turn(self.current["turn_id"], minecraft_message_id=identifier)
                busy = True
                break
        reason, phase = "", "connected"
        if not state.autonomous:
            phase, reason = "paused", "自律生活はOFFです。目的と経験は保存されています。"
        elif not observation:
            phase, reason = "observing", "現在の身体と周囲を取得しています。"
        elif observation.actor.dead:
            phase, reason = "dead", "Minecraftで死亡・退出しているため、再接続が必要です。"
        elif observation.action.get("paused"):
            phase, reason = "paused", "Minecraft側で停止されています。再接続後に再開してください。"
        elif busy or self._voice_busy() or self.session.status.get("audio_input", {}).get("asr_busy"):
            phase, reason = "conversation", "会話しています。生活の目的を保持して、返答後に再判断します。"
        elif observation.action.get("stage") == "running":
            phase, reason = "acting", "Minecraftで選んだ操作の結果を待っています。"
        elif self.environment_task:
            phase, reason = "deciding", "周囲・欲求・体験・会話から、目的と次の行動を考えています。"
        elif now < self.environment_next_decision:
            phase, reason = "waiting", "次の判断を待っています。" if not self.session.status.get("plugin_error") else "処理を再試行するまで待っています。"
        elif (state.life.wait_until > self.session.world.elapsed
              and state.life.wait_signature == situation_signature(observation)):
            phase, reason = "resting", state.life.last_reason or "今は様子を見ることを選びました。"
        elif self.environment_io not in {None, "environment_observe"}:
            phase, reason = "acting", "Minecraftへの要求を処理しています。"
        elif any(job["kind"] in {"reply", "memory", "deliberation"} for job in self.jobs.values()):
            phase, reason = "waiting", "会話・記憶の処理が終わったら生活の判断を再開します。"
        self._status("plugin_phase", phase)
        self._status("plugin_wait_reason", reason)
        if reason:
            return
        self._cancel_memory()
        cancellation = Cancellation()
        self.environment_task = {"cancellation": cancellation, "situation": decision_signature(observation)}
        self._status("plugin_phase", "deciding")
        taught = [{k: entry.get(k) for k in ("label", "value", "source_turn_id", "confidence")}
                  for entry in self.session.knowledge.context((current_goal(state).purpose if current_goal(state) else state.goal)+" "+" ".join(m.text for m in state.life.messages[-2:]), limit=4, char_limit=700)]
        self._job("llm", "environment_decision", decide, self.settings, self.session.active_manifest(),
            state.model_copy(deep=True), {**self.session.experience.context(), "availability": self.session.experience.state.availability,
                "allow_question": bool(self.session.experience.state.preferences.proactive
                    and self.session.experience.state.availability != "busy" and not self.session.experience.pending_question()
                    and not self.session.brain.active
                    and self.session.world.elapsed >= self.session.experience.state.preferences.quiet_until)},
            self.session.plugin_recent(), cancellation, taught,
            environment_generation=self.environment_generation, cancellation=cancellation)

    def stop(self):
        self._disconnect_environment()
        return super().stop()
