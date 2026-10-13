"""Conversation persistence; ESI-01 databases are never opened for writing."""

import copy
import json
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

from esi.models import uid, utc
from esi.storage import Store
from esi02.config import Settings

TERMINAL = {"complete", "cancelled", "failed"}


def inspect_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='esi02_settings'").fetchone()
        if row is None:
            raise ValueError("ESI-02のセーブではありません。ESI-01の元セーブは保持しました。")
        settings = Settings.model_validate(json.loads(row[0]))
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("セーブの整合性を確認できません。")
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
        self.status = {"running": False, "phase": "stopped", "microphone": "stopped"}
        self.agent_id = self.store.meta("agent_id")
        try:
            saved = self.store.load_snapshot()
            if saved:
                state = saved["state"]
                if state["release"] != "ESI-02":
                    raise ValueError("ESI-02の保存状態ではありません。")
                self.elapsed, self.epoch = state["elapsed"], state["epoch"]
                self.turns = OrderedDict((t["id"], t) for t in state["turns"])
                # Recovery is from durable turn events, including uncheckpointed commands.
                for record in self.store.records(after=saved["seq"], limit=self.settings.max_records + 1):
                    if record["kind"] == "turn_state":
                        turn = record["payload"]["turn"]
                        self.turns[turn["id"]] = turn
                        self.epoch = max(self.epoch, turn["epoch"])
                for turn in self.turns.values():
                    if turn["stage"] not in TERMINAL:
                        turn["stage"] = "cancelled"
                        turn["cancellation"] = "restart: interrupted work is not resumed automatically"
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

    def create_turn(self, text, source="text", observation_id=None):
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
            self._prune()
            return copy.deepcopy(turn)

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
            for turn in self.turns.values():
                if turn["id"] == before_id:
                    break
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

    def checkpoint(self, initial=False):
        with self.lock:
            return self.store.snapshot(
                {
                    "release": "ESI-02",
                    "elapsed": self.elapsed,
                    "epoch": self.epoch,
                    "turns": list(self.turns.values()),
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
                }
            )

    def close(self):
        self.checkpoint()
        self.store.close()
