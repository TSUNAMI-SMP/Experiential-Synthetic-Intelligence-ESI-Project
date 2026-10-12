"""Bounded, asynchronous orchestration; numerical and storage mutations have one owner."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from esi.adapters import LLM, TTS, play_wave
from esi.audio import Microphone, Perception
from esi.core import SPEECH, Core
from esi.models import Observation, Source


class Runtime:
    def __init__(self, core: Core, input_device=None, output_device=None):
        self.core = core
        self.settings = core.settings
        self.queue = queue.Queue(maxsize=self.settings.command_queue_capacity)
        self.halt = threading.Event()
        self.output_active = threading.Event()
        self.cancel_playback = threading.Event()
        self.llm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi-llm")
        self.audio_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi-asr")
        self.tts_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi-tts")
        self.jobs = {}
        self.pending = None
        self.overflow = 0
        self.perception = Perception(self.settings, core.store.path.parent / "audio")
        self.tts = TTS(self.settings, core.store.path.parent / "output")
        self.llm = LLM(self.settings)
        self.microphone = None
        self.thread = None
        self.input_device, self.output_device = input_device, output_device
        self.input_count = 0

    def send(self, name, data=None):
        try:
            self.queue.put_nowait((name, data))
            return True
        except queue.Full:
            self.overflow += 1
            return False

    def start(self, microphone=False, device=None):
        if self.thread and self.thread.is_alive():
            return
        self.halt.clear()
        self.thread = threading.Thread(target=self._loop, name="esi-core", daemon=True)
        self.thread.start()
        if microphone:
            self.send("microphone", self.input_device if device is None else device)

    def _activity(self, message):
        self.core.store.append("activity", {"message": message, "elapsed": self.core.elapsed})

    def _status(self, component, message):
        with self.core.lock:
            self.core.status[component] = message
            self.core.store.append(
                "technical_status", {"component": component, "message": message, "elapsed": self.core.elapsed}
            )
            if not isinstance(message, str):
                return
            if message.startswith("failed"):
                self._activity(f"問題が発生（{component}）：{message.removeprefix('failed: ')}")
            elif message.startswith("overflow"):
                self._activity(f"処理待ちが上限に達し、一部の入力を取り込めませんでした（{component}）。")
            elif component == "microphone":
                self._activity(
                    "マイクを開始しました。" if message.startswith("running") else "マイクを停止しました。"
                )
            elif component == "asr" and message.startswith("transcribing"):
                self._activity("声の区間を検出しました。音声を言葉に変換しています。")
            elif component == "llm" and message.startswith("interpreting"):
                self._activity("聞き取った内容の検討を開始しました。")

    def _job(self, pool, kind, function, *args, **metadata):
        future = pool.submit(function, *args)
        self.jobs[future] = {"kind": kind, "started_at": time.monotonic(), **metadata}
        return future

    def _dispatch(self, action):
        if action.kind not in SPEECH:
            return
        if self.settings.llm_mode == "off":
            self.core.finish(action.action_id, "failed")
            self._status("llm", "disabled: selected speech cannot be generated; no mock response")
            return
        if any(m["kind"] in {"interpret", "verbalize"} for m in self.jobs.values()):
            self.core.finish(action.action_id, "cancelled")
            self._status("llm", "speech cancelled: bounded inference slot busy")
            return
        self.core.stage(action.action_id, "generating")
        self._status("llm", "generating speech text")
        self._activity("発声を選択しました。話す文章を準備しています。")
        self._job(
            self.llm_pool,
            "verbalize",
            self.llm.verbalize,
            self.core.context(action.event_id),
            action.model_copy(deep=True),
            action_id=action.action_id,
            version=self.core.context_version,
        )

    def _observation(self, event):
        self.core.observe(event)
        if event.source not in {Source.MICROPHONE, Source.RECORDING, Source.SIMULATED}:
            return
        self.input_count += 1
        with self.core.lock:
            self.core.status["session_inputs"] = self.input_count
        self._activity(
            f"入力を受け取りました：{event.text[:180]}"
            if event.text
            else "声の区間を取り込みましたが、聞き取った文字は空でした。"
        )
        self.cancel_playback.set()
        for action in list(self.core.actions.values()):
            if action.kind in SPEECH and action.stage not in {"complete", "failed", "cancelled", "unknown"}:
                self.core.finish(action.action_id, "cancelled")
        if self.pending:
            self.core.store.append("inference_superseded", {"event_id": self.pending, "by": event.event_id})
        self.pending = event.event_id

    def _command(self, name, data):
        if name == "observe":
            self._observation(data)
        elif name == "feedback":
            self.core.feedback(data)
        elif name == "learning":
            self.core.learning(data)
        elif name == "save":
            self._status("checkpoint", self.core.checkpoint())
        elif name == "branch":
            self._status("branch", str(self.core.branch(data)))
        elif name == "status":
            self._status(*data)
        elif name == "microphone_status":
            capture_id, component, message = data
            if self.microphone and self.microphone.capture_id == capture_id:
                self._status(component, message)
        elif name == "recording":
            if any(j["kind"] == "recording" for j in self.jobs.values()):
                raise RuntimeError("Recording worker already busy")
            self._job(self.audio_pool, "recording", lambda p: list(self.perception.recording(p)), data)
            self._status("asr", "processing recording")
        elif name == "microphone":
            if self.microphone:
                self.microphone.close()
                self.microphone = None
                self._status("microphone", "stopped")
                with self.core.lock:
                    self.core.status["audio_input"] = {}
            else:
                mic = Microphone(
                    self.perception,
                    lambda e: self.send("observe", e),
                    lambda k, v: self.send("microphone_status", (mic.capture_id, k, v)),
                    self.output_active.is_set,
                    self.input_device if data is None else data,
                )
                try:
                    mic.start()
                except Exception:
                    mic.close()
                    raise
                self.microphone = mic
        else:
            raise ValueError("Unknown researcher operation")

    def _complete_jobs(self):
        for future, job in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[future]
            kind = job["kind"]
            seconds = time.monotonic() - job.get("started_at", time.monotonic())
            self.core.store.append(
                "worker_timing", {"kind": kind, "seconds": seconds, "elapsed": self.core.elapsed}
            )
            with self.core.lock:
                self.core.status[f"latency_{kind}"] = f"{seconds:.3f}s"
            aid = job.get("action_id")
            try:
                result = future.result()
                if kind in {"interpret", "verbalize"} and result._transport_trace:
                    self.core.store.append("llm_io", result._transport_trace)
                stale = job.get("version", self.core.context_version) != self.core.context_version
                terminal = aid and self.core.actions[aid].stage in {
                    "complete",
                    "failed",
                    "cancelled",
                    "unknown",
                }
                if (stale or terminal) and kind in {"interpret", "verbalize", "synthesis"}:
                    self.core.store.append(
                        "stale_result", {"kind": kind, "event_id": job.get("event_id"), "action_id": aid}
                    )
                    self._activity("新しい入力や処理期限により、前の処理結果を現在の判断に使いませんでした。")
                    continue
                if kind == "interpret":
                    if self.core.interpret(job["event_id"], result, job["version"]):
                        self._status("llm", "ready: real interpretation validated")
                        action = self.core.choose(job["event_id"])
                        from esi.presentation import ACTION_LABELS

                        self._activity(f"候補を比較し「{ACTION_LABELS[action.kind.value]}」を選びました。")
                        self._dispatch(action)
                elif kind == "verbalize":
                    self.core.store.append(
                        "speech_plan",
                        {"action_id": aid, "plan": result.model_dump(), "observed_truth": False},
                    )
                    self.core.stage(aid, "synthesizing")
                    self._status("tts", "synthesizing")
                    self._activity("文章ができました。凛音エルの声を作っています。")
                    self._job(
                        self.tts_pool,
                        "synthesis",
                        self.tts.synthesize,
                        result.text,
                        aid,
                        action_id=aid,
                        text=result.text,
                        version=job["version"],
                    )
                elif kind == "synthesis":
                    self._status("tts", "ready: real audio synthesized")
                    self._status(
                        "tts_voice",
                        f"{result['model_name']} / {result['style_name']} / {result['model_version']}",
                    )
                    if self.settings.playback:
                        self.cancel_playback.clear()
                        self.output_active.set()
                        self.core.stage(aid, "playing")
                        self._status("playback", "playing")
                        self._activity("選んだ出力機器で音声を再生しています。")
                        result["playback_device_id"] = self.output_device
                        self._job(
                            self.tts_pool,
                            "playback",
                            play_wave,
                            result["path"],
                            self.cancel_playback,
                            self.output_device,
                            result,
                            action_id=aid,
                            text=job["text"],
                            audio=result,
                            version=job["version"],
                        )
                    else:
                        self._speech_complete(aid, job["text"], result)
                elif kind == "playback":
                    self.output_active.clear()
                    self._status("playback", "ready" if result else "cancelled")
                    job["audio"]["played"] = bool(result)
                    self._speech_complete(
                        aid, job["text"], job["audio"], "complete" if result else "cancelled"
                    )
                elif kind == "recording":
                    for event in result:
                        self._observation(event)
                    self._status(
                        "asr", f"ready: {len(result)} real speech intervals; zero means no detected speech"
                    )
            except Exception as exc:
                if kind == "playback":
                    self.output_active.clear()
                self._status(
                    "asr"
                    if kind == "recording"
                    else "playback"
                    if kind == "playback"
                    else "tts"
                    if kind == "synthesis"
                    else "llm",
                    f"failed: {type(exc).__name__}: {exc}",
                )
                if aid:
                    self.core.finish(aid, "failed")
                elif kind == "interpret" and job.get("version") == self.core.context_version:
                    self.core.store.append(
                        "numeric_fallback", {"event_id": job["event_id"], "llm_failed": True}
                    )
                    self._dispatch(self.core.choose(job["event_id"]))

    def _speech_complete(self, action_id, text, audio, stage="complete"):
        self.core.finish(action_id, stage, text=text, audio_ref=audio["path"])
        self.core.observe(
            Observation(
                source=Source.SELF_OUTPUT,
                text=text,
                duration=audio["duration"],
                audio_ref=audio["path"],
                category="self_tts",
                metadata={
                    **audio,
                    "action_id": action_id,
                    "external_ground_truth": False,
                    "output_mode": "speaker" if audio["played"] else "wave_file",
                },
            )
        )
        self._activity(
            f"発声完了：{text}"
            if audio["played"]
            else f"音声ファイルを保存しました（再生OFF）：{text}"
            if stage == "complete"
            else "音声の再生を取り消しました。"
        )

    def _loop(self):
        last = time.monotonic()
        checkpoint_at, idle_at = self.core.elapsed, self.core.elapsed
        try:
            with self.core.lock:
                self.core.status.update(
                    {
                        "core": "running",
                        "microphone": "not_started",
                        "asr": "not_checked",
                        "vad": "not_checked",
                        "llm": "disabled" if self.settings.llm_mode == "off" else "not_checked",
                        "tts": "not_checked",
                        "playback": "not_started",
                        "operation": "ready",
                        "audio_input": {},
                        "session_inputs": 0,
                    }
                )
            self._activity("本体の処理を開始しました。")
            while not self.halt.is_set():
                now = time.monotonic()
                self.core.tick(now - last)
                last = now
                with self.core.lock:
                    self.core.status["queues"] = (
                        f"commands={self.queue.qsize()}, workers={len(self.jobs)}, output_active={self.output_active.is_set()}"
                    )
                    self.core.status["jobs"] = [
                        {
                            "kind": j["kind"],
                            "seconds": max(0, now - j["started_at"]),
                            "stale": j.get("version", self.core.context_version) != self.core.context_version,
                        }
                        for j in self.jobs.values()
                    ]
                    self.core.status["pending_input"] = self.pending
                    try:
                        with open("/proc/self/status") as process_status:
                            memory = next(
                                line.split()[1] for line in process_status if line.startswith("VmRSS:")
                            )
                        self.core.status["resident_memory"] = (
                            f"{int(memory) / 1024:.1f} MiB (ESI process only)"
                        )
                    except (OSError, StopIteration):
                        self.core.status["resident_memory"] = "not_measured"
                if self.overflow:
                    self._status("queue", f"overflow: {self.overflow} researcher/input commands dropped")
                    self.overflow = 0
                for _ in range(16):
                    try:
                        name, data = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._command(name, data)
                        if name not in {"status", "microphone_status"}:
                            with self.core.lock:
                                self.core.status["operation"] = "ready"
                    except Exception as exc:
                        self._status("operation", f"failed: {type(exc).__name__}: {exc}")
                self._complete_jobs()
                if self.pending and not any(
                    j["kind"] in {"interpret", "verbalize"} for j in self.jobs.values()
                ):
                    eid, self.pending = self.pending, None
                    if self.settings.llm_mode == "assist":
                        self._job(
                            self.llm_pool,
                            "interpret",
                            self.llm.interpret,
                            self.core.context(eid),
                            event_id=eid,
                            version=self.core.context_version,
                        )
                        self._status("llm", "interpreting; state and perception continue")
                    else:
                        self._dispatch(self.core.choose(eid))
                if self.core.elapsed - idle_at >= 3 and not self.pending:
                    self.core.choose()
                    idle_at = self.core.elapsed
                if self.core.elapsed - checkpoint_at >= self.settings.checkpoint_seconds:
                    self.core.checkpoint()
                    checkpoint_at = self.core.elapsed
                self.halt.wait(self.settings.tick_seconds)
        except Exception as exc:
            self._status("core", f"failed: {type(exc).__name__}: {exc}")
        finally:
            for action in list(self.core.actions.values()):
                if action.stage not in {"complete", "failed", "cancelled", "unknown"}:
                    self.core.finish(action.action_id, "cancelled")
            self.core.checkpoint()

    def stop(self):
        self.halt.set()
        self.cancel_playback.set()
        if self.microphone:
            self.microphone.close()
            self.microphone = None
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("Core thread did not stop; database left open")
        for pool in (self.llm_pool, self.audio_pool, self.tts_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        for future, job in self.jobs.items():
            self.core.store.append(
                "worker_cancelled",
                {
                    "kind": job["kind"],
                    "running_request_may_finish": future.running(),
                    "result_will_be_applied": False,
                },
            )
        self.core.status["core"] = "stopped"
        self.core.status["jobs"] = []
