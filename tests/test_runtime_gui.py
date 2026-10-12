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
