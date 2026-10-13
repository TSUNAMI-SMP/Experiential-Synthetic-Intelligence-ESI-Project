import os

import pytest

from esi02.config import Settings
from esi02.dialogue import Cancellation, DialogueLLM
from esi02.memory_llm import MemoryLLM
from esi02.session import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("ESI_RUN_INTEGRATION") != "1", reason="actual local Qwen required"),
]


def test_actual_model_cited_memory_and_recall_after_restart_outside_recent_history(tmp_path):
    settings = Settings(playback=False, retained_turns=16, reply_tokens=96)
    path = tmp_path / "remember.sqlite3"
    session = Session(path, settings)
    initial = session.create_turn("私の名前は太郎です。私は紅茶が好きです。")
    session.update_turn(initial["id"], stage="complete")
    corrected = session.create_turn("私の名前は太郎ではなく次郎です。")
    session.update_turn(corrected["id"], stage="complete")
    try:
        result = MemoryLLM(settings).extract(corrected, session.memory.entries(), Cancellation())
        assert any(m["key"] == "name" and m["value"] == "次郎" for m in result["items"]), result
        assert all(m["quote"] in corrected["input"] and m["value"] in m["quote"] for m in result["items"])
        session.memory.apply(corrected, result["items"], "Qwen3-4B")
        for i in range(18):
            turn = session.create_turn(f"別の会話です。{i}")
            session.update_turn(turn["id"], stage="complete")
            session.memory.finish(turn["id"])
    finally:
        session.close()
    reopened = Session(path)
    try:
        query = reopened.create_turn("私の名前と、好きな飲み物を覚えていますか？")
        history = reopened.history(query["id"])
        assert all("次郎" not in m["content"] and "紅茶" not in m["content"] for m in history)
        result = DialogueLLM(settings).stream(
            query["input"],
            history,
            Cancellation(),
            lambda text: None,
            lambda text: None,
            context={"memories": reopened.memory.context(query["input"])},
        )
        assert "次郎" in result["text"] and "紅茶" in result["text"], result["text"]
        assert "太郎" not in result["text"]
        assert not result["output_is_external_evidence"]
        print("Actual remembered reply:", result["text"])
    finally:
        reopened.close()
