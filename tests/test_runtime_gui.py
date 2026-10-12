import time
from concurrent.futures import Future

from esi.core import Core
from esi.models import Interpretation, Observation, Settings
from esi.runtime import Runtime

_application = None


def test_idle_runtime_updates_and_saves_without_llm_calls(tmp_path):
    core = Core(tmp_path / "runtime.sqlite3", Settings(llm_mode="off", tick_seconds=0.02))
    runtime = Runtime(core)
    try:
        runtime.start()
        runtime.send("observe", Observation(text="ordinary stimulus"))
        deadline = time.monotonic() + 2
        while core.elapsed < 0.15 and time.monotonic() < deadline:
            time.sleep(0.02)
        runtime.stop()
        assert core.elapsed >= 0.15
        assert core.store.records("state")
        assert not runtime.jobs
        assert core.status["llm"] == "disabled"
        assert core.store.load_snapshot()["state"]["elapsed"] == core.elapsed
    finally:
        if runtime.thread and runtime.thread.is_alive():
            runtime.stop()
        core.close()


def test_stale_worker_result_is_recorded_and_not_used(tmp_path):
    core = Core(tmp_path / "stale.sqlite3", Settings(llm_mode="assist"))
    runtime = Runtime(core)
    try:
        event = Observation(text="earlier")
        core.observe(event)
        future = Future()
        future.set_result(Interpretation(referenced_event_ids=[event.event_id], category="unknown"))
        runtime.jobs[future] = {
            "kind": "interpret",
            "event_id": event.event_id,
            "version": core.context_version,
        }
        core.observe(Observation(text="later correction"))
        runtime._complete_jobs()
        assert event.event_id not in core.interpretations
        assert core.store.records("stale_result")
    finally:
        runtime.stop()
        core.close()


def test_failed_inference_does_not_stop_clock_or_fabricate_speech(tmp_path):
    class BrokenLLM:
        def interpret(self, context):
            raise ConnectionError("intentional fault injection")

    core = Core(tmp_path / "fault.sqlite3", Settings(llm_mode="assist", tick_seconds=0.02, exploration=0))
    runtime = Runtime(core)
    runtime.llm = BrokenLLM()
    try:
        runtime.start()
        runtime.send("observe", Observation(text="ordinary observation"))
        deadline = time.monotonic() + 2
        while not core.status["llm"].startswith("failed") and time.monotonic() < deadline:
            time.sleep(0.02)
        elapsed = core.elapsed
        time.sleep(0.08)
        assert core.elapsed > elapsed
        assert core.status["llm"].startswith("failed")
        assert core.store.records("numeric_fallback")
        assert not core.store.records("speech_plan")
    finally:
        runtime.stop()
        core.close()


def test_observation_window_renders_and_controls_learning(tmp_path, monkeypatch):
    global _application
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi.gui import Window

    app = _application = QApplication.instance() or QApplication([])
    core = Core(tmp_path / "gui.sqlite3", Settings(llm_mode="off"))
    window = Window(core)
    window.show()
    app.processEvents()
    assert "ESI-01" in window.windowTitle()
    window.learning_box.setChecked(False)
    assert not core.learning_enabled
    core.observe(Observation(text="GUI観測？"))
    core.choose(next(reversed(core.events)))
    window.refresh()
    app.processEvents()
    assert window.workspace_table.rowCount() == 1
    assert window.candidate_table.rowCount() >= 4
    assert window.grab().save(str(tmp_path / "esi-01-observer.png"))
    window.close()
    window.deleteLater()
    from PySide6.QtCore import QEvent

    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_gui_routes_devices_starts_microphone_and_preserves_experiment_settings(tmp_path, monkeypatch):
    global _application
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi import gui

    chosen = []
    available = [
        {"index": 7, "name": "選んだマイク", "hostapi": "WASAPI", "inputs": 1, "outputs": 0},
        {"index": 9, "name": "選んだスピーカー", "hostapi": "WASAPI", "inputs": 0, "outputs": 2},
    ]

    class FakeRuntime:
        def __init__(self, core, input_device, output_device):
            chosen.append((input_device, output_device))
            self.microphone = None

        def start(self, microphone=False):
            chosen.append(microphone)
            self.microphone = object() if microphone else None

        def stop(self):
            pass

    monkeypatch.setattr(gui, "audio_devices", lambda: available)
    monkeypatch.setattr(gui, "Runtime", FakeRuntime)
    app = _application = QApplication.instance() or QApplication([])
    core = Core(tmp_path / "gui-routing.sqlite3", Settings(llm_mode="off"))
    fingerprint = core.settings.fingerprint
    window = gui.Window(core)
    window.show()
    window.resize(900, 650)
    app.processEvents()
    assert window.height() <= 650 and window.width() <= 900
    window.input_devices.setCurrentIndex(1)
    window.output_devices.setCurrentIndex(1)
    window.toggle()
    try:
        assert chosen == [(7, 9), True]
        assert "処理中" in window.statusBar().currentMessage()
        assert not window.input_devices.isEnabled()
        assert window.microphone_button.text() == "マイクを止める"
        core.status.update(
            {
                "microphone": "running",
                "audio_input": {
                    "packets_received": 5,
                    "rms": 0.1,
                    "peak": 0.2,
                    "device_name": "選んだマイク",
                },
                "jobs": [{"kind": "interpret", "seconds": 6}],
            }
        )
        window.refresh()
        assert window.input_meter.value() > 0
        assert "検討" in window.phase_title.text()
        assert "6秒" in window.phase_detail.text()
        assert core.settings.fingerprint == fingerprint
        assert core.store.meta("settings") == core.settings.model_dump()
        window.toggle()
        assert window.input_devices.isEnabled()
        assert "停止中" in window.statusBar().currentMessage()
        assert (tmp_path / "audio-devices.json").is_file()
    finally:
        window.close()
        window.deleteLater()
        from PySide6.QtCore import QEvent

        app.sendPostedEvents(None, QEvent.DeferredDelete)
