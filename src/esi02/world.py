"""One persistent 3-D room and a deliberately simplified virtual body, not physiology."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False, strict=True)


class Point(Model):
    x: float = Field(ge=0, le=6)
    y: float = Field(default=0, ge=0, le=3)
    z: float = Field(ge=0, le=5)

    def distance(self, other):
        return math.sqrt(sum((getattr(self, axis) - getattr(other, axis)) ** 2 for axis in "xyz"))


PLACES = {
    "rest": {"label": "休む場所", "position": {"x": 1.1, "y": 0, "z": 1.1}},
    "desk": {"label": "机", "position": {"x": 4.5, "y": 0, "z": 1.0}},
    "window": {"label": "窓の近く", "position": {"x": 5.5, "y": 0, "z": 3.0}},
    "water": {"label": "水飲み場", "position": {"x": 1.2, "y": 0, "z": 3.9}},
}


class Room(Model):
    temperature: float = Field(default=23, ge=-10, le=45)
    noise_db: float = Field(default=30, ge=15, le=95)
    lamp: bool = True
    cycle_seconds: float = Field(default=1200, ge=60, le=86400)


class Body(Model):
    energy: float = Field(default=0.85, ge=0, le=1)
    fatigue: float = Field(default=0.10, ge=0, le=1)
    hydration: float = Field(default=0.85, ge=0, le=1)
    condition: float = Field(default=1, ge=0, le=1)
    temperature: float = Field(default=37, ge=30, le=43)
    activity: Literal["idle", "moving", "resting", "talking", "exploring"] = "idle"


class Stimulus(Model):
    temperature: float = Field(ge=-15, le=50)
    light_lux: float = Field(ge=0, le=3000)
    noise_db: float = Field(ge=0, le=110)
    near_rest: bool
    near_water: bool
    thermal_discomfort: float = Field(ge=0, le=1)
    noise_discomfort: float = Field(ge=0, le=1)
    body_discomfort: float = Field(ge=0, le=1)


class EnvironmentChange(Model):
    temperature: float | None = Field(default=None, ge=-10, le=45)
    noise_db: float | None = Field(default=None, ge=15, le=95)
    lamp: bool | None = None


class World(Model):
    version: Literal[1] = 1
    elapsed: float = Field(default=0, ge=0)
    room: Room = Field(default_factory=Room)
    body: Body = Field(default_factory=Body)
    position: Point = Field(default_factory=lambda: Point(x=3, z=2.5))
    destination: Point | None = None
    activity_after_move: Literal["idle", "resting", "exploring"] = "idle"
    distance_travelled: float = Field(default=0, ge=0)
    last_intervention: dict | None = None
    conversation_active: bool = False

    @model_validator(mode="after")
    def on_floor(self):
        if self.position.y != 0 or (self.destination and self.destination.y != 0):
            raise ValueError("この部屋では床の上を移動します。高さは0にしてください。")
        return self

    def near(self, place, radius=0.8):
        return self.position.distance(Point.model_validate(PLACES[place]["position"])) <= radius

    def observe(self):
        phase = 2 * math.pi * self.elapsed / self.room.cycle_seconds
        window_distance = self.position.distance(Point.model_validate(PLACES["window"]["position"]))
        daylight = (1 + math.sin(phase)) / 2
        temperature = (
            self.room.temperature
            + 1.2 * math.sin(phase * 0.7)
            + 0.7 * daylight * math.exp(-window_distance / 2)
        )
        light = 60 + 800 * daylight * math.exp(-window_distance / 4) + (240 if self.room.lamp else 0)
        noise = max(0, self.room.noise_db + 2 * math.sin(phase * 2.3) - (4 if self.near("rest") else 0))
        thermal = min(1, max(0, abs(temperature - 23) - 3) / 15)
        noise_discomfort = min(1, max(0, noise - 50) / 40)
        body_discomfort = min(
            1, max(0, abs(self.body.temperature - 37) - 0.4) / 2 + 0.4 * (1 - self.body.condition)
        )
        return Stimulus(
            temperature=temperature,
            light_lux=light,
            noise_db=noise,
            near_rest=self.near("rest"),
            near_water=self.near("water"),
            thermal_discomfort=thermal,
            noise_discomfort=noise_discomfort,
            body_discomfort=body_discomfort,
        )

    def change_environment(self, values):
        change = EnvironmentChange.model_validate(values)
        updated = self.room.model_copy(update=change.model_dump(exclude_none=True))
        self.room = Room.model_validate(updated.model_dump())
        self.last_intervention = {
            "kind": "environment",
            "elapsed": self.elapsed,
            "values": change.model_dump(exclude_none=True),
        }

    def move_to(self, point, after="idle"):
        point = Point.model_validate(point)
        if point.y != 0 or after not in {"idle", "resting", "exploring"}:
            raise ValueError("移動先または移動後の行動が正しくありません。")
        if after == "resting" and point.distance(Point.model_validate(PLACES["rest"]["position"])) > 0.8:
            raise ValueError("休む場所の近くで休んでください。")
        self.destination = point
        self.activity_after_move = after
        self.body.activity = "moving"

    def go_to(self, place):
        if place not in PLACES:
            raise ValueError("部屋にある場所を選んでください。")
        self.move_to(PLACES[place]["position"], "resting" if place == "rest" else "idle")

    def set_activity(self, activity):
        if activity not in {"idle", "resting", "talking", "exploring"}:
            raise ValueError("この身体の行動が正しくありません。")
        if activity == "resting" and not self.near("rest"):
            self.go_to("rest")
        else:
            self.destination = None
            self.body.activity = activity

    def drink(self):
        if not self.near("water"):
            raise ValueError("水飲み場に着いてから飲めます。")
        before = self.body.hydration
        self.body.hydration = min(1, before + 0.30)
        self.last_intervention = {
            "kind": "drink",
            "elapsed": self.elapsed,
            "hydration_added": self.body.hydration - before,
        }

    def advance(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0 or seconds > 60:
            raise ValueError("身体の時間更新は0〜60秒の有限値にしてください。")
        # Small bounded steps make temperature and movement stable regardless of GUI frame rate.
        remaining = seconds
        while remaining > 1e-9:
            dt = min(0.5, remaining)
            self._step(dt)
            remaining -= dt

    def _step(self, dt):
        if self.destination:
            distance = self.position.distance(self.destination)
            travel = min(distance, 0.7 * dt)
            if distance > 1e-9:
                fraction = travel / distance
                self.position = Point(
                    x=self.position.x + (self.destination.x - self.position.x) * fraction,
                    z=self.position.z + (self.destination.z - self.position.z) * fraction,
                )
                self.distance_travelled += travel
            if distance <= travel + 1e-9:
                self.position = self.destination
                self.destination = None
                self.body.activity = self.activity_after_move
        stimulus = self.observe()
        body, activity = self.body, self.body.activity
        resting = activity == "resting" and stimulus.near_rest
        load = {"idle": 1.0, "moving": 3.5, "resting": 0.4, "talking": 1.8, "exploring": 2.0}[activity]
        load += 0.8 if self.conversation_active else 0
        strain = stimulus.thermal_discomfort + 0.5 * stimulus.noise_discomfort
        energy_rate = (0.0007 * (1 - body.energy) if resting else -0.00007 * load) - 0.00008 * strain
        body.energy = min(1, max(0, body.energy + energy_rate * dt))
        body.fatigue = min(
            1,
            max(
                0,
                body.fatigue
                + (
                    (-0.002 * body.fatigue if resting else 0.00009 * load)
                    + (0.00004 if self.conversation_active else 0)
                    + 0.00006 * strain
                )
                * dt,
            ),
        )
        body.hydration = max(
            0, body.hydration - (0.000045 * load + 0.00007 * stimulus.thermal_discomfort) * dt
        )
        target_temperature = min(41, max(33, 37 + 0.065 * (stimulus.temperature - 23) + 0.06 * (load - 1)))
        body.temperature = target_temperature + (body.temperature - target_temperature) * math.exp(-dt / 180)
        danger = (
            max(0, 0.20 - body.hydration)
            + max(0, 0.10 - body.energy)
            + max(0, abs(body.temperature - 37) - 1.3) * 0.2
        )
        condition_rate = -0.0005 * danger if danger else 0.00003 * (1 - body.condition)
        body.condition = min(1, max(0, body.condition + condition_rate * dt))
        self.elapsed += dt

    def context(self):
        return {
            "simulation_only": True,
            "room_metres": [6, 3, 5],
            "position": self.position.model_dump(),
            "body": self.body.model_dump(),
            "stimulus": self.observe().model_dump(),
            "destination": self.destination.model_dump() if self.destination else None,
            "elapsed_seconds": self.elapsed,
            "conversation_active": self.conversation_active,
        }
