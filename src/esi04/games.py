"""Taught controls and visual-feedback play; no game API, DOM or scripted game state."""

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image
from pydantic import Field, create_model, model_validator
from scipy import ndimage

from esi.models import uid, utc
from esi02.dialogue import Cancelled
from esi02.world import Model
from esi04.native_computer import validate_keys


class Binding(Model):
    name: str = Field(min_length=1, max_length=60)
    keys: list[str] = Field(default_factory=list, max_length=4)
    button: Literal["", "left", "right", "middle"] = ""
    milliseconds: int = Field(default=180, ge=40, le=1000)

    @model_validator(mode="after")
    def valid(self):
        if self.keys:
            validate_keys(self.keys)
        if not self.keys and not self.button:
            raise ValueError("キーかマウスボタンを入力してください。")
        return self


class Cue(Model):
    role: Literal["player", "goal", "danger", "stop"]
    rgb: list[int] = Field(min_length=3, max_length=3)
    tolerance: int = Field(default=28, ge=5, le=80)
    frame_id: str
    frame_sha256: str
    point: list[int] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def valid(self):
        if any(not 0 <= x <= 255 for x in self.rgb) or any(not 0 <= x <= 1000 for x in self.point):
            raise ValueError("目印の色または位置が範囲外です。")
        return self


class GameProfile(Model):
    id: str = Field(default_factory=uid, pattern=r"^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=2000)
    taught_at: str = Field(default_factory=utc)
    application: str = Field(default="", max_length=500)
    controller: Literal["colors", "vision"] = "colors"
    steering: Literal["mouse", "keys"] = "mouse"
    region: list[int] = Field(default_factory=lambda: [0, 0, 1000, 1000], min_length=4, max_length=4)
    player_centered: bool = True
    player_point: list[int] = Field(default_factory=lambda: [500, 500], min_length=2, max_length=2)
    bindings: list[Binding] = Field(default_factory=list, max_length=12)
    cues: list[Cue] = Field(default_factory=list, max_length=24)
    demo_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{32})?$")

    @model_validator(mode="after")
    def valid(self):
        x1, y1, x2, y2 = self.region
        if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000 and x2-x1 >= 50 and y2-y1 >= 50):
            raise ValueError("ゲームの範囲を画像上で大きく囲んでください。")
        if any(not 0 <= x <= 1000 for x in self.player_point):
            raise ValueError("自分の位置が画像の範囲外です。")
        if len({b.name for b in self.bindings}) != len(self.bindings):
            raise ValueError("操作の名前は重複させずに入力してください。")
        return self


class GameDecision(Model):
    controls: list[str] = Field(default_factory=list, max_length=3)
    point: list[int] | None = Field(default=None, min_length=2, max_length=2)
    milliseconds: int = Field(default=180, ge=40, le=1000)
    reason: str = Field(max_length=180)
    stop: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.point and any(not 0 <= value <= 1000 for value in self.point):
            raise ValueError("ゲームの操作位置が画像の範囲外です。")
        if not self.stop and not self.controls and self.point is None:
            raise ValueError("操作するキーかマウス位置を指定するか、停止を選んでください。")
        return self


class GameEpisode(Model):
    id: str = Field(default_factory=uid)
    profile_id: str
    started_at: str = Field(default_factory=utc)
    elapsed: float = 0
    inputs: list[dict] = Field(default_factory=list, max_length=3000)
    reason: str = ""
    # Input accepted and game success are deliberately separate observations.
    outcome: Literal["unverified"] = "unverified"


class VisualGameChoice(Model):
    control: str
    point: list[int] | None = Field(default=None, min_length=2, max_length=2)
    milliseconds: int = Field(default=180, ge=40, le=1000)
    reason: str = Field(max_length=100)

    @model_validator(mode="after")
    def valid(self):
        if self.control == "pointer" and self.point is None:
            raise ValueError("向かうマウス位置が必要です。")
        if self.point and any(not 0 <= value <= 1000 for value in self.point):
            raise ValueError("画像内のマウス位置が必要です。")
        return self


def game_choice_schema(profile):
    ids = [f"b{i}" for i in range(len(profile.bindings))]+["stop"]
    if profile.steering == "mouse":
        ids.append("pointer")
    return create_model("TaughtGameChoice", __base__=VisualGameChoice,
                        control=(Literal[tuple(ids)], ...))


def game_choice(choice, profile):
    if choice.control == "stop":
        return GameDecision(stop=True, reason=choice.reason)
    if choice.control == "pointer" and profile.steering == "mouse":
        if choice.point is None:
            raise ValueError("マウスで向かう位置を指定できませんでした。")
        return GameDecision(point=choice.point, reason=choice.reason, milliseconds=choice.milliseconds)
    ids = {f"b{i}": binding.name for i, binding in enumerate(profile.bindings)}
    if choice.control not in ids:
        raise ValueError("教えていない操作が選ばれたため止めました。")
    return GameDecision(controls=[ids[choice.control]], point=choice.point,
                        reason=choice.reason, milliseconds=choice.milliseconds)


class GameDemo(Model):
    id: str = Field(default_factory=uid)
    profile_id: str
    application: str
    examples: list[dict] = Field(default_factory=list, max_length=240)
    taught_at: str = Field(default_factory=utc)


class GameDemonstration:
    def __init__(self, hub, profile):
        if not hub.grant.demonstrate or not hub.latest or not hub.grant.handle:
            raise PermissionError("対象ウィンドウを選び、実演の記録を許可してください。")
        self.hub, self.profile = hub, profile
        self.application = hub.latest.foreground.application
        self.generation = hub.grant.generation
        self.started = time.monotonic()
        self.demo = GameDemo(profile_id=profile.id, application=self.application)
        self.held = {}
        self.pending = []

    def event(self, event, frame):
        if not frame or frame.generation != self.generation or len(self.demo.examples) >= 240:
            return
        if frame.foreground.handle != self.hub.grant.handle:
            return
        value = dict(event)
        value.update(frame_id=frame.id, frame_sha256=frame.sha256)
        if "x" in value:
            if not frame.physical.contains(value["x"], value["y"]):
                return
            value["point"] = [round((value.pop("x")-frame.physical.x)*1000/frame.physical.width),
                              round((value.pop("y")-frame.physical.y)*1000/frame.physical.height)]
        key = value.get("key") or value.get("button")
        if value["kind"] in {"key_down", "button_down"}:
            if key in self.held:
                return  # Autorepeat is one hold, not a series of presses.
            self.held[key] = value
            self.hub.memory.pinned.add(frame.id)
            chord = [v["key"] for v in self.held.values() if "key" in v]
            for active in self.held.values():
                active["simultaneous_keys"] = list(chord)
            return
        if value["kind"] in {"key_up", "button_up"}:
            before = self.held.pop(key, None)
            if not before:
                return
            value["milliseconds"] = min(60_000, max(40, round((event["at"]-before["at"])*1000)))
            value["started_ms"] = max(0, round((before["at"]-self.started)*1000))
            value["simultaneous_keys"] = before.get("simultaneous_keys", [])
            value["kind"] = "key" if "key" in value else "button"
            value["frame_id"], value["frame_sha256"] = before["frame_id"], before["frame_sha256"]
        value.pop("at", None)
        self.pending.append(value)
        self.hub.memory.pinned.add(value["frame_id"])

    def captured(self, frame):
        if frame.foreground.handle != self.hub.grant.handle:
            return
        for value in self.pending:
            value["after_frame"] = frame.id
            self.demo.examples.append(value)
            if len(self.demo.examples) >= 240:
                break
        if self.pending:
            self.hub.memory.pinned.add(frame.id)
        self.pending.clear()

    def finish(self):
        self.held.clear()
        if not self.demo.examples:
            self.hub.memory.pinned.clear()
            raise ValueError("ゲームの操作が記録されていません。短い動作を見せ直してください。")
        self.hub.memory.record("game_demos", self.demo, maximum=32)
        self.profile.demo_id = self.demo.id
        self.hub.memory.save_game(self.profile)
        self.hub.memory.pinned.clear()
        return self.profile


def presets(name):
    if name == "slither":
        return GameProfile(name="slither.io", instructions="自分の頭は画面中央付近。マウスの方向へ進む。"
            "小さい光る粒を集め、ほかのヘビの体を避ける。危険なときは加速しない。",
            bindings=[Binding(name="加速", keys=["SPACE"], milliseconds=180)])
    letters = ("W", "S", "A", "D") if name == "wasd" else ("UP", "DOWN", "LEFT", "RIGHT")
    return GameProfile(name="2Dゲーム", instructions="自分を動かして、教えた目標へ向かい、教えた危険を避ける。",
                       steering="keys", player_centered=False,
                       bindings=[Binding(name=direction, keys=[key]) for direction, key in
                                 zip(("上", "下", "左", "右"), letters, strict=True)]
                       + [Binding(name="決定", keys=["SPACE"])])


def teach_color(frame, role, point, tolerance=28):
    raw = Path(frame.path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != frame.sha256:
        raise ValueError("教える画像が変更されています。撮り直してください。")
    x, y = point
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        raise ValueError("画像の中をクリックしてください。")
    with Image.open(frame.path) as image:
        image = image.convert("RGB")
        ix, iy = min(image.width-1, x*image.width//1000), min(image.height-1, y*image.height//1000)
        # The exact demonstrated pixel matters for tiny food items; an area median loses them.
        rgb = list(image.getpixel((ix, iy)))
    return Cue(role=role, rgb=rgb, tolerance=tolerance, frame_id=frame.id,
               frame_sha256=frame.sha256, point=point)


class ColorController:
    """Fast local perception from the user's labeled pixels, not a hidden game-state reader."""

    def __init__(self, profile):
        self.profile = profile
        self.last_player = None

    def decide(self, frame):
        profile = self.profile
        with Image.open(frame.path) as source:
            source = source.convert("RGB")
            # Keep small pellets visible, and bound connected-component work.
            source.thumbnail((800, 600))
            image = np.asarray(source, dtype=np.int16)
        h, w = image.shape[:2]
        x1, y1, x2, y2 = profile.region
        left, top, right, bottom = x1*w//1000, y1*h//1000, x2*w//1000, y2*h//1000
        inside = np.zeros((h, w), dtype=bool)
        inside[top:bottom, left:right] = True
        masks = {role: np.zeros((h, w), dtype=bool) for role in ("player", "goal", "danger", "stop")}
        for cue in profile.cues:
            masks[cue.role] |= (np.max(np.abs(image-np.array(cue.rgb)), axis=2) <= cue.tolerance) & inside
        if masks["stop"].sum() >= 8:
            return GameDecision(reason="教えられた終了の目印が見えたので停止", stop=True)
        if profile.player_centered:
            player = np.array([profile.player_point[0]*w/1000, profile.player_point[1]*h/1000])
        else:
            labels, count = ndimage.label(masks["player"])
            objects = ndimage.find_objects(labels)
            candidates = []
            for index, box in enumerate(objects):
                if box:
                    yy, xx = np.nonzero(labels[box] == index+1)
                    if len(xx) >= 3:
                        candidates.append(np.array([xx.mean()+box[1].start, yy.mean()+box[0].start]))
            if not candidates:
                return GameDecision(reason="自分の目印を見失ったので停止", stop=True)
            reference = self.last_player if self.last_player is not None else np.array(profile.player_point)*[w/1000, h/1000]
            player = min(candidates, key=lambda c: np.linalg.norm(c-reference))
            self.last_player = player
        if not (left <= player[0] < right and top <= player[1] < bottom):
            return GameDecision(reason="自分の位置がゲーム範囲の外なので停止", stop=True)
        labels, count = ndimage.label(masks["goal"])
        goals = []
        for index, box in enumerate(ndimage.find_objects(labels)):
            if box:
                yy, xx = np.nonzero(labels[box] == index+1)
                if 1 <= len(xx) <= max(600, w*h//80):
                    goal = np.array([xx.mean()+box[1].start, yy.mean()+box[0].start])
                    if np.linalg.norm(goal-player) > 5:
                        goals.append(goal)
        danger_y, danger_x = np.nonzero(masks["danger"])
        dangers = np.column_stack((danger_x, danger_y))[::max(1, len(danger_x)//3000)]
        # Ignore the head's immediate pixels, which may share antialiasing with another cue.
        if len(dangers):
            dangers = dangers[np.linalg.norm(dangers-player, axis=1) > 10]
        if not goals:
            return GameDecision(reason="教えた目標が見えないので停止。目標の色を追加してください。", stop=True)
        candidates = sorted(goals, key=lambda g: np.linalg.norm(g-player))[:30]
        near_danger = len(dangers) and np.min(np.linalg.norm(dangers-player, axis=1)) < 90
        if near_danger:
            candidates += [player+np.array([math.cos(a), math.sin(a)])*100
                           for a in np.linspace(0, 2*math.pi, 24, endpoint=False)]
        best, best_score = None, -float("inf")
        for target in candidates:
            if not (left+4 <= target[0] < right-4 and top+4 <= target[1] < bottom-4):
                continue
            delta = target-player
            distance = np.linalg.norm(delta)
            unit = delta/max(distance, 1)
            path_length = min(distance, 110)
            if len(dangers):
                along = np.clip((dangers-player) @ unit, 0, path_length)
                clearance = np.min(np.linalg.norm(dangers-(player+along[:, None]*unit), axis=1))
                if clearance < 16:
                    continue
            else:
                clearance = 120
            goal_distance = min(np.linalg.norm(target-goal) for goal in goals)
            score = min(clearance, 100)*1.6-goal_distance-distance*0.12
            if score > best_score:
                best, best_score = target, score
        if best is None:
            return GameDecision(reason="教えた危険を避ける方向が決められないので停止", stop=True)
        controls = []
        if profile.steering == "keys":
            delta = best-player
            maximum = max(abs(delta[0]), abs(delta[1]), 1)
            if abs(delta[0])/maximum > 0.4:
                controls.append("右" if delta[0] > 0 else "左")
            if abs(delta[1])/maximum > 0.4:
                controls.append("下" if delta[1] > 0 else "上")
        return GameDecision(controls=controls, point=[round(best[0]*1000/w), round(best[1]*1000/h)]
                            if profile.steering == "mouse" else None, milliseconds=120,
                            reason="教えた危険を避けて進む" if near_danger else "教えた目標へ向かう")


class GamePlayer:
    def __init__(self, settings, hub, services):
        self.settings, self.hub, self.services = settings, hub, services

    def run(self, profile, seconds, cancellation, on_step=lambda value: None):
        profile = GameProfile.model_validate(profile)
        if not 1 <= seconds <= 300:
            raise ValueError("試す時間は1〜300秒にしてください。")
        if not self.hub.grant.control or not self.hub.grant.handle:
            raise PermissionError("ゲームのウィンドウを選び、PC操作を許可してください。")
        generation = self.hub.grant.generation
        episode = GameEpisode(profile_id=profile.id)
        started = time.monotonic()
        controller = ColorController(profile)
        bindings = {b.name: b for b in profile.bindings}
        last = None
        first_decision = True
        self.hub.interactive = True
        try:
            while time.monotonic()-started < seconds and len(episode.inputs) < 3000:
                cancellation.check()
                if self.hub.native.emergency_pressed():
                    episode.reason = "F8で停止"
                    break
                frame = self.hub.snapshot(cancellation, after=last, generation=generation, timeout=2)
                if profile.application and frame.foreground.application != profile.application:
                    raise ValueError("教えたゲームのアプリと対象が違います。対象を選び直してください。")
                if profile.controller == "colors":
                    decision = controller.decide(frame)
                else:
                    with self.hub.memory.pin(frame.id):
                        question = json.dumps({"rules": profile.instructions, "region": profile.region,
                            "steering": profile.steering, "controls": [{"id": f"b{i}", **b.model_dump()}
                                for i, b in enumerate(profile.bindings)],
                            "recent_inputs": episode.inputs[-3:], "demonstration": self.demo_summary(profile)}, ensure_ascii=False)
                        choice = self.services.game_decision(self.settings, frame, question,
                                                             game_choice_schema(profile), cancellation)
                        decision = game_choice(choice, profile)
                        if time.monotonic()-frame.captured_at > 2:
                            fresh = self.hub.snapshot(cancellation, generation=generation)
                            if fresh.physical == frame.physical and same_game_image(frame, fresh, profile.region):
                                frame = fresh
                cancellation.check()
                if decision.stop:
                    episode.reason = decision.reason
                    break
                if first_decision:
                    # Model startup/first inference is preparation, before the timed play trial.
                    started, first_decision = time.monotonic(), False
                if time.monotonic()-started >= seconds:
                    episode.reason = "試す時間に到達"
                    break
                if any(name not in bindings for name in decision.controls):
                    raise ValueError("教えていない操作が選ばれたため止めました。操作表を確認してください。")
                keys = list(dict.fromkeys(k for name in decision.controls for k in bindings[name].keys))
                buttons = list(dict.fromkeys(bindings[name].button for name in decision.controls if bindings[name].button))
                if len(keys) > 4:
                    raise ValueError("同時に押すキーが多すぎます。")
                duration = min(decision.milliseconds/1000,
                    min((bindings[name].milliseconds/1000 for name in decision.controls), default=1),
                    seconds-(time.monotonic()-started))
                if duration < .02:
                    break
                result = self.hub.interactive_input(frame, keys, buttons, decision.point, duration,
                                                    cancellation, profile.region)
                result["reason"] = decision.reason
                episode.inputs.append(result)
                last = frame.sequence
                on_step({"profile": profile.name, "count": len(episode.inputs), "reason": decision.reason,
                         "elapsed": round(time.monotonic()-started, 1), "frame_id": frame.id})
            episode.reason = episode.reason or "試す時間に到達"
        except (Cancelled, PermissionError, RuntimeError, ValueError, OSError) as exc:
            episode.reason = str(exc) or "操作を中止"
            raise
        finally:
            try:
                self.hub.native.release_inputs()
            finally:
                self.hub.interactive = False
                episode.elapsed = time.monotonic()-started
                self.hub.memory.record("game_episodes", episode, maximum=32)
        return {"stage": "unverified", "summary": f"{profile.name}を{len(episode.inputs)}回判断して試した。{episode.reason}",
                "results": [], "episode": episode.model_dump()}

    def demo_summary(self, profile):
        if not profile.demo_id:
            return []
        path = self.hub.memory.root / "game_demos" / (profile.demo_id+".json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [{key: value for key, value in example.items() if key not in {"frame_id", "frame_sha256", "after_frame"}}
                    for example in data.get("examples", [])[-12:]]
        except (OSError, ValueError):
            return []


def same_game_image(before, after, region):
    with Image.open(before.path) as a, Image.open(after.path) as b:
        if a.size != b.size:
            return False
        box = tuple(round(n*(a.width if i % 2 == 0 else a.height)/1000) for i, n in enumerate(region))
        return a.crop(box).tobytes() == b.crop(box).tobytes()
