"""Experience-backed beliefs with alternatives, confidence and auditable revisions."""

import json
import re

from esi.models import uid, utc
from esi02.knowledge import Knowledge
from esi02.memory import Candidate, normalize, validate_citation
from esi02.motivation import clip


class Beliefs(Knowledge):
    TABLE = "esi03_beliefs"
    PENDING = "esi03_beliefs_pending"
    BLOCKED = "esi03_beliefs_blocked"
    REVISION = "esi03_beliefs_revision"
    HISTORY_FLOOR = "esi03_beliefs_history_floor"

    def __init__(self, store, character):
        super().__init__(store)
        self.character = character
        with store.lock, store.db:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS esi03_belief_history (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def _history(self, item, reason, evidence_id):
        entry = {
            "id": uid(),
            "at": utc(),
            "claim_id": item["id"],
            "key": item["key"],
            "reason": reason,
            "evidence_id": evidence_id,
            "value": item["value"],
            "confidence": item["confidence"],
            "status": item["status"],
        }
        self.store.db.execute(
            "INSERT INTO esi03_belief_history VALUES (?,?)",
            (entry["id"], json.dumps(entry, ensure_ascii=False, allow_nan=False)),
        )

    def history(self, key=None, limit=100):
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT payload FROM esi03_belief_history ORDER BY rowid DESC LIMIT ?", (min(1000, limit),)
            )
            items = [json.loads(row[0]) for row in rows]
            return [item for item in items if key is None or item["key"] == key]

    def alternatives(self, key):
        return [
            item
            for item in self.entries(include_old=True)
            if item["key"] == key and item["status"] != "deleted"
        ]

    def apply(self, turn, candidates, extractor="explicit-pattern", expected_revision=None):
        candidates = [Candidate.model_validate(item) for item in candidates]
        if len(candidates) > 6 or any(item.kind != "knowledge" for item in candidates):
            raise ValueError("知識は一度に6件までです。")
        if turn["source"] not in {"text", "microphone", "recording", "confirmed_recognition", "migration"}:
            return False
        if turn.get("recognition_needs_confirmation") or not turn.get("use_in_dialogue_history", True):
            return False
        for item in candidates:
            validate_citation(turn, item)
        with self.store.lock, self.store.db:
            if expected_revision is not None and expected_revision != self.revision:
                return False
            if (
                turn["epoch"] <= self.history_floor
                or self.store.db.execute(
                    f"SELECT 1 FROM {self.BLOCKED} WHERE turn_id=?", (turn["id"],)
                ).fetchone()
            ):
                return False
            changed = False
            for item in candidates:
                evidence = {
                    "id": turn["id"],
                    "source": turn["source"],
                    "quote": item.quote,
                    "at": turn["created_at"],
                    "basis": item.basis,
                    "url": None,
                }
                correction = bool(
                    re.search(r"訂正|間違|ではなく|じゃなく|正しくは|違って|違います", turn["input"])
                )
                changed |= self._observe(item, evidence, turn["epoch"], extractor, correction)
            if changed:
                self._revision()
            return True

    def _observe(self, item, evidence, epoch, extractor, correction=False, source_weight=None):
        all_items = self.entries(include_old=True)
        alternatives = [m for m in all_items if m["key"] == item.key and m["status"] != "deleted"]
        if any(m["epoch"] > epoch for m in alternatives):
            return False
        if item.basis == "inferred" and any(m["trust"] != "inferred" for m in alternatives):
            return False
        if any(evidence["id"] == source["id"] for m in alternatives for source in m.get("evidence", [])):
            return False
        traits = self.character.traits
        preferred = next((m for m in alternatives if m["status"] == "active"), None)
        same = next((m for m in alternatives if normalize(m["value"]) == normalize(item.value)), None)
        weight = source_weight if source_weight is not None else 0.35 + 0.5 * traits.trust
        if item.basis == "inferred":
            weight *= 0.5
        reconsider = clip(
            0.3 + 0.35 * traits.trust + 0.25 * traits.flexibility - 0.25 * traits.stubbornness, 0.08, 0.85
        )
        if correction:
            reconsider = clip(reconsider + 0.2, 0.08, 0.95)
        conflict = preferred is not None and normalize(preferred["value"]) != normalize(item.value)
        if conflict:
            preferred["confidence"] = clip(preferred["confidence"] * (1 - reconsider * weight), 0.05, 0.95)
            preferred["conflicted"] = True
            preferred["updated_at"] = utc()
            self._put(preferred)
            self._history(preferred, "新しい教えと食い違い、以前の確信を再考", evidence["id"])
        if same:
            claim = same
            claim["confidence"] = clip(
                claim["confidence"] + weight * (1 - claim["confidence"]) * 0.5, 0.05, 0.95
            )
            claim["evidence"] = (claim.get("evidence", []) + [evidence])[-32:]
            claim["epoch"] = epoch
            claim["source_turn_id"] = evidence["id"]
            claim["updated_at"] = utc()
        else:
            if len(all_items) >= self.CAPACITY:
                raise ValueError("信念が1000件に達しました。不要な記録を整理してください。")
            claim = {
                "id": uid(),
                "kind": "knowledge",
                "key": item.key,
                "label": item.label,
                "value": item.value,
                "trust": "inferred"
                if item.basis == "inferred" or evidence["source"] in {"web", "file"}
                else "user_reported",
                "quote": item.quote,
                "source_turn_id": evidence["id"],
                "source": evidence["source"],
                "epoch": epoch,
                "created_at": evidence["at"],
                "updated_at": utc(),
                "extractor": extractor,
                "status": "superseded",
                "supersedes": preferred["id"] if preferred else None,
                "confidence": clip(weight if not conflict else weight * (0.5 + 0.5 * reconsider), 0.05, 0.95),
                "evidence": [evidence],
                "conflicted": conflict,
                "edited_in_ui": False,
            }
            alternatives.append(claim)
        # Doubt is observable behavior. A stubborn character can retain the old belief
        # while keeping the correction as an alternative and asking for clarification.
        threshold = 0.08 * traits.stubbornness
        winner = (
            claim
            if not preferred
            or claim["id"] == preferred["id"]
            or claim["confidence"] >= preferred["confidence"] + threshold
            else preferred
        )
        for candidate in alternatives:
            candidate["status"] = "active" if candidate["id"] == winner["id"] else "superseded"
            if candidate["id"] == winner["id"]:
                candidate["conflicted"] = (
                    conflict and abs(claim["confidence"] - (preferred or claim)["confidence"]) < 0.25
                )
            self._put(candidate)
        self._put(claim)
        self._history(claim, "訂正を記録" if correction else "観測・教えを記録", evidence["id"])
        if conflict:
            self._history(winner, "現在の信念を選択（代替説は保持）", evidence["id"])
        return True

    def from_document(self, evidence, candidates, epoch):
        """Claims must be literal spans in an actually retrieved document."""
        if evidence.get("kind") not in {"web", "file"} or not evidence.get("id") or not evidence.get("text"):
            raise ValueError("取得した資料と出典が必要です。")
        validated = [Candidate.model_validate(item) for item in candidates]
        if len(validated) > 6:
            raise ValueError("一つの資料からの知識は6件までです。")
        source_turn = {"id": evidence["id"], "input": evidence["text"], "source": "text", "epoch": epoch}
        for item in validated:
            if item.kind != "knowledge":
                raise ValueError("資料からは知識のみ記録します。")
            validate_citation(source_turn, item)
        with self.store.lock, self.store.db:
            changed = False
            for item in validated:
                source = {
                    "id": evidence["id"],
                    "source": evidence["kind"],
                    "quote": item.quote,
                    "at": evidence["retrieved_at"],
                    "basis": "inferred",
                    "url": evidence.get("url"),
                    "path": evidence.get("path"),
                    "sha256": evidence.get("sha256"),
                }
                changed |= self._observe(item, source, epoch, "quoted-document", source_weight=0.45)
            if changed:
                self._revision()
            return changed

    def context(self, query, limit=8, char_limit=2400):
        result = super().context(query, limit=limit, char_limit=char_limit)
        available = {item["id"]: item for item in self.entries()}
        for item in result:
            stored = available[item["id"]]
            item.update(
                confidence=stored["confidence"],
                conflicted=stored.get("conflicted", False),
                quote=stored["quote"],
                evidence=stored.get("evidence", [])[-2:],
            )
            if stored.get("conflicted"):
                item["alternatives"] = [
                    {
                        "id": alternative["id"],
                        "value": alternative["value"],
                        "confidence": alternative["confidence"],
                    }
                    for alternative in self.alternatives(item["key"])
                    if alternative["id"] != item["id"]
                ][-3:]
        return result

    def edit(self, memory_id, value, epoch, trust="confirmed", resolved=False):
        # Explicit UI edits resolve the current choice, but retain a revision history.
        old = next((item for item in self.entries() if item["id"] == memory_id), None)
        if old is None:
            raise ValueError("信念はすでに更新されています。")
        with self.store.lock, self.store.db:
            self._history(old, "画面から編集する前の内容", "ui:" + uid())
        super().edit(memory_id, value, epoch, trust, resolved)
        with self.store.lock, self.store.db:
            item = next(item for item in self.entries() if item["id"] == memory_id)
            item.update(confidence=0.9 if trust == "confirmed" else 0.45, conflicted=False)
            self._put(item)
            self._history(item, "画面から明示的に編集", "ui:" + uid())

    def add_manual(self, kind, label, value, epoch):
        before = {item["id"] for item in self.entries()}
        result = super().add_manual(kind, label, value, epoch)
        with self.store.lock, self.store.db:
            for item in self.entries():
                if item["id"] not in before:
                    item.update(confidence=0.9, evidence=[], conflicted=False)
                    self._put(item)
                    self._history(item, "画面から新しい知識を記録", "ui:" + uid())
        return result

    def delete(self, memory_id, epoch):
        old = next((item for item in self.entries() if item["id"] == memory_id), None)
        if old is None:
            raise ValueError("信念はすでに更新されています。")
        super().delete(memory_id, epoch)
        with self.store.lock, self.store.db:
            # Delete alternatives too: a deleted belief must not resurrect through an old source.
            for item in self.entries(include_old=True):
                if item["key"] == old["key"]:
                    item["status"] = "deleted"
                    self._put(item)
                    self._history(item, "本人が知識を削除", "ui:" + uid())
