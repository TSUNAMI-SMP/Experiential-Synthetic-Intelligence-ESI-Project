"""Audio and research owner; workers return observations without writing character state."""

from concurrent.futures import ThreadPoolExecutor

from esi02.dialogue import Cancellation, Cancelled
from esi02.runtime import Runtime as PreviousRuntime
from esi03.dialogue import DialogueLLM
from esi03.perception import AcceleratedPerception
from esi03.tools import ToolRunner


class Runtime(PreviousRuntime):
    def __init__(self, session, *args, **kwargs):
        kwargs.setdefault("llm", DialogueLLM(session.settings))
        kwargs.setdefault(
            "perception",
            AcceleratedPerception(
                session.settings,
                session.store.path.parent / "audio",
                lambda state: self._telemetry("status", ("asr_device", state)),
            ),
        )
        super().__init__(session, *args, **kwargs)
        self.pools["tools"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi03-research")
        self.tool_task = None
        self.pending_note = None

    def _cancel_tools(self, record=True):
        if self.tool_task:
            self.tool_task["cancellation"].set()
            if record:
                self.session.research.cancel(self.tool_task["id"])

    def _new_turn(self, *args, **kwargs):
        self._cancel_tools()
        super()._new_turn(*args, **kwargs)

    def _start_tool(self, action, arguments, goal_id=None):
        if self.current and self._is_current(self.current["turn_id"]):
            raise RuntimeError("会話の返答が終わってから調査・PC操作を始めてください。")
        request = self.session.research.start(action, arguments, goal_id)
        cancellation = Cancellation()
        runner = ToolRunner(self.session.research.policy.model_copy(deep=True), cancellation)
        self.tool_task = {"id": request.id, "cancellation": cancellation}
        self._status("tools_phase", "running")
        self._job(
            "tools",
            "tool",
            runner.execute,
            action,
            arguments,
            request_id=request.id,
            cancellation=cancellation,
        )

    def _reply_result(self, job, result):
        self.session.dialogue_result(job["turn_id"], result)
        super()._reply_result(job, result)

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done() or job["kind"] != "tool":
                continue
            del self.jobs[future]
            self.tool_task = None
            try:
                result = future.result()
                if not job["cancellation"].event.is_set():
                    applied = self.session.research.finish(job["request_id"], result)
                    if applied and result.get("evidence") and self.session.research.policy.allow_save_note:
                        lines = ["# ESI-03 調査ノート", "資料の記述は未確認。真偽の確認とは別です。"]
                        for document in result["evidence"]:
                            lines.extend(
                                [
                                    "",
                                    document["title"],
                                    str(document.get("url") or document.get("path")),
                                    document["retrieved_at"],
                                    "SHA256: " + document["sha256"],
                                    document["text"][:2000],
                                ]
                            )
                        self.pending_note = "\n".join(lines)[:16000]
                self._status("tools_phase", "waiting")
            except Cancelled:
                self.session.research.cancel(job["request_id"])
                self._status("tools_phase", "waiting")
            except Exception as exc:
                self.session.research.finish(job["request_id"], error=str(exc))
                self._status("tools_error", str(exc))
                self._status("tools_phase", "failed")
        super()._finish_jobs()

    def _schedule(self):
        super()._schedule()
        if self.halt.is_set() or self.tool_task or any(job["kind"] == "tool" for job in self.jobs.values()):
            return
        if self.current and self._is_current(self.current["turn_id"]):
            return
        if self._voice_busy() or self.session.status.get("audio_input", {}).get("asr_busy"):
            return
        if self.pending_note:
            note, self.pending_note = self.pending_note, None
            try:
                self._start_tool("save_note", {"text": note})
            except (ValueError, PermissionError, RuntimeError) as exc:
                self._status("tools_error", str(exc))
            return
        experience = self.session.experience
        state, policy = experience.state, self.session.research.policy
        if (
            not state.preferences.exploration
            or not self.session.brain.state.enabled
            or state.elapsed < state.preferences.quiet_until
        ):
            return
        if (
            self.session.world.body.fatigue > 0.65
            or self.session.world.body.energy < 0.45
            or self.session.brain.active
        ):
            return
        usage = self.session.store.meta("esi03_tool_usage")
        from esi.models import utc

        if usage and usage["date"] == utc()[:10] and usage["count"] >= state.preferences.daily_tool_budget:
            return
        tool = "read_folder" if policy.read_roots else "web_search" if policy.web_enabled else None
        if tool is None:
            return
        goal = next(
            (goal for goal in state.goals if goal.stage == "pending" and goal.next_at <= state.elapsed), None
        )
        goal = goal or experience.plan_research(tool)
        if goal is None:
            return
        interest = next(item for item in state.interests if item.id == goal.interest_id)
        try:
            self._start_tool(goal.tool, {"query": interest.topic[:200]}, goal.id)
        except (ValueError, PermissionError, RuntimeError) as exc:
            goal.stage, goal.error = "failed", str(exc)[:600]
            self.session._write()
            self._status("tools_error", str(exc))

    def _handle(self, name, data):
        if name in {"character", "tool_policy", "tool", "research_interest", "stop_tools"}:
            try:
                self._control(name, data)
                self._status("control_error", "")
            except (ValueError, PermissionError, RuntimeError) as exc:
                self._status("control_error", str(exc))
            return
        super()._handle(name, data)

    def _control(self, name, data):
        if name == "research_interest":
            policy = self.session.research.policy
            tool = "read_folder" if policy.read_roots else "web_search" if policy.web_enabled else None
            if tool is None:
                raise PermissionError("読めるフォルダーかWebの範囲を設定してください。")
            goal = self.session.experience.plan_research(tool, data, manual=True)
            if goal is None:
                raise ValueError("この疑問は調査中か、見つかりません。")
            self._cancel_request_voice()
            self.session.motivation_operation("stop_requests", None)
            interest = next(
                item for item in self.session.experience.state.interests if item.id == goal.interest_id
            )
            try:
                self._start_tool(goal.tool, {"query": interest.topic[:200]}, goal.id)
            except Exception:
                goal.stage = "cancelled"
                self.session._write()
                raise
            return
        if name == "tool_policy":
            self._cancel_tools()
            self.session.research.configure(data)
            return
        if name == "tool":
            action, arguments = data
            self._start_tool(action, arguments)
            return
        if name == "stop_tools":
            self._cancel_tools()
            return
        if name == "character":
            operation, values = data
            self.session.character_operation(operation, values)
            if not self.session.experience.state.preferences.proactive or operation == "pause_interest":
                self._cancel_request_voice()
                self.session.motivation_operation("stop_requests", None)
            if not self.session.experience.state.preferences.exploration:
                self._cancel_tools()
            return

    def stop(self):
        task = self.tool_task
        self._cancel_tools(record=False)
        super().stop()
        if task:
            self.session.research.cancel(task["id"])
            self.session.checkpoint()
