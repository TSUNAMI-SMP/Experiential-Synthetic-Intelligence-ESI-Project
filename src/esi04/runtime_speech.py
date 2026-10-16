"""One audio lane for replies, spontaneous speech and real work announcements."""

import threading
import time
from collections import deque

from esi.models import uid
from esi02.dialogue import Cancelled
from esi04.speaking import work_text


class SpeakingRuntime:
    def __init__(self, *args, **kwargs):
        self.speech_queue = deque()
        self.aux_speech = None
        super().__init__(*args, **kwargs)

    def _voice_busy(self):
        return bool(self.aux_speech) or super()._voice_busy()

    def _generate(self, turn_id, text, history, cancellation, context):
        context = {**context, "_progress": lambda kind: self._emit(
            "work_progress", {"turn_id": turn_id, "kind": kind})}
        return super()._generate(turn_id, text, history, cancellation, context)

    def _cancel_aux_speech(self):
        while self.speech_queue:
            self._notice_stage(self.speech_queue.popleft(), "interrupted")
        if self.aux_speech:
            self.aux_speech["stop"].set()
            self._notice_stage(self.aux_speech, "interrupted")
        for entry in self.session.cognition.utterances:
            if entry.stage in {"displayed", "speaking"}:
                self.session.utterance_delivery(entry.id, "interrupted")
        self._status("spoken_work", "")

    def _cancel_current(self, reason):
        self._cancel_aux_speech()
        super()._cancel_current(reason)

    def _work_notice(self, kind, turn_id=None, task_id=None):
        prefs = self.session.experience.state.preferences
        if not prefs.processing_voice or (turn_id and not self._is_current(turn_id)):
            return
        text = work_text(kind, self.session.experience.state.character.style)
        if not text:
            return
        notices = self.session.turns[turn_id].get("processing_updates", []) if turn_id else []
        # One explanation per turn is enough; no repetitive narrated progress loop.
        if notices:
            return
        item = {"id": "work:"+uid(), "text": text, "kind": kind, "turn_id": turn_id,
                "task_id": task_id, "created": time.monotonic(), "stop": threading.Event(),
                "stage": "queued", "processing": True}
        if turn_id:
            self.session.update_turn(turn_id, processing_updates=[{
                "id": item["id"], "kind": kind, "text": text, "stage": "queued"}])
        self.speech_queue.append(item)

    def _notice_stage(self, item, stage, result=None):
        if item.get("utterance"):
            self.session.utterance_delivery(item["id"], stage, result)
        elif item.get("turn_id") in self.session.turns:
            turn_id = item["turn_id"]
            updates = self.session.turns[turn_id].get("processing_updates", [])
            self.session.update_turn(turn_id, processing_updates=[
                {**u, "stage": stage, **({"started": True} if stage == "speaking" else {}),
                 **({"audio": result} if result else {})} if u["id"] == item["id"] else u
                for u in updates])
        else:
            self.session.store.append("processing_speech", {"id": item["id"], "kind": item.get("kind"),
                                      "text": item["text"], "stage": stage, "is_factual_evidence": False})

    def _notice_live(self, item):
        if item.get("processing") and not self.session.experience.state.preferences.processing_voice:
            return False
        if item.get("utterance"):
            return (self.session.can_initiate_speech()
                    and not (self.current and self._is_current(self.current["turn_id"])))
        task_id = item.get("task_id")
        if task_id and not any(task_id == task.get("id") for task in (self.tool_task, self.computer_task) if task):
            return False
        if item.get("turn_id"):
            return self._is_current(item["turn_id"]) and not self.current["generation_done"] and not self.voice
        return (not (self.current and self._is_current(self.current["turn_id"])) and any(
            task_id == task.get("id") for task in (self.tool_task, self.computer_task) if task))

    def _pump_speech(self):
        if self.halt.is_set():
            return
        while self.speech_queue and not self._notice_live(self.speech_queue[0]):
            self._notice_stage(self.speech_queue.popleft(), "interrupted")
        if not self.speech_queue or self._voice_busy() or self.voice:
            return
        if self.session.status.get("audio_input", {}).get("asr_busy") or self.session.status.get("speech_problem"):
            return
        item = self.speech_queue[0]
        # Short jobs finish silently. This is a work-duration threshold, not a speech timer.
        if item.get("processing") and time.monotonic()-item["created"] < 1.5:
            return
        self.speech_queue.popleft()
        self.aux_speech = item
        self._notice_stage(item, "speaking")
        self._status("spoken_work", item["text"] if item.get("processing") else "")
        self._job("voice", "auxiliary_voice", self._speak,
                  {"turn_id": item["id"], "text": item["text"], "index": 0, "stop": item["stop"]}, item=item)

    def _schedule_request(self):
        # Replace the inherited request voice path completely. No answer/deadline machinery.
        if self.halt.is_set() or not self.session.can_initiate_speech():
            return
        queued_ids = {i["id"] for i in self.speech_queue}
        if self.aux_speech:
            queued_ids.add(self.aux_speech["id"])
        for entry in self.session.cognition.utterances:
            if entry.stage == "displayed" and entry.id not in queued_ids:
                self.speech_queue.append({"id": entry.id, "text": entry.text, "utterance": True,
                                          "created": time.monotonic(), "stop": threading.Event()})
        self._pump_speech()

    def _start_tool(self, action, arguments, goal_id=None):
        super()._start_tool(action, arguments, goal_id)
        # This method is reached only after permissions and actual job submission succeed.
        if self.tool_task:
            self._work_notice(action, task_id=self.tool_task["id"])

    def _handle(self, name, data):
        if name == "work_progress":
            self._work_notice(data["kind"], data["turn_id"])
            return
        super()._handle(name, data)
        if name == "character" and self.aux_speech:
            prefs = self.session.experience.state.preferences
            if ((self.aux_speech.get("processing") and not prefs.processing_voice)
                    or (self.aux_speech.get("utterance") and not prefs.proactive)):
                self.aux_speech["stop"].set()

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if job["kind"] != "auxiliary_voice" or not future.done():
                continue
            del self.jobs[future]
            item = job["item"]
            try:
                result = future.result()
                stage = "interrupted" if item["stop"].is_set() else "played" if result["played"] else "file_only"
                self._notice_stage(item, stage, result)
            except Cancelled:
                self._notice_stage(item, "interrupted")
            except Exception as exc:
                self._notice_stage(item, "interrupted" if item["stop"].is_set() else "failed")
                if not item["stop"].is_set():
                    # Losing an optional announcement must not discard a valid reply.
                    self._status("announcement_error", str(exc))
            self.aux_speech = None
            self._status("spoken_work", "")
        super()._finish_jobs()

    def stop(self):
        if self.aux_speech:
            self.aux_speech["stop"].set()
        super().stop()
