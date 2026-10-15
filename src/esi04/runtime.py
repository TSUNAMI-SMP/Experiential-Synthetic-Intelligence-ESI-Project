"""ESI-04 owner: cancellable screen/PC work, idle deliberation and natural conversation."""

import re
from concurrent.futures import ThreadPoolExecutor

from esi02.dialogue import Cancellation, Cancelled
from esi02.runtime import Runtime as AudioRuntime
from esi03.runtime import Runtime as PreviousRuntime
from esi04.computer_agent import ComputerAgent
from esi04.deliberation import context as idle_context
from esi04.deliberation import deliberate
from esi04.dialogue import DialogueLLM

SCREEN_QUERY = re.compile(r"(?:この|今の|共有|見せ|画面|動画).*(?:画面|動画|見える|映って|何|どこ|どう|読んで|見て)|(?:画面|動画).*(?:教えて|説明|見て)")
PC_COMMAND = re.compile(r"(?:クリックして|ダブルクリックして|入力して|スクロールして|押して|操作して|開いて|実演.*(?:やって|再現)|動かして)")
HYPOTHETICAL = re.compile(r"できる|できますか|方法|例えば|仮に|したら|すれば")


class Runtime(PreviousRuntime):
    def __init__(self, session, *args, computer=None, services=None, **kwargs):
        kwargs.setdefault("llm", DialogueLLM(session.settings))
        super().__init__(session, *args, **kwargs)
        self.computer, self.services = computer, services
        self.pools["computer"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi04-computer")
        self.computer_task = None
        self.idle_task = None

    def _cancel_computer(self):
        if self.computer_task:
            self.computer_task["cancellation"].set()

    def _cancel_idle(self):
        if self.idle_task:
            self.idle_task["cancellation"].set()

    def _new_turn(self, text, source="text", observation=None):
        self._cancel_computer()
        self._cancel_idle()
        self.session.cognition.pending_computer_task = ""
        super()._new_turn(text, source, observation)
        if not self.current:
            return
        turn = self.session.turns[self.current["turn_id"]]
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return
        text = turn["input"]
        if self.computer and PC_COMMAND.search(text) and not HYPOTHETICAL.search(text):
            self.session.update_turn(turn["id"], computer_request=text)
            if self.computer.grant.control:
                self.session.cognition.pending_computer_task = text[:1000]
        elif self.computer and self.computer.grant.observe and SCREEN_QUERY.search(text):
            self.session.update_turn(turn["id"], screen_query=True)
        self._status("computer_access", bool(self.computer and self.computer.grant.control))

    def _start_computer(self, operation, values, turn_id=None):
        if not self.computer or not self.services:
            raise RuntimeError("画面・PC操作の接続がありません。")
        if self.computer_task or any(j["kind"] in {"computer", "screen_for_reply"} for j in self.jobs.values()):
            raise RuntimeError("画面認識・PC操作の処理中です。停止してから再開してください。")
        self._cancel_memory()
        self._cancel_idle()
        cancellation = Cancellation()
        agent = ComputerAgent(self.settings, self.computer, self.services)
        kind = "screen_for_reply" if turn_id else "computer"
        self.computer_task = {"operation": operation, "cancellation": cancellation}
        self._status("computer_phase", "observing" if operation == "observe" else "operating")
        if operation == "observe":
            function, arguments = agent.observe, (values.get("question", "画面には何が見える？"), cancellation)
        elif operation == "task":
            function, arguments = agent.run, (values["goal"], cancellation,
                                              lambda result: self._telemetry("computer_step", result))
        elif operation == "replay":
            skill = next((s for s in self.computer.memory.skills() if s.id == values["id"]), None)
            if skill is None:
                raise ValueError("その実演は見つかりません。")
            function, arguments = self.computer.replay, (skill.model_dump(), cancellation)
        else:
            self.computer_task = None
            raise ValueError("利用できないPC操作です。")
        self._job("computer", kind, function, *arguments, operation=operation, cancellation=cancellation,
                  turn_id=turn_id, generation=self.computer.grant.generation)

    def _handle(self, name, data):
        if name == "avatar_move":
            self.session.agency.summarize("アバターの位置を選ぶ", str(data["target"]), data["reason"],
                                          [data["frame"]], certainty="planned")
            self.session._write()
            return
        if name == "computer_step":
            result = data["result"]
            self.session.cognition.intention = "operate"
            self.session.agency.summarize("PCの次の操作", data["operation"], data["summary"],
                                          [result["before"], result["id"]], outcome=result["detail"],
                                          certainty="observed" if result["status"] == "verified" else "inferred")
            self.session._write()
            return
        if name == "scene":
            operation, values = data
            if operation == "approach":
                obj = self.session.world.get(values["id"])
                self.session.world_operation("move", self.session.world.approach(obj).model_dump())
            else:
                self.session.scene_operation(operation, values)
            self._status("control_error", "")
            return
        if name == "stop_computer":
            self._cancel_computer()
            self.session.cognition.pending_computer_task = ""
            return
        if name == "computer":
            operation, values = data
            if operation == "teaching":
                if not self.computer or not self.computer.grant.observe:
                    raise PermissionError("画面共有は停止しています。")
                self.session.screen_taught(values)
            elif operation == "skill_learned":
                skill = next(s for s in self.computer.memory.skills() if s.id == values["id"])
                self.session.agency.summarize("実演を教わる", skill.name, skill.user_goal,
                                              [skill.id], outcome=f"{len(skill.steps)}操作を前後の画面とともに記録",
                                              certainty="observed")
                self.session._write()
            else:
                self._start_computer(operation, values)
            return
        super()._handle(name, data)

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done() or job["kind"] not in {"computer", "screen_for_reply", "deliberation"}:
                continue
            del self.jobs[future]
            if job["kind"] == "deliberation":
                self.idle_task = None
            else:
                self.computer_task = None
            try:
                result = future.result()
                if job["cancellation"].event.is_set():
                    continue
                if job["kind"] == "deliberation":
                    if self.current and self._is_current(self.current["turn_id"]):
                        continue
                    if self.session.apply_focus(job["context_key"], result):
                        if result.action == "research":
                            policy = self.session.research.policy
                            if policy.web_enabled or policy.read_roots:
                                self._control("research_interest", result.target_id)
                        elif result.action == "practice" and self.computer and self.computer.grant.control:
                            self._start_computer("replay", {"id": result.target_id})
                    self._status("cognition_phase", "waiting")
                    continue
                if not self.computer or self.computer.grant.generation != job["generation"]:
                    continue
                if job["operation"] == "observe":
                    turn_id = job.get("turn_id")
                    if turn_id and not self._is_current(turn_id):
                        continue
                    self.session.screen_observed(result, turn_id)
                    self._status("screen_observation", result["observation"])
                    if turn_id:
                        self.current["generation_started"] = False
                else:
                    if job["operation"] == "replay":
                        result = {"stage": "verified" if result and result[-1].status == "verified" else "unverified",
                                  "summary": "実演を再現し、操作後の画面を取得した。",
                                  "results": [r.model_dump() for r in result]}
                    self.session.computer_finished(result)
                    self._status("computer_result", result)
                self._status("vision_backend", dict(self.services.vision_backend))
                self._status("computer_phase", "waiting")
            except Cancelled:
                if job["kind"] == "screen_for_reply" and self._is_current(job.get("turn_id")):
                    self.session.update_turn(job["turn_id"], screen_error="画面の観察を中止しました。")
                    self.current["generation_started"] = False
            except Exception as exc:
                name = "cognition_error" if job["kind"] == "deliberation" else "computer_error"
                self._status(name, str(exc))
                if job["kind"] == "deliberation":
                    self.session.agency.remember_processed(job["context_key"])
                    self.session._write()
                elif job["kind"] == "screen_for_reply" and self._is_current(job.get("turn_id")):
                    self.session.update_turn(job["turn_id"], screen_error=str(exc))
                    self.current["generation_started"] = False
        super()._finish_jobs()

    def _schedule_memory(self):
        if not self.computer_task and not self.idle_task:
            super()._schedule_memory()

    def _schedule(self):
        if self.current and self._is_current(self.current["turn_id"]):
            turn = self.session.turns[self.current["turn_id"]]
            if (turn.get("screen_query") and not turn.get("screen_observation") and not turn.get("screen_error")
                    and not self.current["generation_started"]):
                if self.computer_task:
                    return
                try:
                    self._start_computer("observe", {"question": turn["input"]}, turn["id"])
                    self.current["generation_started"] = True
                except Exception as exc:
                    self.session.update_turn(turn["id"], screen_error=str(exc))
                return
        if (self.session.cognition.pending_computer_task and self.computer and self.computer.grant.control
                and not self.computer_task and not self.tool_task
                and not (self.current and self._is_current(self.current["turn_id"]))):
            goal = self.session.cognition.pending_computer_task
            self.session.cognition.pending_computer_task = ""
            self._start_computer("task", {"goal": goal})
            return
        # Research does not start alongside PC activity or its use of the language model.
        if self.computer_task or self.idle_task:
            AudioRuntime._schedule(self)
        else:
            super()._schedule()
        if (self.halt.is_set() or self.computer_task or self.idle_task or self.tool_task
                or (self.current and self._is_current(self.current["turn_id"]))
                or self._voice_busy() or self.session.status.get("audio_input", {}).get("asr_busy")):
            return
        if self.session.cognition.pending_computer_task and self.computer and self.computer.grant.control:
            goal = self.session.cognition.pending_computer_task
            self.session.cognition.pending_computer_task = ""
            self._start_computer("task", {"goal": goal})
            return
        preferences, world = self.session.experience.state.preferences, self.session.world
        if (not self.settings.idle_deliberation or not preferences.idle_activity or not self.session.brain.state.enabled
                or self.session.brain.active or self.session.cognition.active_object or world.destination
                or any(j["kind"] in {"reply", "memory", "tool", "deliberation"} for j in self.jobs.values())):
            return
        skills = self.computer.memory.skills() if self.computer and self.computer.grant.control else []
        key, value = idle_context(self.session, skills)
        if key in self.session.cognition.processed:
            return
        cancellation = Cancellation()
        self.idle_task = {"cancellation": cancellation}
        self._status("cognition_phase", "considering")
        self._job("llm", "deliberation", deliberate, self.settings, value, cancellation,
                  context_key=key, cancellation=cancellation)

    def stop(self):
        self._cancel_computer()
        self._cancel_idle()
        if self.computer:
            self.computer.revoke()
        super().stop()
