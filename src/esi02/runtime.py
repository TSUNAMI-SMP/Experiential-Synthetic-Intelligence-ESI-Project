"""Responsive conversation owner; background observations never invalidate a turn."""

import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from esi.adapters import TTS, play_wave
from esi.audio import Microphone
from esi.models import Observation
from esi02.dialogue import Cancellation, Cancelled, DialogueLLM
from esi02.memory_llm import MemoryLLM
from esi02.perception import AcceleratedPerception
from esi02.session import TERMINAL


class Runtime:
    def __init__(
        self,
        session,
        input_device=None,
        output_device=None,
        llm=None,
        tts=None,
        perception=None,
        playback=play_wave,
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
        self.thread = None
        self.llm = llm or DialogueLLM(self.settings)
        self.memory_llm = memory_llm or MemoryLLM(self.settings)
        self.memory_task = None
        self.tts = tts or TTS(self.settings.audio_settings(), session.store.path.parent / "output")
        self.perception = perception or AcceleratedPerception(
            self.settings,
            session.store.path.parent / "audio",
            lambda state: self._emit("status", ("asr_device", state)),
        )
        self.playback = playback
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

    def start(self, microphone=False):
        if self.thread:
            raise RuntimeError("停止後は新しいRuntimeで再開してください。")
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
        if self.current:
            self.current["cancellation"].set()
            self.current["voice_stop"].set()
            turn_id = self.current["turn_id"]
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
        self._cancel_current("新しい会話入力")
        turn = self.session.create_turn(text, source, observation.event_id if observation else None)
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
        }
        self._status("phase", "queued")

    def _cancel_memory(self):
        if self.memory_task:
            self.memory_task["cancellation"].set()

    def _status(self, name, value):
        with self.session.lock:
            self.session.status[name] = value
            if isinstance(value, str) and value.startswith("failed"):
                self.session.status["error"] = f"{name}：{value.removeprefix('failed: ')}"

    def _is_current(self, turn_id):
        return bool(
            self.current
            and self.current["turn_id"] == turn_id
            and self.session.turns[turn_id]["stage"] not in TERMINAL
        )

    def _handle(self, name, data):
        if name == "text":
            self._new_turn(data)
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
            self._status("environment", data)
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
                    lambda k, v: self._emit("microphone_status", (mic.capture_id, k, v)),
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
                if len(self.voice) >= self.settings.speech_capacity:
                    raise RuntimeError("音声の処理待ちが上限に達しました。返答を中止します。")
                index = self.current["sentences"]
                self.current["sentences"] += 1
                self.voice.append(
                    {"turn_id": turn_id, "index": index, "text": value, "stop": self.current["voice_stop"]}
                )
        elif name == "voice_phase":
            turn_id, phase = data
            if self._is_current(turn_id):
                self.session.update_turn(turn_id, stage=phase)
                self._status("phase", phase)
                if phase == "playing" and self.current["first_voice"] is None:
                    self.current["first_voice"] = time.monotonic() - self.current["accepted_at"]
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
        self._emit("voice_phase", (turn_id, "synthesizing"))
        started = time.monotonic()
        audio = self.tts.synthesize(item["text"], f"{turn_id}-{item['index']}")
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
                self._emit("voice_phase", (turn_id, "playing"))
                audio["played"] = bool(self.playback(audio["path"], stop, self.output_device, audio))
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

    def _finish_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[future]
            turn_id = job.get("turn_id")
            try:
                result = future.result()
                if job["kind"] == "memory":
                    self.memory_task = None
                    if not job["cancellation"].event.is_set():
                        with self.session.lock:
                            applied = self.session.memory.apply(
                                job["request"], result["items"], "Qwen3-4B", job["revision"]
                            )
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
                        self.session.add_chunk(turn_id, result)
            except Cancelled:
                if job["kind"] == "memory":
                    self.memory_task = None
                    self._status("memory_phase", "waiting")
            except Exception as exc:
                if job["kind"] == "memory":
                    self.memory_task = None
                    if not job["cancellation"].event.is_set():
                        self.session.memory.finish(job["request"]["id"], str(exc))
                        self._status("memory_error", f"記憶の整理：{exc}")
                    self._status("memory_phase", "waiting")
                    continue
                active = self._is_current(turn_id)
                if active:
                    self._cancel_current("処理の失敗")
                    self.session.update_turn(turn_id, stage="failed", error=f"{type(exc).__name__}: {exc}")
                    self._status("phase", "failed")
                if active or job["kind"] == "recording":
                    self._status("error", f"{job['kind']}：{exc}")
                else:
                    self.session.store.append("stale_worker_error", {"turn_id": turn_id, "error": str(exc)})

    def _schedule(self):
        if not self.current or not self._is_current(self.current["turn_id"]):
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
            if turn["input"].strip():
                self._job(
                    "llm",
                    "reply",
                    self._generate,
                    turn_id,
                    turn["input"],
                    self.session.history(turn_id),
                    current["cancellation"],
                    {"memories": self.session.memory.context(turn["input"])},
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
        if self.voice and not any(j["kind"] == "voice" for j in self.jobs.values()):
            item = self.voice.popleft()
            self._job("voice", "voice", self._speak, item, turn_id=item["turn_id"])
        active = any(
            j.get("turn_id") == turn_id and j["kind"] in {"reply", "voice"} for j in self.jobs.values()
        )
        if current["generation_done"] and not self.voice and not active:
            self.session.update_turn(
                turn_id,
                stage="complete",
                metrics={
                    "first_token_seconds": current["first_token"],
                    "playback_requested_seconds": current["first_voice"],
                    "total_seconds": time.monotonic() - current["accepted_at"],
                },
            )
            self._status("phase", "waiting")

    def _schedule_memory(self):
        if self.halt.is_set() or any(j["kind"] in {"reply", "memory"} for j in self.jobs.values()):
            return
        pending = self.session.memory.pending(eligible_only=True)
        if not pending:
            return
        request = pending[0]
        cancellation = Cancellation()
        existing = self.session.memory.search(request["input"], limit=12)
        self.memory_task = {"cancellation": cancellation}
        self._status("memory_phase", "organizing")
        self._job(
            "llm",
            "memory",
            self.memory_llm.extract,
            request,
            existing,
            cancellation,
            request=request,
            revision=self.session.memory.revision,
            cancellation=cancellation,
        )

    def _loop(self):
        last, checkpoint = time.monotonic(), self.session.elapsed
        self._status("running", True)
        self._status("phase", "waiting")
        try:
            while not self.halt.is_set():
                now = time.monotonic()
                with self.session.lock:
                    self.session.elapsed += now - last
                last = now
                for _ in range(64):
                    try:
                        name, data = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle(name, data)
                    except Exception as exc:
                        if name in {"delta", "sentence", "voice_phase"} and self.current:
                            self._cancel_current("返答の処理が失敗")
                            self.session.update_turn(self.current["turn_id"], stage="failed", error=str(exc))
                        self._status("error", f"{name}：{exc}")
                        self._status("phase", "failed")
                self._finish_jobs()
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
            self._cancel_memory()
            self._status("memory_phase", "stopped")
            self._cancel_current("停止")
            self._status("running", False)
            self._status("phase", "stopped")
            self.session.checkpoint()

    def stop(self):
        self.accepting = False
        self.halt.set()
        self._cancel_memory()
        self._cancel_current("停止")
        if self.microphone:
            self.microphone.close()
            self.microphone = None
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("本体を停止できません。セーブを閉じずに状態を確認してください。")
        for pool in self.pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        self._status("microphone", "stopped")
