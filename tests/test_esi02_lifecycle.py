import threading
import time

from esi02.config import Settings
from esi02.runtime import Runtime
from esi02.session import Session


def test_fatal_owner_error_rejects_new_commands_and_closes_capture_once(tmp_path):
    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    runtime = Runtime(session)

    class Capture:
        stop_event = threading.Event()
        closes = 0

        def close(self):
            self.closes += 1
            self.stop_event.set()
            runtime._telemetry("microphone_status", ("stale", "microphone", "closing"))

    capture = Capture()
    runtime.microphone = capture

    def broken_world(*args, **kwargs):
        raise ValueError("fault injection: invalid world state")

    session.advance_world = broken_world
    runtime.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not runtime.halt.is_set():
            time.sleep(0.01)
        runtime.thread.join(timeout=3)
        assert not runtime.thread.is_alive()
        assert not runtime.send("text", "owner has stopped")
        assert capture.closes == 1 and capture.stop_event.is_set()
        assert runtime.microphone is None and not session.status["running"]
        assert "invalid world state" in session.status["error"]
        runtime.stop()
        assert capture.closes == 1
    finally:
        runtime.stop()
        session.close()
