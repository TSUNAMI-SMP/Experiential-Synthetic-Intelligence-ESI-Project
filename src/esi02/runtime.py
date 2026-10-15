"""Responsive conversation owner; background observations never invalidate a turn."""

import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import httpx

from esi.adapters import TTS
from esi.audio import Microphone
from esi.models import Observation
from esi02.dialogue import Cancellation, Cancelled, DialogueLLM, Sentences
from esi02.memory_llm import MemoryLLM
from esi02.perception import AcceleratedPerception
from esi02.playback import PlaybackMeter, play_wave
from esi02.session import TERMINAL
from esi02.speech import SpeechFailure, issue


class Runtime:
    def __init__(
        self,
        session,
        input_device=None,
        output_device=None,
        llm=None,
        tts=None,
        perception=None,
        playback=None,
        memory_llm=None,
    ):
        self.session, self.settings = session, session.settings
        self.input_device, self.output_device = input_device, output_device
        self.queue = queue.Queue(maxsize=self.settings.command_capacity)
        self.halt, self.output_active = threading.Event(), threading.Event()
        self.jobs = {}
        self.voice = deque()
        self.current = None
        self.microphone = None
        self.pending_recognition = None
        self.thread = None
        self.llm = llm or DialogueLLM(self.settings)
        self.memory_llm = memory_llm or MemoryLLM(self.settings)
        if memory_llm is None:
            self.memory_llm.on_started = lambda identifier: self._emit(
                "status", ("memory_stream_started", identifier)
            )
        self.memory_task = None
        self.request_voice = None
        self.tts = tts or TTS(self.settings.audio_settings(), session.store.path.parent / "output")
        self.perception = perception or AcceleratedPerception(
            self.settings,
            session.store.path.parent / "audio",
            lambda state: self._telemetry("status", ("asr_device", state)),
        )
        self.playback_meter = PlaybackMeter()
        self.playback = playback or (
            lambda path, stop, device, routing: play_wave(path, stop, device, routing, self.playback_meter)
        )
        self.pools = {
            name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"esi02-{name}")
            for name in ("llm", "voice", "recording")
        }
        self.accepting = False
        self.outgoing = deque(maxlen=16)

    def send(self, name, data=None):
        if not self.accepting or self.halt.is_set():
            return False
        try:
            self.queue.put_nowait((name, data))
            return True
        except queue.Full:
            return False

    def _emit(self, name, data):
        # Backpressure must be cancellable; worker completion never writes the session directly.
        while not self.halt.is_set():
            try:
                self.queue.put((name, data), timeout=0.1)
                return
            except queue.Full:
                pass
        raise Cancelled()

    def _telemetry(self, name, data):
        # Late device telemetry must not raise into PortAudio shutdown or native ASR cleanup.
        try:
            self._emit(name, data)
        except Cancelled:
            pass

    def start(self, microphone=False):
        if self.thread:
            raise RuntimeError("停止後は新しいRuntimeで再開してください。")
        # A restarted owner must not display the old failure or old ASR verification.
        for name in ("error", "control_error", "shutdown_error", "request_error"):
            self._status(name, "")
        self._status("asr_device", {})
        self._status("audio_input", {})
        self._status("audio_errors", {})
        self._status("speech_problem", {})
        self._status("speech_recovery", {})
        self.accepting = True
        self.thread = threading.Thread(target=self._loop, name="esi02-owner", daemon=True)
        self.thread.start()
        if microphone:
            if not self.send("microphone"):
                raise RuntimeError("マイクの開始を受け付けられませんでした。")

    def _job(self, pool, kind, function, *args, **metadata):
        future = self.pools[pool].submit(function, *args)
        self.jobs[future] = {"kind": kind, "started": time.monotonic(), **metadata}

    def _cancel_current(self, reason):
        current = self.current
        if current:
            current["cancellation"].set()
            current["voice_stop"].set()
            turn_id = current["turn_id"]
            if self.session.turns[turn_id]["stage"] not in TERMINAL:
                self.session.update_turn(turn_id, stage="cancelled", cancellation=reason)
        self.voice.clear()

    def _new_turn(self, text, source="text", observation=None):
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("入力は2000文字以内にしてください。")
        if observation:
            self.session.store.append("observation", observation.model_dump(mode="json"))
            if observation.source.value in {"self_output", "recall", "research_feedback"}:
                return
            # Conservative echo rejection: short answers and different barge-in remain accepted.
            normalized = "".join(c for c in text if c.isalnum())
            echoes = [
                item
                for item in self.outgoing
                if observation.mono_start <= item["end"] + 0.3
                and observation.mono_end >= item["start"] - 0.2
                and normalized == "".join(c for c in item["text"] if c.isalnum())
            ]
            if observation.metadata.get("tts_contamination_possible") and len(normalized) >= 4 and echoes:
                self.session.store.append("echo_suppressed", {"observation_id": observation.event_id})
                return
        self._cancel_memory()
        self._cancel_request_voice()
        self._cancel_current("新しい会話入力")
        confirmation = None
        uncertain = bool(observation and observation.metadata.get("recognition_needs_confirmation"))
        if self.pending_recognition and not uncertain and text.strip("。！! \n") in {
            "はい", "合っています", "合ってます", "あっています", "それで合っています", "その通りです",
        }:
            text, confirmation = self.pending_recognition
            source = "confirmed_recognition"
        self.pending_recognition = (text, observation.event_id) if uncertain and text.strip() else None
        turn = self.session.create_turn(
            text, source, confirmation or (observation.event_id if observation else None), remember=not uncertain
        )
        if uncertain:
            self.session.update_turn(turn["id"], recognition_needs_confirmation=True)
        if confirmation:
            self.session.update_turn(turn["id"], recognition_confirmation_of=confirmation)
        self.current = {
            "turn_id": turn["id"],
            "cancellation": Cancellation(),
            "voice_stop": threading.Event(),
            "generation_started": False,
            "generation_done": False,
            "sentences": 0,
            "accepted_at": time.monotonic(),
            "first_token": None,
            "first_voice": None,
            "speech_failed": False,
        }
        self._status("speech_problem", {})
        self._status("speech_recovery", {})
        self._status("phase", "queued")
        self._status("control_error", "")

    def _cancel_memory(self):
        if self.memory_task:
            self.memory_task["cancellation"].set()

    def _cancel_request_voice(self):
        request = self.request_voice
        if request:
            request["stop"].set()
            action = next((a for a in self.session.brain.state.actions if a.id == request["id"]), None)
            if action and action.delivered not in {"interrupted", "played", "file_only", "failed"}:
                self.session.motivation_operation("delivery", {"id": request["id"], "stage": "interrupted"})
        self._status("request_phase", "waiting")

    def _voice_busy(self):
        return any(j["kind"] in {"voice", "request_voice"} for j in self.jobs.values())

    def _status(self, name, value):
        with self.session.lock:
            self.session.status[name] = value
            if isinstance(value, str) and value.startswith("failed"):
                self.session.status["error"] = f"{name}：{value.removeprefix('failed: ')}"
                if name in {"asr", "vad", "microphone"}:
                    self.session.status.setdefault("audio_errors", {})[name] = self.session.status["error"]
                    self.session.store.append("audio_processing_error", {"component": name, "error": value[:1000]})
            elif (name in {"asr", "vad", "microphone"} and isinstance(value, str)
                  and value.startswith(("ready", "running"))):
                errors = self.session.status.setdefault("audio_errors", {})
                old_error = errors.pop(name, None)
                if old_error and self.session.status.get("error") == old_error:
                    self.session.status["error"] = next(reversed(errors.values()), "")

    def _is_current(self, turn_id):
        return bool(
            self.current
            and self.current["turn_id"] == turn_id
            and self.session.turns[turn_id]["stage"] not in TERMINAL
        )

    def _handle(self, name, data):
        if name == "text":
            self._new_turn(data)
        elif name == "retry_voice":
            self._retry_voice(data)
            self._status("control_error", "")
        elif name == "observe":
            if not isinstance(data, Observation):
                raise ValueError("音声の観測形式が正しくありません。")
            self._new_turn(data.text, data.source.value, data)
        elif name == "save":
            self.session.checkpoint()
            self._status("notice", "保存しました。")
        elif name == "memory":
            operation, values = data
            self._cancel_memory()
            if operation in {"edit", "delete", "add"}:
                self._cancel_current("記憶を画面で変更しました")
                self._status("phase", "waiting")
            self.session.memory_operation(operation, values)
        elif name == "environment":
            # This channel does not advance the conversation epoch or cancel its work.
            self.session.world_operation("environment", data)
            self._status("environment", data)
            self._status("control_error", "")
        elif name == "world":
            operation, values = data
            self.session.world_operation(operation, values)
            self._status("control_error", "")
        elif name == "motivation":
            operation, values = data
            self.session.motivation_operation(operation, values)
            self._status("control_error", "")
            active = self.session.brain.active
            if self.request_voice and (not active or active.id != self.request_voice["id"]):
                self._cancel_request_voice()
        elif name == "status":
            self._status(*data)
        elif name == "microphone_status":
            capture, component, value = data
            if self.microphone and self.microphone.capture_id == capture:
                self._status(component, value)
        elif name == "microphone":
            if self.microphone:
                self.microphone.close()
                self.microphone = None
                self._status("microphone", "stopped")
                self._status("audio_input", {})
            else:
                mic = Microphone(
                    self.perception,
                    lambda event: self._emit("observe", event),
                    lambda k, v: self._telemetry("microphone_status", (mic.capture_id, k, v)),
                    self.output_active.is_set,
                    self.input_device,
                )
                try:
                    mic.start()
                    self.microphone = mic
                except Exception:
                    mic.close()
                    raise
        elif name == "recording":
            if any(j["kind"] == "recording" for j in self.jobs.values()):
                raise RuntimeError("録音ファイルを処理中です。")
            self._job("recording", "recording", lambda path: list(self.perception.recording(path)), data)
        elif name in {"delta", "sentence"}:
            turn_id, value = data
            if not self._is_current(turn_id):
                return
            if name == "delta":
                turn = self.session.turns[turn_id]
                self.session.update_turn(turn_id, reply=turn["reply"] + value)
                if self.current["first_token"] is None:
                    self.current["first_token"] = time.monotonic() - self.current["accepted_at"]
            else:
                if len(self.voice) >= self.settings.speech_capacity and not self.current.get("speech_failed"):
                    raise RuntimeError("音声の処理待ちが上限に達しました。返答を中止します。")
                index = self.current["sentences"]
                self.current["sentences"] += 1
                plan = self.session.turns[turn_id].get("speech_plan", []) + [{"index": index, "text": value}]
                self.session.update_turn(turn_id, speech_plan=plan)
                if not self.current.get("speech_failed"):
                    self.voice.append(
                        {"turn_id": turn_id, "index": index, "text": value, "stop": self.current["voice_stop"]}
                    )
        elif name == "voice_phase":
            turn_id, phase = data
            if self._is_current(turn_id) and not self.current.get("speech_failed"):
                self.session.update_turn(turn_id, stage=phase)
                self._status("phase", phase)
                if phase == "playing" and self.current["first_voice"] is None:
                    self.current["first_voice"] = time.monotonic() - self.current["accepted_at"]
        elif name == "request_voice_phase":
            request_id, phase = data
            request = self.request_voice
            action = next((a for a in self.session.brain.state.actions if a.id == request_id), None)
            if (
                request
                and request["id"] == request_id
                and not request["stop"].is_set()
                and action
                and action.delivered not in {"played", "file_only", "interrupted", "failed"}
            ):
                self.session.motivation_operation("delivery", {"id": request_id, "stage": phase})
                self._status("request_phase", phase)
        elif name == "outgoing":
            self.outgoing.append(data)
        else:
            raise ValueError("未対応の操作です。")

    def _generate(self, turn_id, text, history, cancellation, context):
        return self.llm.stream(
            text,
            history,
            cancellation,
            lambda s: self._emit("delta", (turn_id, s)),
            lambda s: self._emit("sentence", (turn_id, s)),
            context=context,
        )

    def _speak(self, item):
        stop, turn_id = item["stop"], item["turn_id"]
        if stop.is_set():
            raise Cancelled()
        phase_event = "request_voice_phase" if item.get("request") else "voice_phase"
        self._emit(phase_event, (turn_id, "synthesizing"))
        started = time.monotonic()
        audio = item.get("cached_audio")
        if audio is None:
            audio = self._synthesize(item)
        synthesized = time.monotonic() - started
        if stop.is_set():
            raise Cancelled()
        audio["played"] = False
        if self.settings.playback:
            self.output_active.set()
            self._emit(
                "outgoing",
                {
                    "text": item["text"],
                    "start": time.monotonic(),
                    "end": time.monotonic() + audio["duration"],
                },
            )
            try:
                self._emit(phase_event, (turn_id, "playing"))
                try:
                    audio["played"] = bool(self.playback(audio["path"], stop, self.output_device, audio))
                    if not audio["played"] and not stop.is_set():
                        raise RuntimeError("スピーカーへの再生が完了しませんでした。")
                except Exception as exc:
                    if stop.is_set():
                        raise Cancelled() from exc
                    raise SpeechFailure(exc, "playback", audio) from exc
            finally:
                self.output_active.clear()
        if stop.is_set():
            raise Cancelled()
        return {
            "text": item["text"],
            "index": item["index"],
            "played": audio["played"],
            "audio": audio,
            "synthesis_seconds": synthesized,
            "external_evidence": False,
        }

    def _synthesize(self, item):
        stop = item["stop"]
        for attempt in range(2):
            if stop.is_set():
                raise Cancelled()
            try:
                return self.tts.synthesize(item["text"], f"{item['turn_id']}-{item['index']}")
            except Cancelled:
                raise
            except Exception as exc:
                problem = issue(exc, "synthesis")
                retry = False
                if not attempt:
                    if problem["kind"] == "memory":
                        release = getattr(self.perception, "release_gpu_for_speech", None)
                        retry = bool(problem["gpu_memory"] and release and release(stop))
                    else:
                        retry = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) or problem["http_status"] in {429, 502, 503, 504}
                if not retry:
                    if stop.is_set():
                        raise Cancelled() from exc
                    raise SpeechFailure(exc, "synthesis") from exc
                self._telemetry("status", ("speech_recovery", {"stage": "synthesis", "kind": problem["kind"], "attempt": 2}))
                if stop.wait(.25):
                    raise Cancelled() from exc

    def _save_voice_chunk(self, turn_id, chunk):
        chunks = [c for c in self.session.turns[turn_id]["chunks"] if c.get("index") != chunk["index"]]
        self.session.update_turn(turn_id, chunks=sorted(chunks+[chunk], key=lambda c: c["index"]))

    def _voice_failed(self, turn_id, job, exc):
        problem = exc.problem if isinstance(exc, SpeechFailure) else issue(exc, "synthesis")
        item = job.get("item", {"index": 0, "text": self.session.turns[turn_id]["reply"]})
        audio = exc.audio if isinstance(exc, SpeechFailure) else None
        self._save_voice_chunk(turn_id, {"index": item["index"], "text": item["text"], "played": False,
                                        "audio": audio or {}, "speech_error": problem, "external_evidence": False})
        self.current["speech_failed"] = True
        self.current["voice_stop"].set()
        self.voice.clear()
        self.playback_meter.clear()
        self.session.update_turn(turn_id, speech_error=problem, speech_status="failed")
        if not self.current["generation_done"]:
            self.session.update_turn(turn_id, stage="generating")
            self._status("phase", "generating")
        self.session.store.append("speech_failure", {"turn_id": turn_id, **problem})
        self._status("speech_problem", problem)

    def _retry_voice(self, turn_id):
        from pathlib import Path

        if (self.current and self._is_current(self.current["turn_id"])) or self._voice_busy():
            raise RuntimeError("返答・発声が終わってから、音声を再試行してください。")
        turn = self.session.turns.get(turn_id)
        if not turn or not turn["reply"].strip():
            raise ValueError("読み上げる返答がありません。")
        if not self.settings.playback:
            raise ValueError("音声再生がOFFです。設定を確認してください。")
        plan = turn.get("speech_plan", [])
        if not plan:
            splitter = Sentences(limit=220)
            plan = [{"index": index, "text": text} for index, text in enumerate(splitter.feed(turn["reply"])+splitter.finish())]
        stop = threading.Event()
        self.current = {"turn_id": turn_id, "cancellation": Cancellation(), "voice_stop": stop,
                        "generation_started": True, "generation_done": True, "sentences": len(plan),
                        "accepted_at": time.monotonic(), "first_token": turn["metrics"].get("first_token_seconds"),
                        "first_voice": None, "speech_failed": False}
        self.voice.clear()
        for item in plan:
            audio = next((c["audio"] for c in turn["chunks"] if c.get("text") == item["text"]
                          and c.get("audio", {}).get("path") and Path(c["audio"]["path"]).is_file()), None)
            self.voice.append({**item, "turn_id": turn_id, "stop": stop, "cached_audio": dict(audio) if audio else None})
        self.session.update_turn(turn_id, stage="queued", error=None, speech_error=None, speech_status="retrying",
                                 speech_retries=turn.get("speech_retries", 0)+1)
        self._status("speech_problem", {})
        self._status("error", "")
        self._status("phase", "queued")

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[future]
            turn_id = job.get("turn_id")
            try:
                result = future.result()
                if job["kind"] == "request_voice":
                    request = self.request_voice
                    if request and request["id"] == turn_id and not request["stop"].is_set():
                        self.session.motivation_operation(
                            "delivery",
                            {
                                "id": turn_id,
                                "stage": "played" if result["played"] else "file_only",
                                "audio": result,
                            },
                        )
                        self._status("request_phase", "waiting")
                    if request and request["id"] == turn_id:
                        self.request_voice = None
                elif job["kind"] == "memory":
                    self.memory_task = None
                    if not job["cancellation"].event.is_set():
                        with self.session.lock:
                            applied = self.session.memory.apply(
                                job["request"], [i for i in result["items"] if i["kind"] != "knowledge"],
                                "Qwen3-4B", job["revision"]
                            )
                            learned = self.session.knowledge.apply(
                                job["request"], [i for i in result["items"] if i["kind"] == "knowledge"],
                                "Qwen3-4B", job["knowledge_revision"]
                            )
                            applied = applied and learned
                            if applied:
                                rejected = result.get("rejected", [])
                                self.session.store.append(
                                    "memory_extraction",
                                    {
                                        "turn_id": job["request"]["id"],
                                        "accepted_count": len(result["items"]),
                                        "rejected": rejected,
                                        "external_evidence": False,
                                    },
                                )
                                self.session.memory.finish(
                                    job["request"]["id"],
                                    "発言と照合できない項目を除外しました。" if rejected else None,
                                )
                        self._status("memory_phase", "waiting")
                        self._status(
                            "memory_error",
                            "発言と照合できない記憶候補を除外しました。" if result.get("rejected") else "",
                        )
                elif job["kind"] == "recording":
                    for event in result:
                        self._new_turn(event.text, event.source.value, event)
                elif self._is_current(turn_id):
                    if job["kind"] == "reply":
                        self.current["generation_done"] = True
                        self.session.store.append("llm_io", {"turn_id": turn_id, **result})
                        self.session.update_turn(turn_id, finish_reason=result["finish_reason"])
                    elif job["kind"] == "voice":
                        self._save_voice_chunk(turn_id, result)
            except Cancelled:
                if job["kind"] == "memory":
                    self.memory_task = None
                    self._status("memory_phase", "waiting")
                elif job["kind"] == "request_voice":
                    if self.request_voice and self.request_voice["id"] == turn_id:
                        self.request_voice = None
                    self._status("request_phase", "waiting")
            except Exception as exc:
                if job["kind"] == "request_voice":
                    request = self.request_voice
                    if request and request["id"] == turn_id and not request["stop"].is_set():
                        self.session.motivation_operation("delivery", {"id": turn_id, "stage": "failed"})
                        self._status("request_error", str(exc))
                        if isinstance(exc, SpeechFailure):
                            self._status("speech_problem", exc.problem)
                            self.session.store.append("speech_failure", {"request_id": turn_id, **exc.problem})
                    self.request_voice = None
                    self._status("request_phase", "waiting")
                    continue
                if job["kind"] == "memory":
                    self.memory_task = None
                    if not job["cancellation"].event.is_set():
                        self.session.memory.finish(job["request"]["id"], str(exc))
                        self._status("memory_error", f"記憶の整理：{exc}")
                    self._status("memory_phase", "waiting")
                    continue
                active = self._is_current(turn_id)
                if active and job["kind"] == "voice":
                    self._voice_failed(turn_id, job, exc)
                    continue
                if active:
                    self._cancel_current("処理の失敗")
                    self.session.update_turn(turn_id, stage="failed", error=f"{type(exc).__name__}: {exc}")
                    self._status("phase", "failed")
                if active or job["kind"] == "recording":
                    self._status("error", f"{job['kind']}：{exc}")
                else:
                    self.session.store.append("stale_worker_error", {"turn_id": turn_id, "error": str(exc)})

    def _schedule(self):
        if self.halt.is_set():
            return
        if not self.current or not self._is_current(self.current["turn_id"]):
            self._schedule_request()
            self._schedule_memory()
            return
        current, turn_id = self.current, self.current["turn_id"]
        turn = self.session.turns[turn_id]
        if not current["generation_started"] and not any(
            j["kind"] in {"reply", "memory"} for j in self.jobs.values()
        ):
            current["generation_started"] = True
            self.session.update_turn(turn_id, stage="generating")
            self._status("phase", "generating")
            self._status("error", "")
            if turn.get("recognition_needs_confirmation") and turn["input"].strip():
                text = f"「{turn['input'][:100]}」と聞こえました。合っていますか。違う場合は言い直すか、入力欄で直してください。"
                self.session.update_turn(turn_id, reply=text, finish_reason="recognition_confirmation")
                current["generation_done"] = True
                current["sentences"] = 1
                self.voice.append({"turn_id": turn_id, "index": 0, "text": text, "stop": current["voice_stop"]})
            elif turn["input"].strip():
                self._job(
                    "llm",
                    "reply",
                    self._generate,
                    turn_id,
                    turn["input"],
                    self.session.history(turn_id),
                    current["cancellation"],
                    self.session.dialogue_context(turn["input"]),
                    turn_id=turn_id,
                )
            else:
                # A measured recognition failure is clarified, not an invented model response.
                text = "すみません、聞き取れませんでした。もう一度話してもらえますか。"
                self.session.update_turn(turn_id, reply=text, finish_reason="recognition_clarification")
                current["generation_done"] = True
                current["sentences"] = 1
                self.voice.append(
                    {"turn_id": turn_id, "index": 0, "text": text, "stop": current["voice_stop"]}
                )
        if self.voice and not self._voice_busy():
            item = self.voice.popleft()
            self._job("voice", "voice", self._speak, item, turn_id=item["turn_id"], item=item)
        active = any(
            j.get("turn_id") == turn_id and j["kind"] in {"reply", "voice"} for j in self.jobs.values()
        )
        if current["generation_done"] and not self.voice and not active:
            self.session.update_turn(
                turn_id,
                stage="failed" if current.get("speech_failed") else "complete",
                error=self.session.turns[turn_id].get("speech_error", {}).get("message") if current.get("speech_failed") else None,
                speech_status="failed" if current.get("speech_failed") else "played" if self.settings.playback else "file_only",
                metrics={
                    "first_token_seconds": current["first_token"],
                    "playback_requested_seconds": current["first_voice"],
                    "total_seconds": time.monotonic() - current["accepted_at"],
                },
            )
            self._status("phase", "speech_failed" if current.get("speech_failed") else "waiting")

    def _schedule_request(self):
        if (
            self.halt.is_set()
            or self._voice_busy()
            or self.session.status.get("audio_input", {}).get("asr_busy")
            or any(j["kind"] == "recording" for j in self.jobs.values())
            or self.session.status.get("speech_problem")
        ):
            return
        active = self.session.brain.active
        if not active or not active.text or active.delivered != "not_sent":
            return
        stop = threading.Event()
        self.request_voice = {"id": active.id, "stop": stop}
        self._status("request_error", "")
        self._job(
            "voice",
            "request_voice",
            self._speak,
            {"turn_id": active.id, "index": 0, "text": active.text, "stop": stop, "request": True},
            turn_id=active.id,
        )

    def _schedule_memory(self):
        if self.halt.is_set() or any(j["kind"] in {"reply", "memory"} for j in self.jobs.values()):
            return
        pending = self.session.memory.pending(eligible_only=True)
        if not pending:
            return
        request = pending[0]
        cancellation = Cancellation()
        existing = (
            self.session.memory.search(request["input"], limit=8)
            + self.session.knowledge.search(request["input"], limit=4)
        )
        self.memory_task = {"cancellation": cancellation}
        self._status("memory_phase", "organizing")
        self._status("memory_stream_started", "")
        self._job(
            "llm",
            "memory",
            self.memory_llm.extract,
            request,
            existing,
            cancellation,
            request=request,
            revision=self.session.memory.revision,
            knowledge_revision=self.session.knowledge.revision,
            cancellation=cancellation,
        )

    def _loop(self):
        last, checkpoint, world_time = time.monotonic(), self.session.elapsed, 0.0
        self._status("running", True)
        self._status("phase", "waiting")
        try:
            while not self.halt.is_set():
                now = time.monotonic()
                with self.session.lock:
                    self.session.elapsed += now - last
                world_time += now - last
                last = now
                if world_time >= 0.5:
                    conversing = bool(self.current and self._is_current(self.current["turn_id"]))
                    allow_requests = (
                        not conversing
                        and not self._voice_busy()
                        and not self.session.status.get("audio_input", {}).get("asr_busy")
                        and not any(j["kind"] == "recording" for j in self.jobs.values())
                    )
                    self.session.advance_world(
                        min(60, world_time), conversing, autonomy=True, allow_requests=allow_requests
                    )
                    world_time = 0.0
                for _ in range(64):
                    try:
                        name, data = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle(name, data)
                    except Exception as exc:
                        if name in {"environment", "world", "motivation", "retry_voice"}:
                            self._status("control_error", f"{name}：{exc}")
                            continue
                        if name in {"delta", "sentence", "voice_phase"} and self.current:
                            self._cancel_current("返答の処理が失敗")
                            self.session.update_turn(self.current["turn_id"], stage="failed", error=str(exc))
                        self._status("error", f"{name}：{exc}")
                        self._status("phase", "failed")
                self._finish_jobs()
                active_request = self.session.brain.active
                if self.request_voice and (
                    not active_request or active_request.id != self.request_voice["id"]
                ):
                    self._cancel_request_voice()
                # Completion is applied only after queued stream events have been consumed.
                if self.queue.empty():
                    self._schedule()
                with self.session.lock:
                    self.session.status["jobs"] = [
                        {"kind": j["kind"], "seconds": now - j["started"]} for j in self.jobs.values()
                    ]
                if self.session.elapsed - checkpoint >= self.settings.checkpoint_seconds:
                    self.session.checkpoint()
                    checkpoint = self.session.elapsed
                self.halt.wait(self.settings.tick_seconds)
        except Exception as exc:
            self._status("error", f"本体：{type(exc).__name__}: {exc}")
        finally:
            self.accepting = False
            self.halt.set()
            self._cancel_memory()
            self._status("memory_phase", "stopped")
            self._cancel_current("停止")
            self._cancel_request_voice()
            microphone, self.microphone = self.microphone, None
            if microphone:
                try:
                    microphone.close()
                except Exception as exc:
                    self._status("error", f"マイクの終了：{exc}")
            self._status("microphone", "stopped")
            self.session.motivation_operation("stop_requests", None)
            if world_time > 0:
                try:
                    self.session.advance_world(min(60, world_time), False)
                except Exception as exc:
                    self._status("shutdown_error", f"最後の身体更新を保存できません：{exc}")
            self.session.world.conversation_active = False
            self._status("running", False)
            self._status("phase", "stopped")
            for pool in self.pools.values():
                pool.shutdown(wait=False, cancel_futures=True)
            self.session.checkpoint()

    def stop(self):
        self.accepting = False
        self.halt.set()
        self.playback_meter.clear()
        self._cancel_memory()
        # Signal native playback promptly; only the owner writes the cancelled turn state.
        current = self.current
        if current:
            current["cancellation"].set()
            current["voice_stop"].set()
        if self.request_voice:
            self.request_voice["stop"].set()
        if not self.thread:
            self._cancel_current("停止")
            self._cancel_request_voice()
        if self.microphone:
            self.microphone.stop_event.set()
            if not self.thread:
                self.microphone.close()
                self.microphone = None
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("本体を停止できません。セーブを閉じずに状態を確認してください。")
        for pool in self.pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        self._status("microphone", "stopped")
