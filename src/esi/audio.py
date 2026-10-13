"""CPU perception. Audio callbacks only copy into a bounded queue."""

import hashlib
import math
import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import correlate, find_peaks, resample_poly

from esi.devices import stream_format
from esi.models import AcousticFrame, Observation, Settings, Source, uid, utc

RATE = 16000
FRAME = 512


def mono16(audio, sample_rate):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.ndim != 1 or not np.isfinite(audio).all() or sample_rate <= 0:
        raise ValueError("Invalid audio samples/rate")
    if sample_rate != RATE:
        divisor = math.gcd(int(sample_rate), RATE)
        audio = resample_poly(audio, RATE // divisor, int(sample_rate) // divisor).astype(np.float32)
    return np.ascontiguousarray(audio)


def acoustics(audio, baseline=None):
    frames = []
    for offset in range(0, len(audio), FRAME):
        chunk = audio[offset : offset + FRAME].astype(float)
        rms = float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0
        f0 = None
        centered = chunk - np.mean(chunk)
        if rms > 0.002 and len(chunk) >= FRAME:
            corr = correlate(centered, centered, mode="full", method="fft")[len(chunk) - 1 :]
            peaks, _ = find_peaks(corr[RATE // 400 : RATE // 65])
            if len(peaks) and corr[0] > 0:
                lag = int(peaks[np.argmax(corr[peaks + RATE // 400])] + RATE // 400)
                if corr[lag] / corr[0] > 0.5:
                    f0 = RATE / lag
        frames.append(
            AcousticFrame(
                seconds=offset / RATE,
                rms=rms,
                normalized_rms=rms / max(baseline, 0.00001) if baseline else None,
                f0_hz=f0,
                voiced=f0 is not None,
            )
        )
    pitches = [f.f0_hz for f in frames if f.f0_hz is not None]
    summary = {
        "rms": float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0.0,
        "f0_hz": float(np.median(pitches)) if pitches else None,
        "voiced_fraction": sum(f.voiced for f in frames) / max(1, len(frames)),
    }
    return frames, summary


class Perception:
    def __init__(self, settings: Settings, audio_directory):
        self.settings = settings
        self.directory = Path(audio_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model = None
        self.model_sha256 = None
        self.lock = threading.Lock()
        self.baseline = None

    def _create_model(self):
        from faster_whisper import WhisperModel

        return WhisperModel(
            self.settings.asr_model_path,
            device="cpu",
            compute_type="int8",
            cpu_threads=self.settings.cpu_threads,
            num_workers=1,
            local_files_only=True,
        )

    def _transcribe(self, audio):
        segments, info = self.model.transcribe(audio, language="ja", beam_size=1, vad_filter=False)
        return list(segments), info

    def recognize(self, audio, source=Source.RECORDING, metadata=None, mono_start=None, wall_start=None):
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) == 0:
            raise ValueError("Empty speech segment")
        metadata = dict(metadata or {})
        duration = len(audio) / RATE
        started = time.monotonic() if mono_start is None else mono_start
        with self.lock:
            if self.model is None:
                if not Path(self.settings.asr_model_path, "model.bin").is_file():
                    raise FileNotFoundError(
                        "ASR model missing; configure a local faster-whisper small multilingual directory"
                    )
                with Path(self.settings.asr_model_path, "model.bin").open("rb") as source_file:
                    self.model_sha256 = hashlib.file_digest(source_file, "sha256").hexdigest()
                self.model = self._create_model()
            segments, info = self._transcribe(audio)
            rms = float(np.sqrt(np.mean(audio * audio)))
            baseline = self.baseline
            self.baseline = rms if baseline is None else 0.9 * baseline + 0.1 * rms
        text = "".join(s.text for s in segments).strip()
        frames, summary = acoustics(audio, baseline)
        summary["characters_per_second_estimate"] = len(text) / duration
        saved_bytes = sum(p.stat().st_size for p in self.directory.glob("*.wav"))
        if saved_bytes + len(audio) * 2 + 256 > self.settings.audio_limit_bytes:
            raise RuntimeError(
                "Audio retention quota reached; archive recordings using researcher controls before continuing"
            )
        eid = uid()
        path = self.directory / f"{eid}.wav"
        sf.write(path, audio, RATE, subtype="PCM_16")
        metadata.update(
            {
                "recognition_sample_rate": RATE,
                "asr_segments": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text,
                        "avg_logprob": s.avg_logprob,
                        "no_speech_prob": s.no_speech_prob,
                    }
                    for s in segments
                ],
                "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "confidence_calibrated": False,
                "asr_model_sha256": self.model_sha256,
                "asr_device": "cpu",
                "asr_compute_type": "int8",
            }
        )
        return Observation(
            event_id=eid,
            source=source,
            mono_start=started,
            mono_end=started + duration,
            wall_start=wall_start or utc(),
            wall_end=utc(),
            duration=duration,
            text=text,
            audio_ref=str(path.resolve()),
            acoustic_frames=frames,
            acoustic_summary=summary,
            category="speech",
            missing=["f0"] if not any(f.voiced for f in frames) else [],
            estimators={
                "asr": "faster-whisper-1.2.1/small multilingual",
                "acoustics": "ESI-01/autocorrelation-v1",
            },
            estimates={"language_probability_uncalibrated": info.language_probability},
            metadata=metadata,
        )

    def recording(self, filename):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        info = sf.info(filename)
        if info.duration > self.settings.recording_max_seconds:
            raise ValueError("Recording exceeds the configured duration limit; split it before importing")
        original, sr = sf.read(filename, dtype="float32", always_2d=True)
        audio = mono16(original, sr)
        regions = get_speech_timestamps(
            audio,
            VadOptions(
                min_silence_duration_ms=self.settings.vad_end_ms,
                max_speech_duration_s=self.settings.utterance_max_seconds,
                speech_pad_ms=100,
            ),
        )
        recording_id = uid()
        mono_start, wall_start = time.monotonic(), utc()
        for index, region in enumerate(regions):
            yield self.recognize(
                audio[region["start"] : region["end"]],
                metadata={
                    "recording_id": recording_id,
                    "segment_index": index,
                    "original_path": str(Path(filename).resolve()),
                    "original_sample_rate": sr,
                    "original_channels": original.shape[1],
                    "recording_start_seconds": region["start"] / RATE,
                    "recording_end_seconds": region["end"] / RATE,
                    "vad": "Silero v6 ONNX",
                    "is_recording_playback": True,
                },
                mono_start=mono_start + region["start"] / RATE,
                wall_start=wall_start,
            )


class Segmenter:
    """Streaming Silero v6 ONNX state; 32ms frames, bounded pre-roll and utterances."""

    def __init__(self, settings):
        from faster_whisper.vad import get_vad_model

        self.session = get_vad_model().session
        self.settings = settings
        self.h = np.zeros((1, 1, 128), dtype="float32")
        self.c = self.h.copy()
        self.context = np.zeros(64, dtype="float32")
        self.preroll = deque(maxlen=6)
        self.frames = []
        self.silent = 0
        self.utterance_id = None
        self.probability = 0.0

    def feed(self, frame):
        frame = np.asarray(frame, dtype=np.float32)
        if frame.shape != (FRAME,):
            raise ValueError("VAD requires 512-sample frames")
        combined = np.concatenate((self.context, frame))[None]
        output, self.h, self.c = self.session.run(None, {"input": combined, "h": self.h, "c": self.c})
        probability = float(np.asarray(output).reshape(-1)[0])
        self.probability = probability
        self.context = frame[-64:].copy()
        speech = probability >= 0.5
        if speech and not self.frames:
            self.frames = list(self.preroll)
            self.utterance_id = self.utterance_id or uid()
        if self.frames:
            self.frames.append(frame.copy())
            self.silent = 0 if speech else self.silent + FRAME
            maxed = len(self.frames) * FRAME / RATE >= self.settings.utterance_max_seconds
            ended = self.silent / RATE * 1000 >= self.settings.vad_end_ms
            if maxed or ended:
                result = (np.concatenate(self.frames), self.utterance_id, maxed and not ended)
                self.frames, self.silent = [], 0
                if ended:
                    self.utterance_id = None
                self.preroll.clear()
                return result
        else:
            self.preroll.append(frame.copy())
        return None


class Microphone:
    def __init__(self, perception, on_event, on_status, output_active=lambda: False, device=None):
        self.perception, self.on_event, self.on_status = perception, on_event, on_status
        self.output_active, self.device = output_active, device
        self.queue = queue.Queue(maxsize=perception.settings.audio_queue_capacity)
        self.segments = queue.Queue(maxsize=4)
        self.stop_event = threading.Event()
        self.stream = None
        self.thread = None
        self.asr_thread = None
        self.dropped = 0
        self.capture_id = uid()
        self.capture_rate, self.capture_channels = RATE, 1
        self.telemetry_lock = threading.Lock()
        self.telemetry = {
            "packets_received": 0,
            "input_seconds": 0.0,
            "rms": 0.0,
            "peak": 0.0,
            "vad_probability": 0.0,
            "speaking": False,
            "speech_intervals": 0,
            "recognized_intervals": 0,
            "asr_busy": False,
        }
        self.asr_started = None
        self.reported_at = 0.0
        self.last_packet = None

    def _report(self, force=False):
        if self.stop_event.is_set():
            return
        now = time.monotonic()
        if not force and now - self.reported_at < 1:
            return
        self.reported_at = now
        with self.telemetry_lock:
            self.telemetry["asr_seconds"] = now - self.asr_started if self.asr_started else 0.0
            self.telemetry["seconds_since_packet"] = now - self.last_packet if self.last_packet else None
            snapshot = dict(self.telemetry)
        self.on_status("audio_input", snapshot)

    def _callback(self, data, frames, timing, status):
        # No model calls, database I/O or GUI operations in PortAudio callback.
        try:
            self.queue.put_nowait((data.copy(), time.monotonic(), utc(), bool(status), self.output_active()))
        except queue.Full:
            self.dropped += 1

    def _worker(self):
        segmenter = self.segmenter
        started, wall, contaminated = None, None, False
        pending = np.empty(0, dtype=np.float32)
        while not self.stop_event.is_set():
            try:
                samples, mono, stamp, overflow, own_output = self.queue.get(timeout=0.2)
            except queue.Empty:
                self._report()
                continue
            if started is None:
                started, wall = mono, stamp
            contaminated |= own_output
            if overflow or self.dropped:
                self.on_status(
                    "microphone",
                    f"overflow: {self.dropped} dropped frames; interval unsuitable for clean experiments",
                )
                self.dropped = 0
            try:
                converted = mono16(samples, self.capture_rate)
                with self.telemetry_lock:
                    self.last_packet = time.monotonic()
                    self.telemetry["packets_received"] += 1
                    self.telemetry["input_seconds"] += len(samples) / self.capture_rate
                    self.telemetry["rms"] = float(np.sqrt(np.mean(converted * converted)))
                    self.telemetry["peak"] = float(np.max(np.abs(converted)))
                pending = np.concatenate((pending, converted))
                while len(pending) >= FRAME:
                    frame, pending = pending[:FRAME], pending[FRAME:]
                    ready = segmenter.feed(frame)
                    with self.telemetry_lock:
                        self.telemetry["vad_probability"] = segmenter.probability
                        self.telemetry["speaking"] = bool(segmenter.frames)
                    if ready:
                        audio, utterance_id, continues = ready
                        try:
                            self.segments.put_nowait(
                                (
                                    audio,
                                    {
                                        "utterance_id": utterance_id,
                                        "continues": continues,
                                        "tts_contamination_possible": contaminated,
                                        "echo_cancellation": "not_implemented",
                                        "original_sample_rate": self.capture_rate,
                                        "original_channels": self.capture_channels,
                                        "input_device_name": self.telemetry.get("device_name"),
                                        "input_device_id": self.device,
                                    },
                                    mono - len(audio) / RATE + FRAME / RATE,
                                    wall,
                                )
                            )
                            with self.telemetry_lock:
                                self.telemetry["speech_intervals"] += 1
                        except queue.Full:
                            self.on_status("asr", "overflow: speech interval dropped; bounded ASR queue full")
                        started, wall, contaminated = None, None, False
                    elif not segmenter.frames:
                        started, wall, contaminated = None, None, False
                self._report()
            except Exception as exc:
                self.on_status("vad", f"failed: {type(exc).__name__}: {exc}")

    def _asr_worker(self):
        parent = {}
        while not self.stop_event.is_set():
            try:
                audio, metadata, started, wall = self.segments.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                key = metadata["utterance_id"]
                metadata["parent_event_id"] = parent.get(key)
                with self.telemetry_lock:
                    self.asr_started = time.monotonic()
                    self.telemetry["asr_busy"] = True
                self.on_status("asr", "transcribing; first request also loads the local recognition model")
                self._report(force=True)
                event = self.perception.recognize(
                    audio, Source.MICROPHONE, metadata=metadata, mono_start=started, wall_start=wall
                )
                if metadata["continues"]:
                    parent[key] = event.event_id
                else:
                    parent.pop(key, None)
                if not self.stop_event.is_set():
                    self.on_event(event)
                    with self.telemetry_lock:
                        self.telemetry["recognized_intervals"] += 1
                    self.on_status("asr", "ready: microphone interval recognized")
            except Exception as exc:
                self.on_status("asr", f"failed: {type(exc).__name__}: {exc}")
            finally:
                with self.telemetry_lock:
                    self.asr_started = None
                    self.telemetry["asr_busy"] = False
                self._report(force=True)

    def start(self):
        import sounddevice as sd

        self.segmenter = Segmenter(self.perception.settings)
        self.capture_rate, self.capture_channels, name = stream_format(self.device, "input", RATE)
        with self.telemetry_lock:
            self.telemetry.update(
                {
                    "device_name": name,
                    "device_id": self.device,
                    "capture_sample_rate": self.capture_rate,
                    "capture_channels": self.capture_channels,
                }
            )
        self.stream = sd.InputStream(
            device=self.device,
            samplerate=self.capture_rate,
            channels=self.capture_channels,
            dtype="float32",
            blocksize=round(self.capture_rate * FRAME / RATE),
            callback=self._callback,
        )
        self.thread = threading.Thread(target=self._worker, name="esi-perception", daemon=True)
        self.thread.start()
        # Native inference cannot be interrupted safely; let it finish before interpreter shutdown.
        self.asr_thread = threading.Thread(target=self._asr_worker, name="esi-microphone-asr", daemon=False)
        self.asr_thread.start()
        self.stream.start()
        self.on_status("microphone", "running; headphones recommended; echo cancellation unavailable")
        self._report(force=True)

    def close(self):
        import sounddevice as sd

        self.stop_event.set()
        if self.stream:
            for operation in (self.stream.stop, self.stream.close):
                try:
                    operation()
                except sd.PortAudioError as exc:
                    self.on_status("microphone", f"failed: closing disconnected audio device: {exc}")
        if self.thread:
            self.thread.join(timeout=2)
        if self.asr_thread:
            self.asr_thread.join(timeout=2)
