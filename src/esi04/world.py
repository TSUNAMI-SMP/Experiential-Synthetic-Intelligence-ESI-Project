"""Persistent objects, reachable actions and observed effects in the virtual room."""

import heapq
import math
import re
from typing import Literal

from pydantic import Field, model_validator

from esi.models import uid
from esi02.world import Model, Point
from esi02.world import World as PreviousWorld

Kind = Literal["snack", "desk", "chair", "bed", "computer", "book", "ball", "water", "window"]
LABELS = {"snack": "おやつ", "desk": "机", "chair": "椅子", "bed": "休む場所", "computer": "パソコン",
          "book": "本", "ball": "ボール", "water": "水", "window": "窓"}
SIZES = {"desk": (1.2, 0.65, 0.75), "chair": (0.5, 0.5, 0.8), "bed": (0.8, 0.8, 0.4),
         "computer": (0.45, 0.3, 0.4), "snack": (0.18, 0.18, 0.12), "book": (0.25, 0.18, 0.04),
         "ball": (0.25, 0.25, 0.25), "water": (0.18, 0.18, 0.25), "window": (0.7, 0.1, 1.0)}


class Object(Model):
    id: str = Field(min_length=1, max_length=64)
    kind: Kind
    label: str = Field(min_length=1, max_length=60)
    position: Point
    quantity: int = Field(default=1, ge=0, le=99)
    parent_id: str | None = Field(default=None, max_length=64)
    revision: int = Field(default=1, ge=1)
    discovered: bool = False
    inspected: bool = False
    used: int = Field(default=0, ge=0)
    last_source: str = Field(default="initial:room", max_length=128)

    @property
    def size(self):
        return SIZES[self.kind]

    @property
    def solid(self):
        return self.kind in {"desk", "chair", "bed"} and self.position.y == 0


def initial_objects():
    return [Object(id=key, kind=kind, label=LABELS[kind], position=Point(x=x, z=z))
            for key, kind, x, z in [("rest", "bed", 1.1, 1.1), ("desk", "desk", 4.5, 1.0),
                                    ("water", "water", 1.2, 3.9), ("window", "window", 5.5, 3.0)]]


class World(PreviousWorld):
    version: Literal[2] = 2
    objects: list[Object] = Field(default_factory=initial_objects, max_length=96)
    revision: int = Field(default=0, ge=0)
    path: list[Point] = Field(default_factory=list, max_length=256)
    events: list[dict] = Field(default_factory=list, max_length=128)
    discoveries: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def object_integrity(self):
        lookup = {item.id: item for item in self.objects}
        if len(lookup) != len(self.objects):
            raise ValueError("空間内の物体IDが重複しています。")
        for item in self.objects:
            width, depth, height = item.size
            if (item.position.x-width/2 < 0 or item.position.x+width/2 > 6
                    or item.position.z-depth/2 < 0 or item.position.z+depth/2 > 5
                    or item.position.y+height > 3):
                raise ValueError("物体が部屋の壁・天井からはみ出しています。")
            if item.kind in {"desk", "chair", "bed"} and item.position.y != 0:
                raise ValueError("この家具は床に置いてください。")
            if item.parent_id:
                parent = lookup.get(item.parent_id)
                if parent is None or parent.kind != "desk" or parent.parent_id:
                    raise ValueError("物を載せる机がありません。")
                if item.kind in {"desk", "bed", "chair", "window"}:
                    raise ValueError("この家具は机に載せられません。")
                dx, dz = abs(item.position.x-parent.position.x), abs(item.position.z-parent.position.z)
                if dx > parent.size[0]/2 or dz > parent.size[1]/2:
                    raise ValueError("机の上の物の位置が机から外れています。")
                if abs(item.position.y-parent.position.y-parent.size[2]) > 0.01:
                    raise ValueError("机に載せる物の高さが合っていません。")
        solids = [o for o in self.objects if o.solid]
        for index, first in enumerate(solids):
            for second in solids[index+1:]:
                if (abs(first.position.x-second.position.x) < (first.size[0]+second.size[0])/2
                        and abs(first.position.z-second.position.z) < (first.size[1]+second.size[1])/2):
                    raise ValueError("家具どうしが重なっています。位置を少し離してください。")
        if any(p.y != 0 for p in self.path):
            raise ValueError("移動経路は床の上にしてください。")
        return self

    def get(self, identifier):
        item = next((o for o in self.objects if o.id == identifier), None)
        if item is None:
            raise ValueError("その物体は空間にありません。")
        return item

    def find(self, name):
        matches = [o for o in self.objects if name in {o.id, o.label, o.kind} and o.quantity > 0]
        if len(matches) != 1:
            raise ValueError("物体を一つ選んでください。同名の場合は一覧のIDで指定できます。")
        return matches[0]

    def free(self, x, z):
        if not 0.16 <= x <= 5.84 or not 0.16 <= z <= 4.84:
            return False
        return not any(o.solid and abs(x-o.position.x) < o.size[0]/2+0.16
                       and abs(z-o.position.z) < o.size[1]/2+0.16 for o in self.objects)

    def near(self, place, radius=0.8):
        item = next((o for o in self.objects if o.id == place and o.quantity > 0), None)
        return bool(item and (radius > 1 or item.position.y <= 1.7)
                    and math.hypot(self.position.x-item.position.x, self.position.z-item.position.z) <= radius)

    def approach(self, item):
        if item.position.y > 1.7:
            raise ValueError("その高さには手が届きません。低い場所か机の上へ移動してください。")
        candidates = [Point(x=i/4, z=j/4) for i in range(1, 24) for j in range(1, 20)
                      if self.free(i/4, j/4) and math.hypot(i/4-item.position.x, j/4-item.position.z) <= 0.8]
        if not candidates:
            raise ValueError("物体の近くに立てる場所がありません。")
        return min(candidates, key=self.position.distance)

    def route(self, target):
        if not self.free(target.x, target.z):
            raise ValueError("移動先は家具や壁に重ならない床を選んでください。")
        cells = [(i, j) for i in range(1, 24) for j in range(1, 20) if self.free(i/4, j/4)]
        start = min(cells, key=lambda p: math.hypot(p[0]/4-self.position.x, p[1]/4-self.position.z))
        goal = min(cells, key=lambda p: math.hypot(p[0]/4-target.x, p[1]/4-target.z))
        available, frontier, cost, parent = set(cells), [(0, start)], {start: 0}, {}
        while frontier:
            _, node = heapq.heappop(frontier)
            if node == goal:
                path = [node]
                while path[-1] in parent:
                    path.append(parent[path[-1]])
                path.reverse()
                return [Point(x=x/4, z=z/4) for x, z in path] + [target]
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = node[0]+dx, node[1]+dz
                proposed = cost[node]+1
                if nxt in available and proposed < cost.get(nxt, 1e9):
                    cost[nxt], parent[nxt] = proposed, node
                    h = abs(nxt[0]-goal[0])+abs(nxt[1]-goal[1])
                    heapq.heappush(frontier, (proposed+h, nxt))
        raise ValueError("家具に遮られて移動先へ進めません。")

    def move_to(self, point, after="idle"):
        target = Point.model_validate(point)
        if after == "exploring" and not self.free(target.x, target.z):
            cells = [Point(x=i/4, z=j/4) for i in range(1, 24) for j in range(1, 20)
                     if self.free(i/4, j/4)]
            if not cells:
                raise ValueError("歩ける床がありません。")
            target = min(cells, key=target.distance)
        if target.y != 0 or after not in {"idle", "resting", "exploring"}:
            raise ValueError("移動先と行動が正しくありません。")
        if after == "resting" and not math.hypot(target.x-self.get("rest").position.x,
                                                target.z-self.get("rest").position.z) <= 0.8:
            raise ValueError("休む場所の近くで休んでください。")
        path = self.route(target)
        self.path, self.destination, self.activity_after_move = path[1:], path[0], after
        self.body.activity = "moving"

    def go_to(self, place):
        self.move_to(self.approach(self.get(place)).model_dump(), "resting" if place == "rest" else "idle")

    def set_activity(self, activity):
        super().set_activity(activity)
        if self.destination is None:
            self.path = []

    def _step(self, dt):
        super()._step(dt)
        if self.destination is None and self.path:
            self.destination, self.path = self.path[0], self.path[1:]
            self.body.activity = "moving"

    def _adopt(self, data):
        validated = type(self).model_validate(data)
        self.__dict__.update(validated.__dict__)

    def object_operation(self, operation, values, source_id):
        if not source_id:
            raise ValueError("空間の操作には出典が必要です。")
        if any(e["source_id"] == source_id for e in self.events):
            return None
        data = self.model_dump()
        before = self.body.model_dump()
        item_id = values.get("id")
        if operation == "place":
            if len(self.objects) >= 96:
                raise ValueError("空間内の物は最大96個です。")
            kind = values["kind"]
            item = Object(id=item_id or uid(), kind=kind, label=values.get("label", LABELS[kind]),
                          position=Point.model_validate(values["position"]), quantity=values.get("quantity", 1),
                          parent_id=values.get("parent_id"), last_source=source_id)
            if item.parent_id:
                parent = self.get(item.parent_id)
                item.position.y = parent.position.y+parent.size[2]
            data["objects"].append(item.model_dump())
            item_id = item.id
        elif operation in {"move", "remove"}:
            item = self.get(item_id)
            if item.id in {"rest", "water", "window"} and operation == "remove":
                raise ValueError("身体維持に使う休憩場所・水・窓は移動で調整してください。")
            if operation == "remove":
                data["objects"] = [o for o in data["objects"] if o["id"] != item_id and o["parent_id"] != item_id]
            else:
                point = Point.model_validate(values["position"])
                parent_id = values.get("parent_id", item.parent_id)
                if parent_id:
                    point.y = self.get(parent_id).position.y+self.get(parent_id).size[2]
                delta = {axis: getattr(point, axis)-getattr(item.position, axis) for axis in "xyz"}
                for obj in data["objects"]:
                    if obj["id"] == item_id or obj["parent_id"] == item_id:
                        obj["position"] = {a: obj["position"][a]+delta[a] for a in "xyz"}
                        obj.update(revision=obj["revision"]+1, discovered=False, last_source=source_id)
                        if obj["id"] == item_id:
                            obj["parent_id"] = parent_id
        elif operation in {"inspect", "eat", "use", "play"}:
            item = self.get(item_id)
            if not self.near(item_id, 0.9):
                raise ValueError("物体の近くに移動してから行動してください。")
            if item.quantity <= 0:
                raise ValueError("その物はもう残っていません。")
            obj = next(o for o in data["objects"] if o["id"] == item_id)
            obj.update(discovered=True, last_source=source_id)
            if operation == "eat":
                if item.kind != "snack":
                    raise ValueError("食べ物だけを食べられます。")
                obj["quantity"] -= 1
                data["body"]["energy"] = min(1, before["energy"]+0.12)
            elif operation == "play" and item.kind != "ball":
                raise ValueError("遊べる物を選んでください。")
            if operation == "inspect":
                obj["inspected"] = True
            else:
                obj["used"] += 1
            obj["revision"] += 1
        else:
            raise ValueError("空間内の物体の操作が正しくありません。")
        candidate = type(self).model_validate(data)
        if not candidate.free(self.position.x, self.position.z):
            raise ValueError("ESIの身体と重なる場所には家具を配置できません。")
        if operation in {"place", "move", "remove"}:
            data["path"], data["destination"] = [], None
            data["body"]["activity"] = "idle"
        event = {"id": uid(), "at": self.elapsed, "operation": operation, "object_id": item_id,
                 "source_id": source_id, "before": before, "after": data["body"],
                 "object": next((o for o in data["objects"] if o["id"] == item_id), None)}
        data["revision"] += 1
        data["events"] = (data["events"]+[event])[-128:]
        self._adopt(data)
        return event

    def discover(self):
        found = []
        for item in self.objects:
            if item.quantity > 0 and not item.discovered and self.near(item.id, 2.5):
                item.discovered = True
                found.append(item.id)
        self.discoveries += len(found)
        return found

    def context(self):
        value = super().context()
        value.update(objects=[o.model_dump() for o in self.objects], revision=self.revision,
                     discoveries=self.discoveries, path=[p.model_dump() for p in self.path])
        return value


ALIASES = {"お菓子": "snack", "クッキー": "snack", "おやつ": "snack", "机": "desk", "デスク": "desk",
           "椅子": "chair", "いす": "chair", "ベッド": "bed", "パソコン": "computer", "PC": "computer",
           "本": "book", "ボール": "ball", "水": "water"}


def scene_command(text, world):
    """Explicit room instructions only; hypothetical teaching never mutates the scene."""
    if re.search(r"できる|例えば|仮に|もし|したら|すると|場合|という|っていう", text):
        return None
    def find(label):
        label = label.strip("「」 \t")
        matches = [o for o in world.objects if o.quantity > 0 and
                   (o.id == label or o.label == label or o.kind == ALIASES.get(label))]
        if len(matches) != 1:
            raise ValueError("どの物体か一つに決められないので、部屋の一覧で選んでもらえる？")
        return matches[0]
    remove = re.fullmatch(r"(.{1,64}?)を(?:取り除いて|撤去して|片付けて|消して)(?:ください|ね)?[。！!\s]*", text)
    movement = re.fullmatch(r"(.{1,64}?)を(.{1,80}?)に(?:移動して|動かして)(?:ください|ね)?[。！!\s]*", text)
    if remove or movement:
        try:
            item = find((remove or movement).group(1))
            if remove:
                return {"operation": "remove", "values": {"id": item.id}}
            target = movement.group(2)
            values = {"id": item.id, "position": item.position.model_dump()}
            if target in {"机の上", "デスクの上"}:
                parent = find("机")
                values.update(position=parent.position.model_dump(), parent_id=parent.id)
            elif target in {"床", "床の上"}:
                values["position"]["y"] = 0.0
                values["parent_id"] = None
            elif target in {"右", "左", "奥", "手前"}:
                axis, amount = {"右": ("x", 0.5), "左": ("x", -0.5), "奥": ("z", -0.5), "手前": ("z", 0.5)}[target]
                values["position"][axis] += amount
            else:
                coordinates = re.fullmatch(r"[（(]?\s*(\d(?:\.\d+)?)\s*[,、]\s*(\d(?:\.\d+)?)\s*[,、]\s*(\d(?:\.\d+)?)\s*[)）]?", target)
                if not coordinates:
                    return {"error": "移動先は、右・左・奥・手前・床・机の上、または（x,y,z）の座標で指定してもらえる？"}
                values.update(position=dict(zip("xyz", map(float, coordinates.groups()), strict=True)), parent_id=None)
            return {"operation": "move", "values": values}
        except ValueError as exc:
            return {"error": str(exc)}
    match = re.fullmatch(r"(?:仮想(?:の)?(?:空間|部屋)(?:に|で))?(?:(机|デスク)の上に)?(.{1,30}?)(?:を|は)(\d{1,2}|[一二三四五])?(?:個|つ|台|冊)?(?:置いて|置く|配置して)(?:ください|くれる|ね)?[。！!？?\s]*", text)
    if not match:
        return None
    surface, label, count = match.groups()
    kind = next((kind for alias, kind in ALIASES.items() if alias in label), None)
    if kind is None:
        return {"error": "置く物は、おやつ・机・椅子・ベッド・パソコン・本・ボール・水から選べるよ。"}
    quantity = int(count) if count and count.isdigit() else "一二三四五".index(count)+1 if count else 1
    if surface:
        desks = [o for o in world.objects if o.kind == "desk"]
        if len(desks) != 1:
            return {"error": "どの机に置くか、空間の一覧で選んでもらえる？"}
        parent = desks[0]
        position, parent_id = parent.position.model_dump(), parent.id
    else:
        p = world.position
        position, parent_id = {"x": min(5.5, p.x+0.7), "y": 0.0, "z": p.z}, None
        if kind in {"desk", "chair", "bed"}:
            positions = sorted((Point(x=i/2, z=j/2) for i in range(1, 12) for j in range(1, 10)),
                               key=world.position.distance)
            position = None
            for point in positions:
                data = world.model_dump()
                data["objects"].append(Object(id=uid(), kind=kind, label=label, position=point).model_dump())
                try:
                    candidate = type(world).model_validate(data)
                    if candidate.free(world.position.x, world.position.z):
                        position = point.model_dump()
                        break
                except ValueError:
                    continue
            if position is None:
                return {"error": "この家具を置ける空き場所を見つけられないので、部屋の配置を調整してもらえる？"}
    return {"operation": "place", "values": {"kind": kind, "label": label, "quantity": quantity,
                                               "position": position, "parent_id": parent_id}}
