"""Conversation persistence; ESI-01 databases are never opened for writing."""

import copy
import json
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from esi.models import uid, utc
from esi.storage import Store
from esi02.config import Settings
from esi02.memory import Memory, explicit_candidates
from esi02.world import World

TERMINAL = {"complete", "cancelled", "failed"}


class SavedTurn(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False, strict=True)
    id: str = Field(min_length=1, max_length=64)
    epoch: int = Field(ge=1)
    input: str = Field(max_length=2000)
    source: str = Field(max_length=100)
    created_at: str
    started_elapsed: float = Field(ge=0)
    stage: Literal["queued", "generating", "synthesizing", "playing", "complete", "cancelled", "failed"]
    reply: str = Field(max_length=2048)
    chunks: list[dict] = Field(max_length=32)
    metrics: dict[str, float | None]
    error: str | None


class SavedState(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False, strict=True)
    release: Literal["ESI-02"]
    elapsed: float = Field(ge=0)
    epoch: int = Field(ge=0)
    turns: list[SavedTurn] = Field(max_length=512)


def validate_state(state):
    value = SavedState.model_validate(state)
    ids = [t.id for t in value.turns]
    if len(ids) != len(set(ids)) or any(t.epoch > value.epoch for t in value.turns):
        raise ValueError("セーブの会話番号やIDが整合していません。")
    if state.get("world") is not None:
        World.model_validate(state["world"])


def inspect_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='esi02_settings'").fetchone()
        if row is None:
            raise ValueError("ESI-02のセーブではありません。ESI-01の元セーブは保持しました。")
        settings = Settings.model_validate(json.loads(row[0]))
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("セーブの整合性を確認できません。")
        snapshot = db.execute("SELECT seq,payload FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
        if not snapshot:
            raise ValueError("ESI-02の保存状態がありません。")
        validate_state(json.loads(snapshot[1]))
        for (payload,) in db.execute(
            "SELECT payload FROM records WHERE seq>? AND kind='turn_state' ORDER BY seq LIMIT ?",
            (snapshot[0], settings.max_records + 1),
        ):
            SavedTurn.model_validate(json.loads(payload)["turn"])
        return settings
    finally:
        db.close()


class Session:
    def __init__(self, path, settings=None):
        path = Path(path)
        stored = inspect_database(path) if path.exists() else None
        self.settings = settings or stored or Settings()
        if stored and stored.fingerprint != self.settings.fingerprint:
            raise ValueError("保存設定と異なります。新しい実験用のコピーを作ってから変更してください。")
        self.store = Store(path, max_records=self.settings.max_records)
        self.lock = threading.RLock()
        self.elapsed, self.epoch = 0.0, 0
        self.turns = OrderedDict()
        self.world = World()
        self.status = {"running": False, "phase": "stopped", "microphone": "stopped"}
        self.agent_id = self.store.meta("agent_id")
        try:
            self.memory = Memory(self.store)
            saved = self.store.load_snapshot()
            if saved:
                state = saved["state"]
                if state["release"] != "ESI-02":
                    raise ValueError("ESI-02の保存状態ではありません。")
                self.elapsed, self.epoch = state["elapsed"], state["epoch"]
                self.turns = OrderedDict((t["id"], t) for t in state["turns"])
                self.world = World.model_validate(state.get("world", {}))
                # Recovery is from durable turn events, including uncheckpointed commands.
                for record in self.store.records(after=saved["seq"], limit=self.settings.max_records + 1):
                    if record["kind"] == "turn_state":
                        turn = record["payload"]["turn"]
                        self.turns[turn["id"]] = turn
                        self.epoch = max(self.epoch, turn["epoch"])
                        self.elapsed = max(self.elapsed, record["payload"].get("elapsed", self.elapsed))
                    elif record["kind"] == "world_step":
                        self.world.conversation_active = record["payload"]["conversation_active"]
                        self.world.advance(record["payload"]["seconds"])
                        self.elapsed = max(self.elapsed, record["payload"]["elapsed"])
                    elif record["kind"] == "world_operation":
                        self._world_operation(record["payload"]["operation"], record["payload"]["values"])
                self.world.conversation_active = False
                for turn in self.turns.values():
                    if turn["stage"] not in TERMINAL:
                        turn["stage"] = "cancelled"
                        turn["cancellation"] = "restart: interrupted work is not resumed automatically"
                # Recover the small window between a durable turn and its memory queue insertion.
                for turn in self.turns.values():
                    if not turn.get("memory_queued"):
                        self._remember(turn)
            else:
                self.store.set_meta("esi02_settings", self.settings.model_dump())
            self._prune()
            self.checkpoint(initial=not saved)
        except Exception:
            self.store.close()
            raise

    def _prune(self):
        while len(self.turns) > self.settings.retained_turns:
            key = next(iter(self.turns))
            if self.turns[key]["stage"] not in TERMINAL:
                raise RuntimeError("未完了の会話が保存上限を超えました。")
            self.turns.pop(key)

    def create_turn(self, text, source="text", observation_id=None, remember=True):
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("入力は2000文字以内にしてください。")
        with self.lock:
            self.epoch += 1
            turn = {
                "id": uid(),
                "epoch": self.epoch,
                "input": text,
                "source": source,
                "observation_id": observation_id,
                "created_at": utc(),
                "started_elapsed": self.elapsed,
                "stage": "queued",
                "reply": "",
                "chunks": [],
                "metrics": {},
                "error": None,
            }
            self.turns[turn["id"]] = turn
            self._persist(turn)
            if remember:
                self._remember(turn)
            else:
                turn["memory_queued"] = True
                turn["use_in_dialogue_history"] = False
            self._persist(turn)
            self._prune()
            return copy.deepcopy(turn)

    def _remember(self, turn):
        try:
            self.memory.apply(turn, explicit_candidates(turn["input"]))
            self.memory.queue(turn)
            turn["memory_queued"] = True
        except ValueError as exc:
            # Memory capacity/errors never prevent the ordinary conversation response.
            self.status["memory_error"] = str(exc)

    def _persist(self, turn):
        self.store.append("turn_state", {"turn": copy.deepcopy(turn), "elapsed": self.elapsed})

    def update_turn(self, turn_id, **changes):
        with self.lock:
            turn = self.turns[turn_id]
            turn.update(changes)
            self._persist(turn)

    def add_chunk(self, turn_id, chunk):
        with self.lock:
            self.turns[turn_id]["chunks"].append(copy.deepcopy(chunk))
            self._persist(self.turns[turn_id])

    def history(self, before_id):
        with self.lock:
            history = []
            superseded = {
                m["source_turn_id"]
                for m in self.memory.entries(include_old=True)
                if m["status"] == "superseded"
            }
            for turn in self.turns.values():
                if turn["id"] == before_id:
                    break
                if turn["epoch"] <= self.memory.history_floor:
                    continue
                if turn["id"] in superseded:
                    continue
                if not turn.get("use_in_dialogue_history", True):
                    continue
                history.append({"role": "user", "content": turn["input"] or "（音声を聞き取れなかった）"})
                if turn["reply"] and turn["stage"] == "complete":
                    history.append({"role": "assistant", "content": turn["reply"]})
                elif turn["chunks"]:
                    delivered = "".join(c["text"] for c in turn["chunks"] if c.get("played"))
                    if delivered:
                        history.append(
                            {"role": "assistant", "content": delivered + "（この発声は中断された）"}
                        )
            return history[-self.settings.history_pairs * 2 :]

    def memory_operation(self, operation, data=None):
        data = data or {}
        with self.lock:
            if operation == "edit":
                self.memory.edit(
                    data["id"],
                    data["value"],
                    self.epoch,
                    data.get("trust", "confirmed"),
                    data.get("resolved", False),
                )
            elif operation == "add":
                self.memory.add_manual(data["kind"], data["label"], data["value"], self.epoch)
            elif operation == "delete":
                self.memory.delete(data["id"], self.epoch)
            elif operation == "retry":
                self.memory.retry()
            else:
                raise ValueError("記憶の操作が正しくありません。")
            self.status["memory_error"] = ""
            self.status["notice"] = "記憶を更新して保存しました。"

    def advance_world(self, seconds, conversing=False):
        with self.lock:
            self.world.conversation_active = bool(conversing)
            self.world.advance(seconds)
            self.store.append(
                "world_step",
                {"seconds": seconds, "conversation_active": bool(conversing), "elapsed": self.elapsed},
            )

    def _world_operation(self, operation, values):
        if operation == "environment":
            self.world.change_environment(values)
        elif operation == "place":
            self.world.go_to(values)
        elif operation == "move":
            self.world.move_to(values)
        elif operation == "activity":
            self.world.set_activity(values)
        elif operation == "drink":
            self.world.drink()
        else:
            raise ValueError("部屋の操作が正しくありません。")

    def world_operation(self, operation, values=None):
        with self.lock:
            self._world_operation(operation, values)
            self.store.append("world_operation", {"operation": operation, "values": values})

    def dialogue_context(self, query):
        with self.lock:
            return {"memories": self.memory.context(query), "virtual_world": self.world.context()}

    def checkpoint(self, initial=False):
        with self.lock:
            return self.store.snapshot(
                {
                    "release": "ESI-02",
                    "elapsed": self.elapsed,
                    "epoch": self.epoch,
                    "turns": list(self.turns.values()),
                    "world": self.world.model_dump(),
                },
                initial=initial,
            )

    def view(self):
        with self.lock:
            return copy.deepcopy(
                {
                    "release": "ESI-02",
                    "agent_id": self.agent_id,
                    "elapsed": self.elapsed,
                    "status": self.status,
                    "turns": list(self.turns.values())[-12:],
                    "memory": {
                        "count": len(self.memory.entries()),
                        "pending": len(self.memory.pending()),
                        "failed": sum(m["attempts"] >= 2 for m in self.memory.pending()),
                        "revision": self.memory.revision,
                    },
                    "world": self.world.context(),
                }
            )

    def close(self):
        self.checkpoint()
        self.store.close()
