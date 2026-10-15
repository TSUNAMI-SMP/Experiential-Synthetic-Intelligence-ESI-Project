import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from esi02 import playback


def test_meter_follows_dac_time_silence_cancellation_and_stale_generation():
    meter = playback.PlaybackMeter()
    stop = threading.Event()
    token = meter.begin(stop)
    meter.publish(token, 10, 0.2, np.full((3200, 2), 0.1))
    meter.publish(token, 10.2, 0.2, np.zeros((3200, 2)))
    assert meter.level(9.9) == 0
    assert meter.level(10.1) > 0.6
    assert meter.level(10.3) == 0
    assert meter.level(10.5) == 0
    new = meter.begin(threading.Event())
    meter.publish(new, 12, 1, np.ones((1, 1)))
    meter.clear(token)  # Old worker cleanup must not clear a new playback.
    meter.publish(token, 12, 1, np.zeros((1, 1)))
    assert meter.level(12.1) == 1
    meter.clear()
    token = meter.begin(stop)
    meter.publish(token, 13, 1, np.ones((1, 1)))
    stop.set()
    assert meter.level(13.1) == 0


def install_stream(monkeypatch, *, status=None, dac_delay=0.02, on_block=lambda output: None):
    streams = []

    class Stream:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.aborted = threading.Event()
            self.samples = []
            self.error = None
            streams.append(self)

        def run(self):
            try:
                while not self.aborted.is_set():
                    block = np.empty((512, self.options["channels"]), dtype=np.float32)
                    timing = SimpleNamespace(currentTime=1, outputBufferDacTime=1 + dac_delay)
                    try:
                        self.options["callback"](block, 512, timing, status)
                    except sd.CallbackStop:
                        self.samples.append(block.copy())
                        on_block(block)
                        self.aborted.wait(dac_delay + 512 / self.options["samplerate"])
                        break
                    except sd.CallbackAbort:
                        break
                    self.samples.append(block.copy())
                    on_block(block)
                    self.aborted.wait(512 / self.options["samplerate"])
            except Exception as exc:
                self.error = exc
            finally:
                self.options["finished_callback"]()

        def __enter__(self):
            self.thread = threading.Thread(target=self.run)
            self.thread.start()
            return self

        def abort(self):
            self.aborted.set()

        def __exit__(self, *args):
            self.thread.join(2)
            assert not self.thread.is_alive()
            if self.error:
                raise self.error

    monkeypatch.setattr(sd, "OutputStream", Stream)
    monkeypatch.setattr(playback, "stream_format", lambda *args: (16000, 2, "test-output"))
    return streams


def test_output_resampling_amplitude_silence_and_final_partial_buffer(tmp_path, monkeypatch):
    path = tmp_path / "audio.wav"
    sf.write(path, np.r_[np.full(1600, 0.2), np.zeros(1600)], 8000)
    meter = playback.PlaybackMeter()
    observed = []
    streams = install_stream(monkeypatch, on_block=lambda output: observed.append(meter.level()))
    routing = {}
    assert playback.play_wave(path, routing=routing, meter=meter)
    sent = np.concatenate(streams[0].samples)
    assert sent.shape == (6656, 2) and np.allclose(sent[:, 0], sent[:, 1])
    assert np.max(sent[:3000]) > 0.19 and np.max(np.abs(sent[4000:])) < 0.001
    assert max(observed) > 0.9 and observed[-1] == 0 and meter.level() == 0
    assert routing["playback_sample_rate"] == 16000 and routing["playback_device_name"] == "test-output"


def test_cancel_during_output_closes_mouth_and_does_not_report_success(tmp_path, monkeypatch):
    path = tmp_path / "audio.wav"
    sf.write(path, np.full(16000, 0.15), 16000)
    meter, stop = playback.PlaybackMeter(), threading.Event()
    observed = []

    def cancel(block):
        stop.set()
        observed.append(meter.level())

    install_stream(monkeypatch, on_block=cancel)
    assert not playback.play_wave(path, stop, meter=meter)
    assert observed == [0] and meter.level() == 0


def test_output_underflow_keeps_speaking_and_reports_discontinuity(tmp_path, monkeypatch):
    path = tmp_path / "audio.wav"
    sf.write(path, np.ones(1600), 16000)
    meter = playback.PlaybackMeter()
    flags = sd.CallbackFlags()
    flags.output_underflow = True
    install_stream(monkeypatch, status=flags)
    route = {}
    assert playback.play_wave(path, meter=meter, routing=route)
    assert route["playback_underflows"] > 0
    assert meter.level() == 0


def test_already_cancelled_never_opens_output(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(sd, "OutputStream", lambda **kwargs: pytest.fail("must not open"))
    assert not playback.play_wave("does-not-exist.wav", stop)


def test_output_device_open_failure_clears_preexisting_and_new_meter_state(tmp_path, monkeypatch):
    path = tmp_path / "audio.wav"
    sf.write(path, np.full(1600, 0.2), 16000)
    meter = playback.PlaybackMeter()
    token = meter.begin(threading.Event())
    meter.publish(token, time.monotonic(), 5, np.ones((512, 1)))
    monkeypatch.setattr(playback, "stream_format", lambda *args: (16000, 1, "test-output"))

    def fail(**kwargs):
        raise OSError("test: device disconnected")

    monkeypatch.setattr(sd, "OutputStream", fail)
    with pytest.raises(OSError, match="device disconnected"):
        playback.play_wave(path, meter=meter)
    assert meter.level() == 0
