"""PC access tests cover revocation, scope, actual postconditions and visual ambiguity."""

import threading
import time
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from esi.models import uid, utc
from esi02.dialogue import Cancellation, Cancelled
from esi04.computer import ComputerHub, ComputerMemory, crop_anchor, match_anchor
from esi04.computer_models import Element, Foreground, Rect
from esi04.config import Settings
from esi04.session import Session


class Native:
    def __init__(self):
        self.counter = 0
        self.calls = []
        self.app = Foreground(handle=17, pid=123, application="test.exe", title="test")
        self.after_type = None

    def foreground(self):
        return self.app

    def click(self, x, y, double=False):
        self.calls.append(("click", x, y))
        self.counter += 1

    def type(self, text):
        self.calls.append(("type", text))
        if self.after_type:
            self.after_type()

    def key(self, keys):
        self.calls.append(("key", keys))

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))


def png(duplicate=False):
    image = Image.new("RGB", (400, 260), "white")
    draw = ImageDraw.Draw(image)
    for x in ([70, 260] if duplicate else [70]):
        draw.rectangle((x, 70, x+70, 120), fill="navy", outline="black", width=3)
        draw.text((x+12, 84), "GO 42", fill="white")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def publish(hub, raw=None):
    count = hub.native.counter
    element = Element(name="Counter", role="Edit", automation_id="count",
                      bounds=Rect(x=40, y=40, width=100, height=50), value=str(count))
    frame = hub.memory.save_frame(raw or png(), sequence=hub.sequence+1, generation=hub.grant.generation,
        captured_at=time.monotonic(), observed_at=utc(), screen="test",
        physical=Rect(x=0, y=0, width=400, height=260), logical=Rect(x=0, y=0, width=400, height=260),
        image_width=400, image_height=260, foreground=hub.native.foreground(), elements=[element])
    hub.publish(frame)
    return frame


@pytest.fixture
def hub(tmp_path):
    value = ComputerHub(ComputerMemory(tmp_path / "screen", 32), Native())
    value.configure("test", observe=True, control=True, application="test.exe", pid=123)
    publish(value)
    return value


def test_actual_condition_and_effect_are_separate_from_changed_pixels(hub):
    stop = threading.Event()
    def publishing():
        while not stop.wait(0.04):
            publish(hub)
    worker = threading.Thread(target=publishing)
    worker.start()
    try:
        before = hub.latest
        result, after = hub.act({"kind": "click", "frame_id": before.id, "point": [250, 300],
                                 "expected": {"kind": "element_value", "selector": {"automation_id": "count"}, "value": "1"}},
                                Cancellation())
        assert result.status == "verified" and result.effect_observed
        assert hub.native.counter == 1
        result, _ = hub.act({"kind": "click", "frame_id": after.id, "point": [250, 300],
                             "expected": {"kind": "element_value", "selector": {"automation_id": "count"}, "value": "99"}},
                            Cancellation())
        assert result.status != "verified" and not result.effect_observed
    finally:
        stop.set()
        worker.join(3)


def test_restart_never_restores_grants_or_reuses_old_frame(hub):
    old = hub.latest
    hub.revoke()
    hub.configure("test", observe=True, control=True)
    publish(hub)
    with pytest.raises(ValueError):
        hub.act({"kind": "click", "frame_id": old.id, "point": [250, 300]}, Cancellation())
    assert not hub.native.calls
    reopened = ComputerHub(hub.memory, hub.native)
    assert not reopened.grant.observe and not reopened.grant.control
    with pytest.raises(PermissionError):
        reopened.snapshot(Cancellation())


def test_application_change_blocks_before_input(hub):
    original = hub.latest
    hub.native.app = Foreground(handle=20, pid=123, application="other.exe", title="other")
    with pytest.raises(RuntimeError):
        hub.act({"kind": "click", "frame_id": original.id, "point": [250, 300]}, Cancellation())
    assert hub.native.calls == []


def test_revocation_interrupts_remaining_text(hub):
    hub.native.after_type = hub.revoke
    with pytest.raises((PermissionError, Cancelled)):
        hub.act({"kind": "type", "frame_id": hub.latest.id, "text": "private text"}, Cancellation())
    assert hub.native.calls == [("type", "p")]


def test_visual_anchor_must_be_unique_and_maps_actual_pixels(hub):
    frame = hub.latest
    path, digest, offset = crop_anchor(hub.memory, frame, 100, 95)
    assert match_anchor(frame, path, digest, offset) == (100, 95)
    duplicate = publish(hub, png(True))
    with pytest.raises(ValueError, match="複数"):
        match_anchor(duplicate, path, digest, offset)
    with open(path, "ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="変更"):
        match_anchor(frame, path, digest, offset)


def test_negative_monitor_origin_and_dpi_mapping(hub):
    frame = hub.latest.model_copy(update={"physical": Rect(x=-1920, y=0, width=1920, height=1080),
                                         "logical": Rect(x=-1280, y=0, width=1280, height=720)})
    assert frame.image_point(500, 500) == (-960, 540)
    assert frame.logical_point(-960, 540) == (-640, 360)
    with pytest.raises(ValueError):
        frame.logical_point(0, 0)


def test_unverified_computer_claim_cannot_train_reward(tmp_path):
    session = Session(tmp_path / "session.sqlite3", Settings(playback=False))
    try:
        before = session.experience.state.habits["explore"].trials
        session.computer_finished({"stage": "unverified", "summary": "done", "results": [
            {"id": uid(), "status": "changed_unverified", "before": "a", "after": "b", "effect_observed": False}]})
        assert session.experience.state.habits["explore"].trials == before
        with pytest.raises(ValueError):
            session.experience.outcome("fake", "explore", 0.1, "computer_observation", "fake",
                                       {"id": "fake", "status": "verified", "before": "a", "after": "b"})
    finally:
        session.close()


def test_stale_grant_is_rejected_for_semantic_targets_too(hub):
    old = hub.latest
    hub.revoke()
    hub.configure("test", observe=True, control=True)
    publish(hub)
    with pytest.raises(ValueError, match="古く"):
        hub.act({"kind": "click", "frame_id": old.id, "selector": {"automation_id": "count"}}, Cancellation())
    assert not hub.native.calls


def test_changed_keyboard_focus_is_reobserved_before_typing(hub):
    first = publish(hub)
    first.elements[0].focused = True
    hub.publish(first)
    second = publish(hub)
    second.elements[0].focused = True
    second.elements[0].automation_id = "different-field"
    hub.publish(second)
    with pytest.raises(ValueError, match="入力先が変わり"):
        hub.act({"kind": "type", "frame_id": first.id, "text": "do not misdirect"}, Cancellation())
    assert not hub.native.calls


@pytest.mark.parametrize("goal", ["一度だけクリックして", "このボタンをクリックして"])
def test_model_cannot_repeat_an_unverified_click(hub, goal):
    from esi04.computer_agent import ComputerAgent, OperationPlan
    from esi04.vision import Observation, VisualDescription

    class Screens:
        def observe(self, settings, frame, question, cancellation):
            return Observation(id=uid(), frame_id=frame.id, frame_sha256=frame.sha256, question=question,
                               observed_at=utc(), result=VisualDescription(description="Counter: 1"))
    def always_click(*args):
        return OperationPlan(action="click", target="e0", text="", keys=[], amount=0,
                             expected_target="", expected_value="", summary="クリック")
    stop = threading.Event()
    def publishing():
        while not stop.wait(.04):
            publish(hub)
    worker = threading.Thread(target=publishing)
    worker.start()
    try:
        result = ComputerAgent(Settings(), hub, Screens(), decide=always_click).run(goal, Cancellation(), max_steps=3)
        assert hub.native.counter == 1 and len(result["results"]) == 1
        assert result["stage"] in {"unverified", "clarify"}
        assert not result["results"][0]["effect_observed"]
    finally:
        stop.set()
        worker.join(3)
