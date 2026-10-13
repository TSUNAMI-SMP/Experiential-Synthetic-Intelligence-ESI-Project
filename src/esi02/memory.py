"""Bounded, cited dialogue memory, independent of compactable conversation events."""

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from esi.models import uid, utc

LABELS = {
    "name": "名前",
    "nickname": "呼び名",
    "residence": "住んでいる場所",
    "occupation": "仕事",
    "age": "年齢",
}
TRUST = {
    "user_reported": "本人の発言・自動整理",
    "confirmed": "本人が画面で確認",
    "inferred": "AIの推測・未確認",
}


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def grams(text):
    text = re.sub(r"\s+", "", normalize(text))
    return {text[i : i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["fact", "preference", "topic"]
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=500)
    basis: Literal["explicit", "inferred"] = "explicit"
    resolves_id: str | None = Field(default=None, max_length=64)


def explicit_candidates(text):
    """Only unambiguous self reports get an immediate fast path. Other speech uses the model."""
    if any(
        word in text
        for word in (
            "たとえば",
            "例えば",
            "もし",
            "仮に",
            "という文",
            "かもしれ",
            "たぶん",
            "多分",
            "？",
            "?",
            "「",
            "『",
            '"',
        )
    ):
        return []
    result = []
    for field, pattern in [
        (
            "name",
            r"(?:私|僕|ぼく|わたし|俺)の名前は[^。\n、]{1,50}?(?:ではなく|じゃなく)(?P<value>[^。\n、]{1,50}?)(?:です|だよ|だ)(?:[。！!]|$)",
        ),
        ("name", r"(?:私|僕|ぼく|わたし|俺)の名前は(?P<value>[^。\n、]{1,50}?)(?:です|だよ|だ)(?:[。！!]|$)"),
        (
            "nickname",
            r"(?:私|僕|ぼく|わたし|俺)を(?P<value>[^。\n、]{1,50}?)と呼んで(?:ください|ね)?(?:[。！!]|$)",
        ),
        (
            "residence",
            r"(?:私|僕|ぼく|わたし|俺)は(?P<value>[^。\n、]{1,50}?)に住んで(?:います|いる)(?:[。！!]|$)",
        ),
    ]:
        match = re.search(pattern, text)
        if match and not any(v in match["value"] for v in ("ではなく", "じゃなく", "だった", "ない")):
            result.append(
                Candidate(kind="fact", key=field, label=LABELS[field], value=match["value"], quote=match[0])
            )
    match = re.search(
        r"(?:私|僕|ぼく|わたし|俺)は(?P<object>[^。\n、]{1,50}?)が(?P<value>好き|苦手|嫌い)(?:です|だよ|だ)(?:[。！!]|$)",
        text,
    )
    if match and not any(v in match["object"] for v in ("ではなく", "じゃなく", "ない")):
        result.append(
            Candidate(
                kind="preference",
                key="preference:" + normalize(match["object"]),
                label=match["object"],
                value=match["value"],
                quote=match[0],
            )
        )
    return result


def validate_citation(turn, item):
    if item.quote not in turn["input"] or item.value not in item.quote:
        raise ValueError("本人の発言にない内容を記憶として登録できません。")
    if item.kind == "fact" and item.key not in LABELS and not item.key.startswith("fact:"):
        raise ValueError("事実の項目名が正しくありません。")
    if item.kind == "preference" and (not item.key.startswith("preference:") or item.label not in item.quote):
        raise ValueError("好みの対象を本人の発言で確認できません。")
    if item.kind == "topic" and not item.key.startswith("topic:"):
        raise ValueError("話題の項目が正しくありません。")
    markers = {
        "name": r"名前|なまえ|名まえ|名は",
        "nickname": r"呼んで|呼び名|あだ名|ニックネーム",
        "residence": r"住|暮ら|居住",
        "occupation": r"仕事|職業|勤務|働|会社員|学生|エンジニア|教師|医師|師です",
        "age": r"歳|才|年齢",
    }
    if (
        item.basis == "explicit"
        and item.kind == "fact"
        and item.key in markers
        and not re.search(markers[item.key], item.quote)
    ):
        raise ValueError("この項目を本人が明言したと確認できません。")


class Memory:
    CAPACITY = 1000
    PENDING_CAPACITY = 128

    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS esi02_memories (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS esi02_memory_pending (turn_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS esi02_memory_blocked (turn_id TEXT PRIMARY KEY);
            """)
        self.revision = store.meta("esi02_memory_revision") or 0
        self.history_floor = store.meta("esi02_history_floor") or 0

    def entries(self, include_old=False):
        with self.store.lock:
            rows = self.store.db.execute("SELECT payload FROM esi02_memories ORDER BY rowid DESC")
            items = [json.loads(r[0]) for r in rows]
            return items if include_old else [m for m in items if m["status"] == "active"]

    def _put(self, entry):
        self.store.db.execute(
            "INSERT OR REPLACE INTO esi02_memories VALUES (?,?)",
            (entry["id"], json.dumps(entry, ensure_ascii=False)),
        )

    def _revision(self, history_floor=None):
        self.revision += 1
        updates = [("esi02_memory_revision", self.revision)]
        if history_floor is not None:
            self.history_floor = max(self.history_floor, history_floor)
            updates.append(("esi02_history_floor", self.history_floor))
            for item in self.pending():
                if item["epoch"] <= self.history_floor:
                    self.store.db.execute("DELETE FROM esi02_memory_pending WHERE turn_id=?", (item["id"],))
        # Stay in the caller's transaction; Store.set_meta would commit an inner context early.
        self.store.db.executemany(
            "INSERT OR REPLACE INTO metadata VALUES (?,?)",
            [(key, json.dumps(value)) for key, value in updates],
        )

    def queue(self, turn):
        if not turn["input"].strip() or turn["epoch"] <= self.history_floor:
            return
        with self.store.lock, self.store.db:
            if self.store.db.execute(
                "SELECT 1 FROM esi02_memory_blocked WHERE turn_id=?", (turn["id"],)
            ).fetchone():
                return
            count = self.store.db.execute("SELECT count(*) FROM esi02_memory_pending").fetchone()[0]
            if count >= self.PENDING_CAPACITY:
                raise ValueError(
                    "記憶の整理待ちが128件あります。会話は保存されています。記憶画面で再整理できます。"
                )
            request = {
                "id": turn["id"],
                "input": turn["input"],
                "source": turn["source"],
                "epoch": turn["epoch"],
                "created_at": turn["created_at"],
                "attempts": 0,
                "error": None,
            }
            self.store.db.execute(
                "INSERT OR IGNORE INTO esi02_memory_pending VALUES (?,?)",
                (turn["id"], json.dumps(request, ensure_ascii=False)),
            )

    def pending(self, eligible_only=False):
        with self.store.lock:
            rows = self.store.db.execute("SELECT payload FROM esi02_memory_pending ORDER BY rowid DESC")
            items = [json.loads(r[0]) for r in rows]
            return [m for m in items if m["attempts"] < 2] if eligible_only else items

    def finish(self, turn_id, error=None):
        with self.store.lock, self.store.db:
            if error is None:
                self.store.db.execute("DELETE FROM esi02_memory_pending WHERE turn_id=?", (turn_id,))
            else:
                row = self.store.db.execute(
                    "SELECT payload FROM esi02_memory_pending WHERE turn_id=?", (turn_id,)
                ).fetchone()
                if row:
                    data = json.loads(row[0])
                    data.update(attempts=data["attempts"] + 1, error=str(error)[:300])
                    self.store.db.execute(
                        "UPDATE esi02_memory_pending SET payload=? WHERE turn_id=?",
                        (json.dumps(data, ensure_ascii=False), turn_id),
                    )

    def retry(self):
        with self.store.lock, self.store.db:
            for item in self.pending():
                item.update(attempts=0, error=None)
                self.store.db.execute(
                    "UPDATE esi02_memory_pending SET payload=? WHERE turn_id=?",
                    (json.dumps(item, ensure_ascii=False), item["id"]),
                )

    def apply(self, turn, candidates, extractor="explicit-pattern", expected_revision=None):
        """Validate every citation before an atomic update; old turns cannot reverse a correction."""
        candidates = [Candidate.model_validate(c) for c in candidates]
        if turn["source"] in {"simulated", "self_output", "recall", "research_feedback"}:
            candidates = [c.model_copy(update={"basis": "inferred"}) for c in candidates]
        if len(candidates) > 6:
            raise ValueError("一回の記憶整理は6項目以内です。")
        for item in candidates:
            validate_citation(turn, item)
        with self.store.lock, self.store.db:
            if expected_revision is not None and self.revision != expected_revision:
                return False
            if turn["epoch"] <= self.history_floor:
                return False
            if self.store.db.execute(
                "SELECT 1 FROM esi02_memory_blocked WHERE turn_id=?", (turn["id"],)
            ).fetchone():
                return False
            all_items = self.entries(include_old=True)
            added = False
            for item in candidates:
                old = next((m for m in all_items if m["key"] == item.key and m["status"] == "active"), None)
                if old and (
                    old["epoch"] > turn["epoch"]
                    or (old["trust"] == "confirmed" and old["epoch"] >= turn["epoch"])
                    or (old["trust"] != "inferred" and item.basis == "inferred")
                ):
                    continue
                if item.resolves_id:
                    target = next(
                        (
                            m
                            for m in all_items
                            if m["id"] == item.resolves_id
                            and m["kind"] == "topic"
                            and m["status"] == "active"
                        ),
                        None,
                    )
                    if target and item.basis == "explicit" and target["epoch"] <= turn["epoch"]:
                        target.update(
                            status="resolved",
                            resolution_quote=item.quote,
                            resolved_by=turn["id"],
                            updated_at=utc(),
                        )
                        self._put(target)
                        added = True
                    continue
                if (
                    old
                    and old["value"] == item.value
                    and old["trust"] == ("user_reported" if item.basis == "explicit" else "inferred")
                ):
                    continue
                if len(all_items) >= self.CAPACITY:
                    raise ValueError("記憶が1000件に達しました。記憶画面で不要なものを削除してください。")
                entry = {
                    "id": uid(),
                    "kind": item.kind,
                    "key": item.key,
                    "label": item.label,
                    "value": item.value,
                    "trust": "user_reported" if item.basis == "explicit" else "inferred",
                    "quote": item.quote,
                    "source_turn_id": turn["id"],
                    "source": turn["source"],
                    "epoch": turn["epoch"],
                    "created_at": turn["created_at"],
                    "updated_at": utc(),
                    "extractor": extractor,
                    "status": "active",
                    "supersedes": old["id"] if old else None,
                }
                if old:
                    old.update(status="superseded", updated_at=utc())
                    self._put(old)
                self._put(entry)
                all_items.insert(0, entry)
                added = True
            if added:
                self._revision()
            return True

    def edit(self, memory_id, value, epoch, trust="confirmed", resolved=False):
        value = value.strip()
        if not value or len(value) > 120 or trust not in TRUST:
            raise ValueError("記憶の内容は1〜120文字、確認状態は一覧から選んでください。")
        with self.store.lock, self.store.db:
            entry = next((m for m in self.entries() if m["id"] == memory_id), None)
            if not entry:
                raise ValueError("この記憶はすでに更新・削除されています。一覧を更新してください。")
            # Retain the original quote as provenance, not as evidence for the edited value.
            entry.update(
                value=value,
                trust=trust,
                epoch=epoch,
                updated_at=utc(),
                edited_in_ui=True,
                status="resolved" if resolved and entry["kind"] == "topic" else "active",
            )
            self._put(entry)
            self._revision(history_floor=epoch)

    def add_manual(self, kind, label, value, epoch):
        label, value = label.strip(), value.strip()
        if (
            kind not in {"fact", "preference", "topic"}
            or not label
            or len(label) > 60
            or not value
            or len(value) > 120
        ):
            raise ValueError("項目は1〜60文字、内容は1〜120文字で入力してください。")
        key = next((k for k, v in LABELS.items() if label == v), "manual:" + normalize(label))
        if kind in {"preference", "topic"}:
            key = kind + ":" + normalize(label)
        with self.store.lock, self.store.db:
            items = self.entries(include_old=True)
            old = next((m for m in items if m["key"] == key and m["status"] == "active"), None)
            if len(items) >= self.CAPACITY:
                raise ValueError("記憶の上限です。不要なものを削除してください。")
            entry = {
                "id": uid(),
                "kind": kind,
                "key": key,
                "label": label,
                "value": value,
                "trust": "confirmed",
                "quote": "",
                "source_turn_id": None,
                "source": "memory-editor",
                "epoch": epoch,
                "created_at": utc(),
                "updated_at": utc(),
                "extractor": "manual",
                "status": "active",
                "supersedes": old["id"] if old else None,
            }
            if old:
                old["status"] = "superseded"
                self._put(old)
            self._put(entry)
            self._revision(history_floor=epoch)
            return entry["id"]

    def delete(self, memory_id, epoch):
        with self.store.lock, self.store.db:
            items = self.entries(include_old=True)
            entry = next((m for m in items if m["id"] == memory_id), None)
            if not entry:
                raise ValueError("この記憶はすでに削除されています。")
            # Remove previous versions of the same field, and block deferred extraction of their sources.
            for item in items:
                if item["key"] == entry["key"]:
                    self.store.db.execute("DELETE FROM esi02_memories WHERE id=?", (item["id"],))
                    if item["source_turn_id"]:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO esi02_memory_blocked VALUES (?)", (item["source_turn_id"],)
                        )
                        self.store.db.execute(
                            "DELETE FROM esi02_memory_pending WHERE turn_id=?", (item["source_turn_id"],)
                        )
            self._revision(history_floor=epoch)

    def search(self, query="", include_old=False, limit=100):
        items = self.entries(include_old)
        if not query.strip():
            return items[:limit]
        query_grams = grams(query)

        def score(item):
            text = item["label"] + " " + item["value"] + " " + item["quote"]
            return len(query_grams & grams(text)) + (8 if normalize(query) in normalize(text) else 0)

        ranked = [(score(item), item) for item in items]
        ranked.sort(key=lambda pair: (pair[0], pair[1]["epoch"]), reverse=True)
        return [m for s, m in ranked if s > 0][:limit]

    def context(self, query, limit=8, char_limit=1400):
        relevant = self.search(query, limit=limit)
        relevant_ids = {m["id"] for m in relevant}
        # Identity/calling-name remains useful outside the recent conversational window.
        base = sorted(
            self.entries(), key=lambda m: (m["key"] in {"name", "nickname"}, m["epoch"]), reverse=True
        )
        selected = relevant + [m for m in base if m["id"] not in relevant_ids and m["trust"] != "inferred"]
        result, size = [], 0
        for item in selected:
            data = {
                "id": item["id"],
                "kind": item["kind"],
                "label": item["label"],
                "value": item["value"],
                "trust": TRUST[item["trust"]],
                "quote": item["quote"][:160],
                "source": item["source"],
                "edited_in_ui": item.get("edited_in_ui", False),
            }
            length = len(json.dumps(data, ensure_ascii=False))
            if size + length > char_limit:
                continue
            result.append(data)
            size += length
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def topic_key(value):
        return "topic:" + hashlib.sha256(normalize(value).encode()).hexdigest()[:16]
