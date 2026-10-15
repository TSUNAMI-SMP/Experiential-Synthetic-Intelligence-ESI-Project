"""Cancellable output with amplitude measured at the scheduled DAC time."""

import math
import threading
import time
from collections import deque

import numpy as np
import soundfile as sf

from esi.devices import stream_format


class PlaybackMeter:
    """Transient renderer telemetry; never persisted as experience or reward."""

    def __init__(self):
        self.lock = threading.Lock()
        self.generation = 0
        self.stop = None
        self.blocks = deque(maxlen=256)

    def begin(self, stop):
        with self.lock:
            self.generation += 1
            self.stop = stop
            self.blocks.clear()
            return self.generation

    def publish(self, generation, start, duration, samples):
        rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) if samples.size else 0.0
        opening = min(1.0, max(0.0, (rms - 0.003) * 7)) if math.isfinite(rms) else 0.0
        with self.lock:
            if generation == self.generation and not (self.stop and self.stop.is_set()):
                self.blocks.append((start, start + duration, opening))

    def level(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.stop and self.stop.is_set():
                return 0.0
            while self.blocks and self.blocks[0][1] <= now:
                self.blocks.popleft()
            if self.blocks and self.blocks[0][0] <= now < self.blocks[0][1]:
                return self.blocks[0][2]
            return 0.0

    def clear(self, generation=None):
        with self.lock:
            if generation is None or generation == self.generation:
                self.generation += 1
                self.blocks.clear()
                self.stop = None


def play_wave(path, stop_event=None, device=None, routing=None, meter=None):
    """Retry native formats only if the failed stream submitted no audio."""
    import sounddevice as sd

    routing = routing if routing is not None else {}
    attempts = []
    routing["playback_attempts"] = attempts
    if device is None:
        default = sd.default.device[1]
        device = default if isinstance(default, int) and default >= 0 else None
    profile, candidates = None, None
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        routing["playback_frames_submitted"] = 0
        attempt = {"sample_rate": None, "channels": None}
        attempts.append(attempt)
        try:
            result = _play_wave_once(path, stop_event, device, routing, meter, profile)
            attempt.update(sample_rate=routing.get("playback_sample_rate"),
                           channels=routing.get("playback_channels"), completed=result)
            return result
        except sd.PortAudioError as exc:
            attempt.update(sample_rate=routing.get("playback_sample_rate"),
                           channels=routing.get("playback_channels"), error=str(exc)[:500], completed=False)
            if routing["playback_frames_submitted"]:
                raise  # Never repeat audio that may already have been heard.
            if candidates is None:
                details = sd.query_devices(device, "output")
                native, channels = int(details["default_samplerate"]), int(details["max_output_channels"])
                failed = (routing.get("playback_sample_rate"), routing.get("playback_channels"))
                candidates = [(native, count, details["name"]) for count in dict.fromkeys((min(2, channels), 1))
                              if count > 0 and (native, count) != failed]
            while candidates:
                profile = candidates.pop(0)
                try:
                    sd.check_output_settings(device=device, samplerate=profile[0], channels=profile[1], dtype="float32")
                    break
                except (sd.PortAudioError, ValueError):
                    continue
            else:
                raise


def _play_wave_once(path, stop_event=None, device=None, routing=None, meter=None, profile=None):
    import sounddevice as sd
    from scipy.signal import resample_poly

    stop_event = stop_event if stop_event is not None else threading.Event()
    meter = meter if meter is not None else PlaybackMeter()
    if stop_event.is_set():
        return False
    audio, source_rate = sf.read(path, dtype="float32", always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("再生する音声が空か、不正な値を含んでいます。")
    rate, channels, name = profile or stream_format(device, "output", source_rate, audio.shape[1])
    if rate != source_rate:
        divisor = math.gcd(rate, source_rate)
        audio = resample_poly(audio, rate // divisor, source_rate // divisor, axis=0).astype(np.float32)
    if audio.shape[1] != channels:
        audio = np.repeat(audio.mean(axis=1, keepdims=True), channels, axis=1)
    if routing is not None:
        routing.update(
            playback_device_id=device,
            playback_device_name=name,
            playback_sample_rate=rate,
            playback_channels=channels,
        )
    generation = meter.begin(stop_event)
    finished = threading.Event()
    position = 0
    failure = []
    underflows = 0

    def callback(output, frames, timing, status):
        nonlocal position, underflows
        output.fill(0)
        if stop_event.is_set():
            raise sd.CallbackAbort
        try:
            # PortAudio status flags describe a recovered buffer discontinuity,
            # not a disconnected device. Aborting here cuts off the first word
            # (and clears its lip telemetry) on busy WASAPI/ASIO devices.
            if status and getattr(status, "output_underflow", False):
                underflows += 1
            count = min(frames, len(audio) - position)
            output[:count] = audio[position : position + count]
            position += count
            if routing is not None:
                routing["playback_frames_submitted"] = position
            start = time.monotonic() + max(0, timing.outputBufferDacTime - timing.currentTime)
            meter.publish(generation, start, count / rate, output[:count])
        except Exception as exc:
            failure.append(exc)
            raise sd.CallbackAbort from exc
        if position >= len(audio):
            raise sd.CallbackStop  # Drain the final queued block before finished_callback.

    try:
        with sd.OutputStream(
            device=device,
            samplerate=rate,
            channels=channels,
            dtype="float32",
            blocksize=0,
            latency="high",
            callback=callback,
            finished_callback=finished.set,
        ) as output:
            deadline = time.monotonic() + len(audio) / rate + 10
            while not finished.wait(0.02):
                if stop_event.is_set():
                    output.abort()
                    return False
                if time.monotonic() >= deadline:
                    output.abort()
                    raise RuntimeError("音声出力の完了が時間切れになりました。")
            if failure:
                raise failure[0]
            if not stop_event.is_set() and position != len(audio):
                raise RuntimeError("音声の途中で出力機器が停止しました。出力スピーカーを選び直して再試行してください。")
            return not stop_event.is_set() and position == len(audio)
    finally:
        if routing is not None:
            routing["playback_underflows"] = underflows
        meter.clear(generation)
