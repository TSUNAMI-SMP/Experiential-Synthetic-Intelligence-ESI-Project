import threading
import time

from esi03.config import Settings
from esi03.runtime import Runtime
from esi03.session import Session
from esi03.tools import Policy


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("Owner did not reach the expected state")


def test_autonomous_folder_goal_reaches_evidence_and_belief(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "source.md").write_text("星砂村は月の国です。", encoding="utf-8")
    session = Session(tmp_path / "save.db", Settings(playback=False))
    session.research.configure(Policy(read_roots=[str(folder)]).model_dump())
    session.character_operation("preferences", {"exploration": True, "proactive": False})
    session.experience.state.interests[0].attempts = 3
    session.character_operation("interest", {"topic": "星砂村", "question": "星砂村とは何ですか？"})
    runtime = Runtime(session)
    try:
        runtime.start(microphone=False)
        wait_for(lambda: bool(session.knowledge.entries()))
        assert session.knowledge.entries()[0]["value"] == "月の国"
        assert session.research.entries()[0].stage == "complete"
        assert session.experience.state.goals[-1].stage == "complete"
        assert session.experience.state.goals[-1].evidence_ids
        assert session.experience.state.habits["research"].trials == 1
    finally:
        runtime.stop()
        session.close()


def test_stop_tools_rejects_late_observation_and_never_learns(tmp_path, monkeypatch):
    from esi03 import runtime as module

    started, release = threading.Event(), threading.Event()

    class SlowRunner:
        def __init__(self, policy, cancellation):
            pass

        def execute(self, *args):
            started.set()
            release.wait(2)
            return {"evidence": []}

    monkeypatch.setattr(module, "ToolRunner", SlowRunner)
    session = Session(tmp_path / "save.db", Settings(playback=False))
    session.research.configure(Policy(web_enabled=True).model_dump())
    session.character_operation("preferences", {"proactive": False})
    runtime = Runtime(session)
    try:
        runtime.start(microphone=False)
        assert runtime.send("tool", ("web_search", {"query": "学習"}))
        assert started.wait(2)
        assert runtime.send("stop_tools")
        wait_for(lambda: session.research.entries()[0].stage == "cancelled")
        release.set()
        wait_for(lambda: runtime.tool_task is None)
        assert not session.knowledge.entries()
        assert session.experience.state.habits["research"].trials == 0
    finally:
        release.set()
        runtime.stop()
        session.close()
