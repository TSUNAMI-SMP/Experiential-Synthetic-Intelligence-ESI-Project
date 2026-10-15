"""Bounded screen observations, app-scoped actions and demonstrated skills."""

from typing import Literal

from pydantic import Field, model_validator

from esi02.world import Model


class Rect(Model):
    x: int
    y: int
    width: int = Field(gt=0, le=40000)
    height: int = Field(gt=0, le=40000)

    def contains(self, x, y):
        return self.x <= x < self.x+self.width and self.y <= y < self.y+self.height

    @property
    def center(self):
        return (self.x+self.width//2, self.y+self.height//2)


class Foreground(Model):
    handle: int = 0
    pid: int = 0
    application: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=1000)
    bounds: Rect | None = None


class Element(Model):
    name: str = Field(default="", max_length=500)
    role: str = Field(default="", max_length=100)
    automation_id: str = Field(default="", max_length=300)
    bounds: Rect
    value: str = Field(default="", max_length=4000)
    protected: bool = False
    enabled: bool = True
    focused: bool = False


class Frame(Model):
    id: str
    sequence: int = Field(ge=0)
    generation: int = Field(ge=0)
    captured_at: float = Field(ge=0)
    observed_at: str
    path: str = Field(max_length=4096)
    sha256: str = Field(min_length=64, max_length=64)
    screen: str = Field(max_length=300)
    physical: Rect
    logical: Rect
    image_width: int = Field(gt=0, le=40000)
    image_height: int = Field(gt=0, le=40000)
    foreground: Foreground
    elements: list[Element] = Field(default_factory=list, max_length=256)
    cursor: list[int] = Field(default_factory=lambda: [0, 0], min_length=2, max_length=2)

    def image_point(self, x, y):
        if not 0 <= x <= 1000 or not 0 <= y <= 1000:
            raise ValueError("画面の範囲外です。")
        return (self.physical.x+min(self.physical.width-1, round(x*self.physical.width/1000)),
                self.physical.y+min(self.physical.height-1, round(y*self.physical.height/1000)))

    def logical_point(self, x, y):
        if not self.physical.contains(x, y):
            raise ValueError("選択した画面の範囲外です。")
        return (round(self.logical.x+(x-self.physical.x)*self.logical.width/self.physical.width),
                round(self.logical.y+(y-self.physical.y)*self.logical.height/self.physical.height))


class Selector(Model):
    name: str = Field(default="", max_length=500)
    role: str = Field(default="", max_length=100)
    automation_id: str = Field(default="", max_length=300)

    @classmethod
    def for_element(cls, element):
        # Stable automation IDs survive changing captions and edited field contents.
        return cls(name="" if element.automation_id else element.name,
                   role=element.role, automation_id=element.automation_id)

    def matches(self, element):
        return bool(self.name or self.automation_id) and all(
            not getattr(self, key) or getattr(self, key) == getattr(element, key)
            for key in ("name", "role", "automation_id"))


class Postcondition(Model):
    kind: Literal["element_value", "element_visible", "window_title", "screen_text"]
    selector: Selector | None = None
    value: str = Field(default="", max_length=4000)


class ComputerAction(Model):
    kind: Literal["click", "double_click", "move", "type", "key", "scroll"]
    frame_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    selector: Selector | None = None
    # Coordinates are normalized to the recorded frame, never global invented positions.
    point: list[int] | None = Field(default=None, min_length=2, max_length=2)
    text: str = Field(default="", max_length=2000)
    replace: bool = False
    keys: list[str] = Field(default_factory=list, max_length=4)
    amount: int = Field(default=0, ge=-5, le=5)
    expected: Postcondition | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.point and any(not 0 <= n <= 1000 for n in self.point):
            raise ValueError("操作点は画面内の0〜1000の座標が必要です。")
        if self.kind in {"click", "double_click", "move"} and not (self.point or self.selector):
            raise ValueError("操作対象がありません。")
        if self.kind == "type" and not self.text:
            raise ValueError("入力する文章がありません。")
        if self.kind == "key" and not self.keys:
            raise ValueError("押すキーがありません。")
        return self


class ActionResult(Model):
    id: str
    action: ComputerAction
    before: str
    after: str | None = None
    status: Literal["verified", "changed_unverified", "unchanged", "stopped", "failed"]
    detail: str = Field(max_length=1000)
    observed_at: str
    effect_observed: bool = False


class SkillStep(Model):
    action: ComputerAction
    before_frame: str
    after_frame: str
    # Visual fallback is an actual local crop, matched again before using its offset.
    anchor_path: str = Field(default="", max_length=4096)
    anchor_sha256: str = Field(default="", max_length=64)
    anchor_offset: list[int] = Field(default_factory=lambda: [0, 0], min_length=2, max_length=2)


class Skill(Model):
    id: str
    name: str = Field(min_length=1, max_length=160)
    user_goal: str = Field(min_length=1, max_length=1000)
    application: str = Field(min_length=1, max_length=500)
    taught_at: str
    steps: list[SkillStep] = Field(min_length=1, max_length=80)
    uses: int = Field(default=0, ge=0)
    verified_uses: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    complete: bool = True
    practice_allowed: bool = False
    last_result: str = Field(default="", max_length=1000)


class ScreenTeaching(Model):
    id: str
    frame_id: str
    frame_sha256: str
    user_text: str = Field(min_length=1, max_length=2000)
    region: list[int] | None = Field(default=None, min_length=4, max_length=4)
    observed_at: str
    basis: Literal["user_explanation_of_screen"] = "user_explanation_of_screen"
