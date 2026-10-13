import json
import threading
import time

import httpx
import pytest

from esi.core import Core
from esi.models import Observation, Source
from esi.models import Settings as OldSettings
from esi02.config import Settings
from esi02.dialogue import Cancellation, DialogueLLM
from esi02.memory import Candidate
from esi02.memory_llm import MemoryLLM
from esi02.migration import migrate_esi01, validate_save
from esi02.runtime import Runtime
from esi02.session import Session


def until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class SilentMemory:
    def extract(self, turn, existing, cancellation):
        cancellation.check()
        return {"items": [], "rejected": []}


class Speech:
    def synthesize(self, text, action):
        return {"path": action + ".wav", "duration": 0.1, "played": False}


class Replies:
    def __init__(self):
        self.contexts = []

    def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
        cancellation.check()
        self.contexts.append(context)
        on_delta("わかりました。")
        on_sentence("わかりました。")
        return {"text": "わかりました。", "finish_reason": "stop"}


def report(turn, key="name", value="太郎", kind="fact", label="名前", **kwargs):
    return Candidate(kind=kind, key=key, label=label, value=value, quote=turn["input"], **kwargs)


def completed(session, text, **kwargs):
    turn = session.create_turn(text, **kwargs)
    session.update_turn(turn["id"], stage="complete")
    session.memory.finish(turn["id"])
    return turn


def test_memory_outlives_short_history_and_restart_and_has_exact_evidence(tmp_path):
    path = tmp_path / "memory.sqlite3"
    session = Session(path, Settings(retained_turns=16))
    first = completed(session, "私の名前は太郎です。私は紅茶が好きです。")
    for i in range(20):
        completed(session, f"別の会話です。{i}")
    query = completed(session, "私の名前と好きな飲み物を覚えていますか？")
    assert first["id"] not in session.turns
    assert all("太郎" not in m["content"] for m in session.history(query["id"]))
    session.close()
    restored = Session(path)
    try:
        memory = restored.memory.context("名前と紅茶")
        assert any(m["value"] == "太郎" and m["quote"] == "私の名前は太郎です。" for m in memory)
        assert any(m["label"] == "紅茶" and m["value"] == "好き" for m in memory)
        assert restored.memory.search("太郎")[0]["source_turn_id"] == first["id"]
    finally:
        restored.close()


def test_correction_supersedes_old_value_and_late_old_extraction_cannot_reverse_it(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    try:
        old = completed(session, "私の名前は太郎です。")
        new = completed(session, "私の名前は太郎ではなく次郎です。")
        assert session.memory.entries()[0]["value"] == "次郎"
        session.memory.apply(old, [report(old)])
        assert session.memory.entries()[0]["value"] == "次郎"
        assert not any("太郎です" in m["content"] for m in session.history(new["id"]))
        item = session.memory.entries()[0]
        session.memory_operation("edit", {"id": item["id"], "value": "三郎"})
        session.memory.apply(new, [report(new, value="次郎")])
        assert session.memory.entries()[0]["value"] == "三郎"
        completed(session, "私の名前は四郎です。")
        assert session.memory.entries()[0]["value"] == "四郎"
    finally:
        session.close()


def test_inferred_is_distinct_not_used_as_unrelated_fact_and_cannot_replace_direct_report(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    try:
        direct = completed(session, "私の名前は太郎です。")
        guess = completed(session, "次郎かもしれません。")
        session.memory.apply(guess, [report(guess, value="次郎", basis="inferred")])
        assert session.memory.entries()[0]["value"] == "太郎"
        session.memory.apply(
            guess, [report(guess, key="fact:候補", label="候補", value="次郎", basis="inferred")]
        )
        assert not any(m["value"] == "次郎" for m in session.memory.context("天気"))
        assert "未確認" in next(m["trust"] for m in session.memory.context("次郎") if m["value"] == "次郎")
        assert direct["id"] in session.turns
    finally:
        session.close()


def test_unquoted_facts_are_rejected_before_any_batch_write(tmp_path):
    session = Session(tmp_path / "s.sqlite3")
    try:
        turn = completed(session, "名前は太郎。")
        with pytest.raises(ValueError, match="発言にない"):
            session.memory.apply(turn, [report(turn), report(turn, key="occupation", value="医師")])
        assert not session.memory.entries()
    finally:
        session.close()


def test_delete_blocks_deferred_source_and_clears_prompt_history_without_erasing_original_conversation(
    tmp_path,
):
    path = tmp_path / "s.sqlite3"
    session = Session(path)
    turn = completed(session, "私の名前は太郎です。")
    memory = session.memory.entries()[0]
    session.memory.queue(turn)
    revision = session.memory.revision
    session.memory_operation("delete", {"id": memory["id"]})
    assert not session.memory.apply(turn, [report(turn)], expected_revision=revision)
    assert not session.memory.apply(turn, [report(turn)])
    assert not session.memory.pending()
    session.close()
    restored = Session(path)
    try:
        query = completed(restored, "何を覚えている？")
        assert not restored.memory.entries()
        assert not restored.history(query["id"])
        assert restored.turns[turn["id"]]["input"] == turn["input"]
    finally:
        restored.close()


def test_topic_resolution_and_manual_correction_are_persistent(tmp_path):
    path = tmp_path / "s.sqlite3"
    session = Session(path)
    turn = completed(session, "仕事探しで悩んでいます。")
    session.memory.apply(
        turn, [report(turn, kind="topic", key="topic:仕事探し", label="仕事探し", value="仕事探し")]
    )
    topic = session.memory.entries()[0]
    solved = completed(session, "仕事探しが解決しました。")
    session.memory.apply(
        solved,
        [
            report(
                solved,
                kind="topic",
                key="topic:仕事探し",
                label="仕事探し",
                value="解決",
                resolves_id=topic["id"],
            )
        ],
    )
    session.close()
    restored = Session(path)
    try:
        assert not restored.memory.entries()
        archived = restored.memory.entries(include_old=True)[0]
        assert archived["status"] == "resolved" and archived["resolution_quote"] == solved["input"]
    finally:
        restored.close()


def test_memory_survives_event_compaction_and_uncheckpointed_crash(tmp_path):
    path = tmp_path / "s.sqlite3"
    session = Session(path, Settings(max_records=1000))
    turn = completed(session, "私の名前は太郎です。")
    session.store.max_records = 1
    session.checkpoint()
    assert not session.store.records()
    session.memory_operation("edit", {"id": session.memory.entries()[0]["id"], "value": "次郎"})
    session.store.close()  # no session checkpoint after correction
    restored = Session(path)
    try:
        assert restored.memory.entries()[0]["value"] == "次郎"
        assert restored.memory.history_floor == turn["epoch"]
    finally:
        restored.close()


def test_idle_extraction_yields_to_new_conversation_and_never_invalidates_environment(tmp_path):
    analyzing, cancelled = threading.Event(), threading.Event()

    class Analysis:
        calls = 0

        def extract(self, turn, existing, cancellation):
            self.calls += 1
            if self.calls == 1:
                analyzing.set()
                assert cancellation.event.wait(2)
                cancelled.set()
                cancellation.check()
            return {"items": [], "rejected": []}

    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    replies = Replies()
    runtime = Runtime(session, llm=replies, tts=Speech(), memory_llm=Analysis())
    runtime.start()
    try:
        runtime.send("text", "私の名前は太郎です。")
        assert analyzing.wait(2)
        for _ in range(4):
            runtime.send("environment", {"temperature": 24})
        assert not cancelled.wait(0.05)
        runtime.send("text", "名前を覚えていますか？")
        assert cancelled.wait(1)
        until(lambda: len(session.turns) == 2 and session.view()["turns"][-1]["stage"] == "complete")
        assert session.epoch == 2
        assert any(m["value"] == "太郎" for m in replies.contexts[-1]["memories"])
        until(lambda: not session.memory.pending())
    finally:
        runtime.stop()
        session.close()


def test_memory_failure_does_not_break_reply_retries_are_bounded_and_visible(tmp_path):
    class Broken:
        calls = 0

        def extract(self, *args):
            self.calls += 1
            raise ValueError("unquoted suggestion")

    session = Session(tmp_path / "s.sqlite3", Settings(playback=False, tick_seconds=0.01))
    broken = Broken()
    runtime = Runtime(session, llm=Replies(), tts=Speech(), memory_llm=broken)
    runtime.start()
    try:
        runtime.send("text", "こんにちは")
        until(lambda: session.view()["memory"]["failed"] == 1)
        assert broken.calls == 2
        assert session.view()["turns"][-1]["stage"] == "complete"
        assert "unquoted" in session.status["memory_error"]
        assert session.status["phase"] == "waiting"
    finally:
        runtime.stop()
        session.close()


def test_extractor_excludes_fabricated_value_and_mismatched_fact_but_accepts_exact_name():
    turn = {"id": "test", "input": "私の名前は次郎です。", "epoch": 1}
    items = [
        report(turn, value="次郎").model_dump(),
        report(turn, key="occupation", label="職業", value="医師").model_dump(),
        report(turn, key="nickname", label="呼び名", value="次郎").model_dump(),
    ]
    requests = []

    def response(request):
        requests.append(request)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2]})
        event = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "delta": {"content": json.dumps({"items": items}, ensure_ascii=False)},
                }
            ]
        }
        return httpx.Response(
            200, text="data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
        )

    result = MemoryLLM(
        Settings(), lambda **kw: httpx.Client(**kw, transport=httpx.MockTransport(response))
    ).extract(turn, [], Cancellation())
    assert len(result["items"]) == 1 and result["items"][0]["value"] == "次郎"
    assert len(result["rejected"]) == 2
    assert json.loads(requests[-1].content)["response_format"]["type"] == "json_schema"


def test_migration_uses_wal_and_uncheckpointed_commands_keeps_original_and_archives_numerical_learning(
    tmp_path,
):
    source, dest = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    old = Core(source, OldSettings(llm_mode="off"))
    old.observe(Observation(text="私の名前は太郎です。", source=Source.MICROPHONE))
    old.observe(Observation(text="私の名前は試験用です。", source=Source.SIMULATED))
    old.observe(Observation(text="私は医師です。", source=Source.SELF_OUTPUT))
    old.tick(7)  # original snapshot still precedes these commands, committed records are in WAL
    before, wal = source.read_bytes(), source.with_name(source.name + "-wal").read_bytes()
    result = migrate_esi01(source, dest, Settings(playback=False))
    assert source.read_bytes() == before
    assert source.with_name(source.name + "-wal").read_bytes() == wal
    session = Session(dest)
    try:
        assert session.agent_id == old.agent_id and session.elapsed == 7
        archive = session.store.meta("esi01_import")
        assert archive["state"]["predictor"] == old.predictor.dump()
        assert result["archive"].is_file()
        assert len(session.turns) == 2
        assert all(t["stage"] == "complete" and not t["reply"] for t in session.turns.values())
        assert len(session.memory.entries()) == 1 and session.memory.entries()[0]["value"] == "太郎"
        query = completed(session, "私の名前は？")
        assert not any("試験用" in m["content"] for m in session.history(query["id"]))
    finally:
        session.close()
        old.close()
    assert validate_save(result["archive"])["release"] == "ESI-01"


def test_migration_never_overwrites_existing_destination_or_original(tmp_path):
    path = tmp_path / "old.sqlite3"
    core = Core(path, OldSettings(llm_mode="off"))
    core.close()
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        migrate_esi01(path, path)
    dest = tmp_path / "occupied.sqlite3"
    dest.write_bytes(b"existing user data")
    with pytest.raises(FileExistsError):
        migrate_esi01(path, dest)
    assert dest.read_bytes() == b"existing user data" and path.read_bytes() == before


def test_model_context_shrinks_recall_instead_of_rejecting_current_input():
    count = 0

    def response(request):
        nonlocal count
        if request.url.path == "/apply-template":
            count += 1
            return httpx.Response(200, json={"prompt": "prompt"})
        return httpx.Response(200, json={"tokens": list(range(2050 if count < 3 else 50))})

    llm = DialogueLLM(Settings(context_tokens=2048))
    messages = llm.messages("こんにちは", [], {"memories": [{"label": "one"}, {"label": "two"}]})
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(response)) as client:
        result = llm._budget(client, messages, Cancellation())
    assert result[-1]["content"] == "こんにちは" and count == 3
    assert '"memories": []' in result[0]["content"]
