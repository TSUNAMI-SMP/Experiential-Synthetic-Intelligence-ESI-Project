"""Reuse the audio owner; apply language results on that owner before further decisions."""

from esi02.runtime import Runtime as PreviousRuntime
from esi03.dialogue import DialogueLLM


class Runtime(PreviousRuntime):
    def __init__(self, session, *args, **kwargs):
        kwargs.setdefault("llm", DialogueLLM(session.settings))
        super().__init__(session, *args, **kwargs)

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done() or job["kind"] != "reply" or not self._is_current(job.get("turn_id")):
                continue
            try:
                result = future.result()
            except Exception:
                continue  # Base owner applies the normal cancellation/error handling.
            self.session.dialogue_result(job["turn_id"], result)
        super()._finish_jobs()

    def _handle(self, name, data):
        if name == "character":
            operation, values = data
            self.session.character_operation(operation, values)
            if not self.session.experience.state.preferences.proactive:
                self._cancel_request_voice()
                self.session.motivation_operation("stop_requests", None)
            return
        super()._handle(name, data)
