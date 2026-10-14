"""Versioned save owner. Live state and its event commit together, before external effects."""

import copy
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from esi.models import uid
from esi.storage import Store
from esi02.knowledge import teaching_candidates
from esi02.memory import Memory, explicit_candidates
from esi02.motivation import State as MotivationState
from esi02.session import TERMINAL
from esi02.session import SavedState as PreviousSavedState
from esi02.session import Session as PreviousSession
from esi02.world import World
from esi03.beliefs import Beliefs
from esi03.config import Settings
from esi03.experience import Experience
from esi03.motivation import Brain
from esi03.personality import Character, Preferences
from esi03.personality import State as ExperienceState


class SavedState(PreviousSavedState):
    release: Literal["ESI-03"]
    schema_version: Literal[1] = 1
    world: World
    motivation: MotivationState
    experience: ExperienceState


def validate_state(state):
    value = SavedState.model_validate(state)
    identifiers = [turn.id for turn in value.turns]
    if len(set(identifiers)) != len(identifiers) or any(turn.epoch > value.epoch for turn in value.turns):
        raise ValueError("会話の保存番号が整合していません。")
    if abs(value.world.elapsed - value.experience.elapsed) > 1e-6:
        raise ValueError("身体と経験の時間が整合していません。")
    return value


def inspect_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("セーブの整合性を確認できません。")
        row = db.execute("SELECT value FROM metadata WHERE key='esi03_settings'").fetchone()
        if not row:
            raise ValueError("ESI-03のセーブではありません。旧セーブはコピー移行してください。")
        settings = Settings.model_validate(json.loads(row[0]))
        live = db.execute("SELECT value FROM metadata WHERE key='esi03_live'").fetchone()
        snapshot = db.execute("SELECT payload FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
        if not live or not snapshot:
            raise ValueError("ESI-03の保存状態がありません。")
        validate_state(json.loads(live[0]))
        validate_state(json.loads(snapshot[0]))
        return settings
    finally:
        db.close()


class Session(PreviousSession):
    def __init__(self, path, settings=None, character=None):
        path = Path(path)
        stored = inspect_database(path) if path.exists() else None
        self.settings = settings or stored or Settings()
        if stored and self.settings.fingerprint != stored.fingerprint:
            raise ValueError("保存済みの実行設定と異なります。設定画面から停止中に変更してください。")
        self.store = Store(path, max_records=self.settings.max_records)
        self.lock = threading.RLock()
        self.elapsed, self.epoch = 0.0, 0
        self.turns = OrderedDict()
        self.world = World()
        self.status = {"running": False, "phase": "stopped", "microphone": "stopped"}
        self.agent_id = self.store.meta("agent_id")
        self.experience = Experience()
        self.brain = Brain(self.agent_id, self.world, self.experience)
        self.tool_policy = None
        try:
            live = self.store.meta("esi03_live")
            if live:
                state = validate_state(live)
                self.elapsed, self.epoch = state.elapsed, state.epoch
                self.turns = OrderedDict((turn.id, turn.model_dump()) for turn in state.turns)
                self.world = state.world
                self.experience = Experience(state.experience.model_dump())
                self.brain = Brain(self.agent_id, self.world, self.experience, state.motivation.model_dump())
            elif character is not None:
                self.experience.state.character = Character.model_validate(character)
            self.memory = Memory(self.store)
            self.knowledge = Beliefs(self.store, self.experience.state.character)
            self.store.db.execute("CREATE TABLE IF NOT EXISTS esi03_outcomes (id TEXT PRIMARY KEY)")
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS esi03_evidence (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            self.store.db.commit()
            self.experience.seen_outcome = lambda identifier: (
                self.store.db.execute("SELECT 1 FROM esi03_outcomes WHERE id=?", (identifier,)).fetchone()
                is not None
            )
            self.world.conversation_active = False
            self.experience.recover()
            if self.brain.active and self.brain.active.kind.startswith("ask_"):
                self.brain.restart(self.world)
            for turn in self.turns.values():
                if turn["stage"] not in TERMINAL:
                    turn.update(stage="cancelled", cancellation="再起動前の未完了の発声は自動再開しません。")
                if not turn.get("memory_queued") and turn.get("use_in_dialogue_history", True):
                    self._remember(turn)
            self._prune()
            if not stored:
                self.store.set_meta("esi03_settings", self.settings.model_dump())
            self.checkpoint(initial=not live)
        except Exception:
            self.store.close()
            raise

    def dump(self):
        return {
            "release": "ESI-03",
            "schema_version": 1,
            "elapsed": self.elapsed,
            "epoch": self.epoch,
            "turns": list(self.turns.values()),
            "world": self.world.model_dump(),
            "motivation": self.brain.state.model_dump(),
            "experience": self.experience.state.model_dump(),
        }

    def _write(self, events=()):
        value = self.dump()
        validate_state(value)
        with self.store.lock, self.store.db:
            self.store.db.executemany(
                "INSERT OR IGNORE INTO esi03_outcomes VALUES (?)",
                [(identifier,) for identifier in self.experience.state.recent_outcomes],
            )
            self.store.db.executemany(
                "INSERT INTO records(kind,payload) VALUES (?,?)",
                [
                    (kind, json.dumps(payload, ensure_ascii=False, allow_nan=False))
                    for kind, payload in events
                ],
            )
            self.store.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('esi03_live',?)",
                (json.dumps(value, ensure_ascii=False, allow_nan=False),),
            )

    def _persist(self, turn):
        self._write([("turn_state", {"turn": copy.deepcopy(turn), "elapsed": self.elapsed})])

    def _record_motivation(self, events):
        self._write([(event["kind"], event["payload"]) for event in events])

    def create_turn(self, text, source="text", observation_id=None, remember=True):
        # Exclusion is established before any contact, reward or belief handling.
        if not remember:
            turn = super().create_turn(text, "unconfirmed_recognition", observation_id, remember=False)
            with self.lock:
                self.turns[turn["id"]]["source"] = source
                self._persist(self.turns[turn["id"]])
            return copy.deepcopy(self.turns[turn["id"]])
        return super().create_turn(text, source, observation_id, remember=True)

    def _remember(self, turn):
        try:
            before = {item["id"]: copy.deepcopy(item) for item in self.knowledge.entries(include_old=True)}
            self.memory.apply(turn, explicit_candidates(turn["input"]))
            self.knowledge.apply(turn, teaching_candidates(turn["input"]))
            self.experience.user_turn(turn)
            learned = [
                item
                for item in self.knowledge.entries(include_old=True)
                if item.get("source_turn_id") == turn["id"] and item != before.get(item["id"])
            ]
            self.experience.learned(
                learned, turn["id"], bool(re.search(r"訂正|ではなく|じゃなく|間違", turn["input"]))
            )
            for item in learned:
                preferred = next(
                    (claim for claim in self.knowledge.entries() if claim["key"] == item["key"]), item
                )
                if preferred.get("conflicted"):
                    self.experience.interest(
                        item["label"],
                        f"{item['label']}について、以前の教えと違うところをもう少し説明してもらえる？",
                        "以前に教わった内容と新しい説明が食い違う",
                        "conflict",
                        turn["id"],
                        0.9,
                    )
            self.memory.queue(turn)
            turn["memory_queued"] = True
        except ValueError as exc:
            self.status["memory_error"] = str(exc)

    def advance_world(self, seconds, conversing=False, autonomy=False, allow_requests=False):
        with self.lock:
            self.world.conversation_active = bool(conversing)
            self.world.advance(seconds)
            self.experience.advance(self.world, seconds)
            events = self.brain.tick(self.world, seconds, autonomy, allow_requests)
            self._write(
                [
                    (
                        "world_step",
                        {
                            "seconds": seconds,
                            "conversation_active": bool(conversing),
                            "elapsed": self.elapsed,
                            "autonomy": bool(autonomy),
                            "allow_requests": bool(allow_requests),
                        },
                    )
                ]
                + [(event["kind"], event["payload"]) for event in events]
            )

    def world_operation(self, operation, values=None):
        with self.lock:
            events = self._apply_world_command(operation, values)
            self.experience.advance(self.world, 0)
            self._write(
                [("world_operation", {"operation": operation, "values": values})]
                + [(event["kind"], event["payload"]) for event in events]
            )

    def motivation_operation(self, operation, values):
        with self.lock:
            events = self._motivation_operation(operation, values)
            self._write(
                [("motivation_command", {"operation": operation, "values": values})]
                + [(event["kind"], event["payload"]) for event in events]
            )

    def memory_operation(self, operation, data=None):
        with self.lock:
            super().memory_operation(operation, data)
            self.experience.trace(
                "memory_edit",
                "本人が保存内容を整理",
                "ui:" + uid(),
                operation=operation,
                scope=(data or {}).get("scope", "memory"),
            )
            self._write()

    def character_operation(self, operation, data):
        with self.lock:
            if operation == "preferences":
                value = Preferences.model_validate({**self.experience.state.preferences.model_dump(), **data})
                self.experience.state.preferences = value
            elif operation == "character":
                value = Character.model_validate(data)
                self.experience.state.character = value
                self.knowledge.character = value
            elif operation == "interest":
                if set(data) != {"topic", "question"}:
                    raise ValueError("関心の話題と疑問を指定してください。")
                self.experience.interest(
                    data["topic"], data["question"], "本人から関心の方向を提案", "user", "ui:" + uid(), 0.8
                )
            elif operation == "pause_interest":
                item = next((item for item in self.experience.state.interests if item.id == data), None)
                if item is None:
                    raise ValueError("関心が見つかりません。")
                item.stage = "paused"
                item.attempts = 3
                self.experience.pause_question("本人が関心を保留", "ui:" + uid())
            else:
                raise ValueError("キャラクターの操作が正しくありません。")
            self.experience.trace(
                "settings", "本人がキャラクター設定を変更", "ui:" + uid(), operation=operation
            )
            self._write([("character_operation", {"operation": operation, "data": data})])

    def dialogue_context(self, query):
        with self.lock:
            context = super().dialogue_context(query)
            character = self.experience.context()
            if (
                not self.experience.state.preferences.proactive
                or self.world.elapsed < self.experience.state.preferences.quiet_until
            ):
                character["interests"] = []
            context.update(knowledge_policy="experience_only_v2", character=character)
            return context

    def dialogue_result(self, turn_id, result):
        with self.lock:
            turn = self.turns[turn_id]
            if turn.get("experience_processed"):
                return
            if result.get("unknown_topic"):
                topic = result["unknown_topic"][:100]
                self.experience.interest(
                    topic,
                    f"{topic}について、まだ知らないので教えてもらえる？",
                    "対話で説明できないことに気づいた",
                    "unknown",
                    turn_id,
                    0.75,
                )
            if result.get("asked_interest"):
                identifier = result["asked_interest"]
                if any(item.id == identifier for item in self.experience.state.interests):
                    self.experience.asked(identifier, "chat:" + turn_id)
                    self.experience.finish_question("chat:" + turn_id, "unknown", turn_id)
            turn.update(experience_processed=True, grounding=result.get("grounding", {}))
            self._persist(turn)

    def checkpoint(self, initial=False):
        with self.lock:
            self._write()
            return self.store.snapshot(self.dump(), initial=initial)

    def view(self):
        with self.lock:
            view = super().view()
            view.update(
                release="ESI-03",
                character=self.experience.state.model_dump(),
                beliefs={
                    "count": len(self.knowledge.entries()),
                    "conflicts": sum(bool(item.get("conflicted")) for item in self.knowledge.entries()),
                },
            )
            return view
