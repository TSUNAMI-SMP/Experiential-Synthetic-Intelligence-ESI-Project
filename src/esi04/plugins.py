"""Declarative environment plugins. Connections and control leases never live in a save."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import ConfigDict, Field, model_validator

from esi.models import uid
from esi02.desktop_preferences import write_json
from esi02.world import Model
from esi04.environment_learning import LifeLearning


class PluginManifest(Model):
    api_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(max_length=1200)
    provider: Literal["minecraft", "local_environment"] = "local_environment"
    actions: list[str] = Field(min_length=1, max_length=32)
    disabled_concepts: list[Literal["noise", "hydration", "temperature", "virtual_body", "virtual_room"]] = Field(default_factory=list)
    basic_knowledge: list[str] = Field(default_factory=list, max_length=40)


class PluginCatalog:
    def __init__(self, folder):
        self.folder = Path(folder)

    @staticmethod
    def builtin():
        return PluginManifest.model_validate_json(files("esi04").joinpath("resources/plugins/minecraft.json").read_text(encoding="utf-8"))

    def manifests(self):
        built = self.builtin()
        result = {built.id: built}
        for path in sorted(self.folder.glob("*.json"))[:32]:
            try:
                if path.stat().st_size > 64_000:
                    continue
                manifest = PluginManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if manifest.id != built.id:
                result[manifest.id] = manifest
        return list(result.values())

    def install(self, source):
        source = Path(source)
        if source.stat().st_size > 64_000:
            raise ValueError("プラグイン定義は64KBまでです。")
        manifest = PluginManifest.model_validate_json(source.read_text(encoding="utf-8-sig"))
        if manifest.id == self.builtin().id:
            raise ValueError("同梱プラグインを外部の定義で上書きすることはできません。")
        self.folder.mkdir(parents=True, exist_ok=True)
        write_json(self.folder / (manifest.id+".json"), manifest.model_dump())
        return manifest


class Connection(Model):
    model_config = ConfigDict(hide_input_in_errors=True)
    endpoint: str = "http://127.0.0.1:8766"
    token: str = Field(min_length=16, max_length=256, repr=False)

    @model_validator(mode="after")
    def local(self):
        url = urlparse(self.endpoint)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not url.port or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("接続先には、このPC内のHTTP URLとポートを指定してください。")
        if not self.token.isascii() or any(c.isspace() for c in self.token):
            raise ValueError("接続コードはMinecraftからコピーした英数字のコードを指定してください。")
        return self


class Actor(Model):
    id: str = Field(max_length=80)
    name: str = Field(max_length=80)
    dimension: str = Field(max_length=120)
    x: float = Field(ge=-30_000_000, le=30_000_000)
    y: float = Field(ge=-2048, le=2048)
    z: float = Field(ge=-30_000_000, le=30_000_000)
    yaw: float = Field(allow_inf_nan=False)
    pitch: float = Field(ge=-90, le=90)
    health: float = Field(ge=0, le=2048)
    max_health: float = Field(gt=0, le=2048)
    food: int = Field(ge=0, le=20)
    saturation: float = Field(ge=0, le=20)
    air: int = Field(ge=-20, le=300)
    dead: bool = False
    on_ground: bool = True


class Item(Model):
    slot: int = Field(ge=0, le=40)
    name: str = Field(max_length=120)
    count: int = Field(ge=1, le=999)
    edible: bool = False
    placeable: bool = False
    selected: bool = False
    durability: int | None = Field(default=None, ge=0)


class Target(Model):
    id: str = Field(min_length=1, max_length=120)
    kind: Literal["block", "entity", "waypoint"]
    name: str = Field(max_length=160)
    x: float = Field(ge=-30_000_000, le=30_000_000)
    y: float = Field(ge=-2048, le=2048)
    z: float = Field(ge=-30_000_000, le=30_000_000)
    distance: float = Field(ge=0, le=64)
    reachable: bool = False
    harvestable: bool = False


class Recipe(Model):
    id: str = Field(max_length=160)
    output: str = Field(max_length=120)
    count: int = Field(ge=1, le=999)
    ingredients: list[str] = Field(default_factory=list, max_length=9)
    station: Literal["inventory", "crafting_table"]
    craftable: bool = False
    basis: Literal["unlocked_recipe_book"] = "unlocked_recipe_book"


class ContainerItem(Model):
    slot: int = Field(ge=0, le=89)
    name: str = Field(max_length=120)
    count: int = Field(ge=1, le=999)


class OpenContainer(Model):
    id: int = Field(ge=1)
    kind: str = Field(max_length=120)
    target_id: str = Field(max_length=120)
    x: float | None = Field(default=None, ge=-30_000_000, le=30_000_000)
    y: float | None = Field(default=None, ge=-2048, le=2048)
    z: float | None = Field(default=None, ge=-30_000_000, le=30_000_000)
    items: list[ContainerItem] = Field(default_factory=list, max_length=90)


class EnvironmentObservation(Model):
    protocol: Literal[1] = 1
    id: str = Field(min_length=1, max_length=80)
    plugin_id: str
    world_id: str = Field(max_length=100)
    session_id: str = Field(max_length=100)
    tick: int = Field(ge=0)
    actor: Actor
    inventory: list[Item] = Field(default_factory=list, max_length=41)
    targets: list[Target] = Field(default_factory=list, max_length=160)
    recipes: list[Recipe] = Field(default_factory=list, max_length=96)
    container: OpenContainer | None = None
    crafting_grid: Literal[0, 2, 3] = 2
    action: dict = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list, max_length=16)
    basis: Literal["game_observation"] = "game_observation"


class EnvironmentAction(Model):
    request_id: str = Field(default_factory=uid, pattern=r"^[0-9a-f]{32}$")
    observation_id: str = Field(max_length=80)
    kind: Literal["look", "move", "go_to", "mine", "eat", "select_slot", "place", "use", "attack", "stop",
                  "craft", "deposit", "withdraw", "close_container"]
    target_id: str = Field(default="", max_length=120)
    slot: int = Field(default=0, ge=0, le=35)
    yaw: float = Field(default=0, ge=-360, le=360)
    pitch: float = Field(default=0, ge=-90, le=90)
    forward: float = Field(default=0, ge=-1, le=1)
    strafe: float = Field(default=0, ge=-1, le=1)
    jump: bool = False
    sprint: bool = False
    ticks: int = Field(default=20, ge=1, le=200)
    recipe_id: str = Field(default="", max_length=160)
    container_id: int = Field(default=0, ge=0)
    container_slot: int = Field(default=0, ge=0, le=89)
    reason: str = Field(default="", max_length=300)


class PluginState(Model):
    active_id: str = ""
    autonomous: bool = False
    goal: str = Field(default="周囲を観察して少しずつできることを増やす。空腹や危険も大切にする。", max_length=1000)
    observation: EnvironmentObservation | None = None
    last_result: dict = Field(default_factory=dict)
    discoveries: dict[str, int] = Field(default_factory=dict, max_length=256)
    attempts: int = Field(default=0, ge=0)
    confirmed_changes: int = Field(default=0, ge=0)
    asked_contexts: dict[str, str] = Field(default_factory=dict, max_length=32)
    learning: LifeLearning = Field(default_factory=LifeLearning)


class EnvironmentClient:
    """The MOD owns physics; this client can only request its declared primitive actions."""

    def __init__(self, manifest, connection, client_factory=httpx.Client):
        self.manifest, self.connection, self.client_factory = manifest, connection, client_factory
        self.lease = ""

    def call(self, path, payload=None):
        headers = {"Authorization": "Bearer "+self.connection.token}
        if self.lease:
            headers["X-ESI-Lease"] = self.lease
        with self.client_factory(base_url=self.connection.endpoint, headers=headers,
                                 trust_env=False, timeout=8, follow_redirects=False) as client:
            with client.stream("POST" if payload is not None else "GET", path, json=payload) as response:
                if response.status_code in {401, 403}:
                    raise PermissionError("接続コードが違うか、接続が解除されています。Minecraftで /esi link をやり直してください。")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 256_000:
                        raise ValueError("プラグインからの状態が大きすぎます。")
                value = json.loads(raw)
                if response.status_code >= 400:
                    raise RuntimeError(str(value.get("error", "Minecraftとの接続でエラーが発生しました。"))[:500])
                return value

    def connect(self, character_id, name):
        data = self.call("/v1/connect", {"protocol": 1, "plugin_id": self.manifest.id,
                                         "character_id": character_id, "name": name[:60]})
        if data.get("protocol") != 1 or data.get("plugin_id") != self.manifest.id:
            raise ValueError("プラグインの接続方式が一致しません。")
        self.lease = data["lease"]
        try:
            return self.observe()
        except Exception:
            try:
                self.disconnect()
            except Exception:
                pass
            raise

    def observe(self):
        result = EnvironmentObservation.model_validate(self.call("/v1/state"))
        if result.plugin_id != self.manifest.id:
            raise ValueError("別のプラグインの観察は使えません。")
        return result

    def act(self, action):
        action = EnvironmentAction.model_validate(action)
        if action.kind not in self.manifest.actions:
            raise ValueError("このプラグインには、その操作がありません。")
        return self.call("/v1/action", action.model_dump(exclude={"reason"}))

    def stop(self):
        if self.lease:
            return self.call("/v1/stop", {})

    def disconnect(self):
        try:
            if self.lease:
                self.call("/v1/disconnect", {})
        finally:
            self.lease = ""
