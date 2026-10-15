"""Feedback play, taught mappings, input revocation and source-linked demonstrations."""

import threading
import time
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from esi.models import utc
from esi02.dialogue import Cancellation, Cancelled
from esi04.computer import ComputerHub, ComputerMemory
from esi04.computer_models import Foreground, Rect
from esi04.config import Settings
from esi04.games import (
    Binding,
    ColorController,
    GameDemonstration,
    GamePlayer,
    VisualGameChoice,
    game_choice_schema,
    presets,
    teach_color,
)
from esi04.native_computer import HeldInputs


class Native(HeldInputs):
    def __init__(self):
        self._input_state()
        self.app = Foreground(handle=17, pid=123, application="game.exe")
        self.inputs = []
        self.stop = False

    def foreground(self):
        return self.app

    def _key_event(self, key, down):
        self.inputs.append(("key", key, down))

    def _button_event(self, button, down):
        self.inputs.append(("button", button, down))

    def move(self, x, y):
        self.inputs.append(("move", x, y))

    def emergency_pressed(self):
        return self.stop


def publish(hub, goal=(320, 130), wall=False):
    image = Image.new("RGB", (400, 260), "#101820")
    draw = ImageDraw.Draw(image)
    draw.ellipse((95, 120, 115, 140), fill=(30, 230, 80))
    draw.ellipse((goal[0]-4, goal[1]-4, goal[0]+4, goal[1]+4), fill=(250, 220, 20))
    if wall:
        draw.rectangle((145, 40, 170, 200), fill=(230, 20, 40))
    stream = BytesIO()
    image.save(stream, format="PNG")
    frame = hub.memory.save_frame(stream.getvalue(), sequence=hub.sequence+1, generation=hub.grant.generation,
        captured_at=time.monotonic(), observed_at=utc(), screen="screen", image_width=400, image_height=260,
        physical=Rect(x=-400, y=0, width=400, height=260), logical=Rect(x=-400, y=0, width=400, height=260),
        foreground=hub.native.foreground())
    hub.publish(frame)
    return frame


@pytest.fixture
def hub(tmp_path):
    value = ComputerHub(ComputerMemory(tmp_path), Native())
    value.configure("screen", control=True, demonstrate=True, application="game.exe", handle=17, pid=123)
    publish(value)
    return value


def profile_for(hub, steering="arrows"):
    profile = presets(steering)
    profile.application = "game.exe"
    profile.player_point = [260, 500]
    for role, point in (("player", [260, 500]), ("goal", [800, 500])):
        profile.cues.append(teach_color(hub.latest, role, point))
    return profile


def test_feedback_changes_direction_when_the_visible_goal_changes(hub):
    profile = profile_for(hub)
    controller = ColorController(profile)
    assert controller.decide(hub.latest).controls == ["右"]
    frame = publish(hub, goal=(105, 40))
    assert controller.decide(frame).controls == ["上"]
    profile.controller = "colors"
    hub.memory.save_game(profile)
    assert hub.memory.games()[0] == profile


def test_taught_obstacle_changes_the_route_instead_of_crossing_it(hub):
    publish(hub, wall=True)
    profile = profile_for(hub)
    profile.cues.append(teach_color(hub.latest, "danger", [390, 400]))
    decision = ColorController(profile).decide(hub.latest)
    assert decision.stop or decision.controls != ["右"]


def test_missing_player_stops_without_inventing_a_position(hub):
    profile = profile_for(hub)
    profile.cues = [c for c in profile.cues if c.role != "player"]
    decision = ColorController(profile).decide(hub.latest)
    assert decision.stop and "自分" in decision.reason


@pytest.mark.parametrize("ending", ["revoke", "cancel", "focus", "f8"])
def test_mid_hold_stop_releases_every_key_and_mouse_button(hub, ending):
    cancellation = Cancellation()
    errors = []
    def operation():
        try:
            hub.interactive_input(hub.latest, ["W", "SPACE"], ["left"], [500, 500], 1,
                                  cancellation, [0, 0, 1000, 1000])
        except (Cancelled, PermissionError, RuntimeError) as exc:
            errors.append(exc)
    worker = threading.Thread(target=operation)
    worker.start()
    deadline = time.monotonic()+1
    while not hub.native.held_buttons and time.monotonic() < deadline:
        time.sleep(.005)
    assert hub.native.held_keys == {"W", "SPACE"}
    if ending == "revoke":
        hub.revoke()
    elif ending == "cancel":
        cancellation.set()
    elif ending == "focus":
        hub.native.app = Foreground(handle=99, application="other.exe")
    else:
        hub.native.stop = True
    worker.join(.5)
    assert not worker.is_alive() and errors
    assert not hub.native.held_keys and not hub.native.held_buttons
    assert ("key", "SPACE", False) in hub.native.inputs
    assert ("button", "left", False) in hub.native.inputs


def test_stale_vision_decision_never_sends_an_input(hub):
    frame = hub.latest.model_copy(update={"captured_at": time.monotonic()-3})
    with pytest.raises(RuntimeError, match="古く"):
        hub.interactive_input(frame, ["LEFT"], [], None, .1, Cancellation(), [0, 0, 1000, 1000])
    assert not hub.native.inputs


def test_game_requires_an_exact_window_and_region(hub):
    frame = hub.latest
    profile = profile_for(hub)
    with pytest.raises(ValueError, match="範囲外"):
        hub.interactive_input(frame, [], [], [10, 10], .1, Cancellation(), [100, 100, 900, 900])
    hub.configure("screen", control=True)
    with pytest.raises(PermissionError, match="ウィンドウ"):
        GamePlayer(Settings(), hub, None).run(profile.model_dump(), 1, Cancellation())


def test_realtime_player_uses_taught_key_mapping_and_new_frames(hub):
    profile = profile_for(hub, "wasd")
    running = threading.Event()
    def publishing():
        while not running.wait(.035):
            publish(hub)
    worker = threading.Thread(target=publishing)
    worker.start()
    try:
        result = GamePlayer(Settings(), hub, None).run(profile.model_dump(), 1, Cancellation())
        assert len(result["episode"]["inputs"]) >= 3
        assert ("key", "D", True) in hub.native.inputs
        assert all(k != ("key", "RIGHT", True) for k in hub.native.inputs)
        assert len({r["frame_id"] for r in result["episode"]["inputs"]}) > 1
        assert result["stage"] == "unverified"
        assert not hub.native.held_keys
    finally:
        running.set()
        worker.join(1)


def test_game_demo_preserves_hold_and_before_after_sources(hub):
    profile = profile_for(hub)
    demo = GameDemonstration(hub, profile)
    before = hub.latest
    demo.event({"kind": "key_down", "key": "RIGHT", "at": 1.0}, before)
    demo.event({"kind": "key_down", "key": "RIGHT", "at": 1.1}, before)  # repeat
    demo.event({"kind": "key_up", "key": "RIGHT", "at": 1.35}, before)
    after = publish(hub)
    demo.captured(after)
    saved = demo.finish()
    summary = GamePlayer(Settings(), hub, None).demo_summary(saved)
    assert len(summary) == 1 and summary[0]["milliseconds"] == 350
    assert hub.memory.games()[0].demo_id == demo.demo.id
    assert demo.demo.examples[0]["frame_id"] == before.id
    assert demo.demo.examples[0]["after_frame"] == after.id


@pytest.mark.parametrize("screen_changed", [False, True])
def test_slow_vision_uses_a_fresh_static_image_and_stops_for_a_moving_game(hub, screen_changed):
    profile = profile_for(hub)
    profile.controller = "vision"
    class Screens:
        calls = 0

        def game_decision(self, settings, frame, question, schema, cancellation):
            self.calls += 1
            frame.captured_at = time.monotonic()-3
            publish(hub, goal=(160, 30) if screen_changed else (320, 130))
            return VisualGameChoice(control="stop" if self.calls > 1 else "b3", milliseconds=40, reason="教えた目標へ向かう")
    if screen_changed:
        with pytest.raises(RuntimeError, match="古く"):
            GamePlayer(Settings(), hub, Screens()).run(profile.model_dump(), 1, Cancellation())
        assert not hub.native.inputs
    else:
        result = GamePlayer(Settings(), hub, Screens()).run(profile.model_dump(), 1, Cancellation(), on_step=lambda _: publish(hub))
        assert len(result["episode"]["inputs"]) == 1
        assert ("key", "RIGHT", True) in hub.native.inputs


def test_unknown_vision_control_is_never_injected(hub):
    profile = profile_for(hub)
    profile.controller = "vision"
    class Screens:
        def game_decision(self, *args):
            return VisualGameChoice(control="unknown", reason="推測")
    with pytest.raises(ValueError, match="教えていない"):
        GamePlayer(Settings(), hub, Screens()).run(profile.model_dump(), 1, Cancellation())
    assert not hub.native.inputs


def test_visual_model_schema_requires_one_of_the_taught_input_ids():
    schema = game_choice_schema(presets("arrows"))
    assert schema.model_validate({"control": "b3", "reason": "右へ"}).control == "b3"
    with pytest.raises(ValueError):
        schema.model_validate({"control": "ENTER", "reason": "教えていないキー"})
    with pytest.raises(ValueError):
        schema.model_validate({"reason": "右へ進むが入力がない"})


@pytest.mark.parametrize("keys", [["ALT", "TAB"], ["F8"], ["W", "A", "S", "D", "SPACE"]])
def test_unusable_control_bindings_are_rejected(keys):
    with pytest.raises(ValueError):
        Binding(name="bad", keys=keys)
