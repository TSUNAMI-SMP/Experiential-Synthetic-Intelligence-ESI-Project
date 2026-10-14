import json
import time

import httpx

from esi.models import Observation, Source
from esi02.config import Settings
from esi02.dialogue import Cancellation, DialogueLLM
from esi02.grounding import Decision, preflight, render
from esi02.knowledge import teaching_candidates
from esi02.runtime import Runtime
from esi02.session import Session


def test_persons_feeling_is_not_reported_as_the_agents_virtual_body():
    context = {"virtual_world": {"body": {"energy": .5, "fatigue": .25}}}
    statement = "私は疲れています。"
    reply = render(preflight(statement, context), statement, context, [])
    assert "仮想" not in reply and "エネルギー" not in reply
    question = "私は疲れていますか？"
    assert preflight(question, context).intent == "unknown"
    # Even a wrong model classification cannot turn the person's state into
    # the agent's own numeric observation.
    forced = Decision(intent="state", refs=[], phrase="none")
    assert "エネルギー" not in render(forced, question, context, [])
    for third_person in ["妻は元気ですか？", "あなたの犬は元気ですか？"]:
        assert preflight(third_person, context).intent == "unknown"
        assert "エネルギー" not in render(forced, third_person, context, [])
    for own_question in ["あなたは元気ですか？", "あなたの疲れは？", "調子はどうですか？"]:
        answer = render(preflight(own_question, context), own_question, context, [])
        assert "仮想" in answer and "50%" in answer and "25%" in answer
    temperature = "部屋の温度は？"
    assert "エネルギー" not in render(preflight(temperature, context), temperature, context, [])


def test_mistaken_teaching_correction_persistence_and_stale_results(tmp_path):
    path = tmp_path / "learn.sqlite3"
    session = Session(path)
    first = session.create_turn("ルーメリアは月にある街です。")
    assert session.knowledge.entries()[0]["value"] == "月にある街"
    revision = session.knowledge.revision
    session.create_turn("ルーメリアは月ではなく海にある街です。")
    assert session.knowledge.entries()[0]["value"] == "海にある街"
    assert not session.knowledge.apply(first, teaching_candidates(first["input"]), expected_revision=revision)
    assert len(session.knowledge.entries(include_old=True)) == 2
    assert not session.memory.entries()
    session.close()
    reopened = Session(path)
    try:
        assert reopened.knowledge.entries()[0]["value"] == "海にある街"
        assert reopened.knowledge.entries(include_old=True)[1]["status"] == "superseded"
    finally:
        reopened.close()


def test_unknown_and_forged_or_unrelated_citations_cannot_create_facts(tmp_path):
    session = Session(tmp_path / "learn.sqlite3")
    try:
        session.create_turn("ルーメリアは月にある街です。")
        context = session.dialogue_context("ニューヨークはどんな街？")
        entry = session.knowledge.entries()[0]
        context["knowledge"] = [entry]
        for ids in (["invented"], [entry["id"]]):
            answer = render(Decision(intent="recall", refs=ids, phrase="none"), "ニューヨークはどんな街？", context, [])
            assert "まだ知りません" in answer and "月にある" not in answer
        context = session.dialogue_context("ルーメリアはどんな街？")
        answer = render(Decision(intent="recall", refs=[entry["id"]], phrase="none"), "ルーメリアはどんな街？", context, [])
        assert "月にある街" in answer and "お手伝い" not in answer
    finally:
        session.close()


def test_schema_decision_is_checked_before_gui_or_speech_and_no_boilerplate(tmp_path):
    session = Session(tmp_path / "learn.sqlite3")
    sent, deltas, requests = [], [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "test"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2]})
        raw = json.dumps({"intent": "unknown", "refs": [], "phrase": "none"})
        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": raw[:10]}}]}),
                 "data: " + json.dumps({"choices": [{"delta": {"content": raw[10:]}, "finish_reason": "stop"}]})]
        return httpx.Response(200, text="\n\n".join(lines))

    try:
        context = session.dialogue_context("なんだか気になる")
        llm = DialogueLLM(Settings(), lambda **kw: httpx.Client(**kw, transport=httpx.MockTransport(respond)))
        result = llm.stream("なんだか気になる", [], Cancellation(), deltas.append, sent.append, context)
        assert result["text"] == "".join(sent) == "".join(deltas)
        assert "知りません" in result["text"]
        assert all("intent" not in sentence for sentence in sent)
        assert requests[-1]["response_format"]["json_schema"]["schema"]["properties"]["refs"]["maxItems"] == 0
    finally:
        session.close()


def test_uncertain_recognition_does_not_teach_until_user_confirms(tmp_path):
    class Replies:
        def stream(self, text, history, cancellation, on_delta, on_sentence, context=None):
            on_delta("覚えました。")
            on_sentence("覚えました。")
            return {"text": "覚えました。", "finish_reason": "stop"}

    class SilentMemory:
        def extract(self, *args):
            return {"items": []}

    class Speech:
        def synthesize(self, text, action):
            return {"path": action + ".wav", "duration": 0.1, "played": False}

    session = Session(tmp_path / "learn.sqlite3", Settings(playback=False, tick_seconds=.01))
    runtime = Runtime(session, llm=Replies(), tts=Speech(), memory_llm=SilentMemory())
    runtime.start()
    event = Observation(source=Source.MICROPHONE, text="ルーメリアは月にある街です。",
                        metadata={"recognition_needs_confirmation": True})

    def complete(count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            turns = session.view()["turns"]
            if len(turns) == count and turns[-1]["stage"] == "complete":
                return
            time.sleep(.01)
        raise AssertionError("turn did not complete")

    try:
        runtime.send("observe", event)
        complete(1)
        assert not session.knowledge.entries() and not session.memory.pending()
        assert "合っていますか" in session.view()["turns"][-1]["reply"]
        runtime.send("text", "はい")
        complete(2)
        assert session.knowledge.entries()[0]["value"] == "月にある街"
        assert session.view()["turns"][-1]["recognition_confirmation_of"] == event.event_id
    finally:
        runtime.stop()
        session.close()
