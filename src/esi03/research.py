"""Persist tool starts before execution; learn only from completed, scoped observations."""

import json
import re
from typing import Literal

from pydantic import Field

from esi.models import uid, utc
from esi02.knowledge import teaching_candidates
from esi02.memory import Candidate, normalize
from esi02.world import Model
from esi03.storage import atomic
from esi03.tools import Evidence, Policy


class Request(Model):
    id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=40)
    arguments: dict[str, str]
    goal_id: str | None = None
    policy_revision: int = Field(ge=0)
    created_at: str
    stage: Literal["running", "complete", "failed", "cancelled"] = "running"
    error: str = ""
    result: dict = Field(default_factory=dict)


def document_candidates(evidence, topic=""):
    """Conservative literal Japanese claim spans, without model-written summaries."""
    text = evidence["text"]
    result = []
    for sentence in re.split(r"[。\n]", text):
        sentence = sentence.strip()
        if not sentence or len(sentence) > 500:
            continue
        explicit = teaching_candidates(sentence)
        result.extend(explicit)
        if not explicit:
            english = re.fullmatch(
                r"([A-Za-z][A-Za-z0-9 ,()'’-]{0,59}?)\s+(?:is|are)\s+(.{1,120})[.!]?", sentence
            )
            if english and not re.search(
                r"[?]|[\"「『]|\b(?:if|suppose|hypothetically)\b", sentence, re.IGNORECASE
            ):
                subject, value = english.groups()
                result.append(
                    Candidate(
                        kind="knowledge",
                        key="knowledge:" + normalize(subject),
                        label=subject,
                        value=value,
                        quote=sentence,
                        basis="inferred",
                    )
                )
        # Encyclopaedia-style definitions often use nominal or longer endings.
        if not explicit and topic and topic in sentence and "は" in sentence:
            subject, value = sentence.split("は", 1)
            if topic not in subject or re.search(
                r"[？?]|例えば|たとえば|仮に|もし|という文|[「『\"]", sentence
            ):
                continue
            value = value.strip()[:120]
            if value:
                result.append(
                    Candidate(
                        kind="knowledge",
                        key="knowledge:" + normalize(topic),
                        label=topic,
                        value=value,
                        quote=sentence[:500],
                        basis="inferred",
                    )
                )
        if len(result) >= 6:
            break
    # One source cannot reinforce the same topic through many redundant sentences.
    seen = set()
    return [item for item in result if not (item.key in seen or seen.add(item.key))][:6]


class Research:
    def __init__(self, session):
        self.session = session
        self.store = session.store
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS esi03_tool_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        self.policy_revision = self.store.meta("esi03_tool_policy_revision") or 0
        stored = self.store.meta("esi03_tool_policy")
        try:
            self.policy = Policy.model_validate(stored or {})
        except ValueError:
            self.policy = Policy(web_enabled=False)
            session.status["tools_error"] = (
                "保存した調査フォルダーを確認できないため探索を停止しました。範囲を設定し直してください。"
            )
        with self.store.lock, self.store.db:
            for request in self.entries(1000):
                if request.stage == "running":
                    request.stage = "cancelled"
                    request.error = "再起動前の操作結果を確認できないため自動再実行しません。"
                    self._put(request)
        session.tool_policy = self.policy

    def _put(self, request):
        self.store.db.execute(
            "INSERT OR REPLACE INTO esi03_tool_jobs VALUES (?,?)", (request.id, request.model_dump_json())
        )

    def entries(self, limit=20):
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT payload FROM esi03_tool_jobs ORDER BY rowid DESC LIMIT ?", (min(limit, 1000),)
            )
            return [Request.model_validate_json(row[0]) for row in rows]

    @atomic
    def configure(self, values):
        value = Policy.model_validate(values)
        with self.session.lock, self.store.lock, self.store.db:
            self.policy_revision += 1
            self.policy = self.session.tool_policy = value
            for request in self.entries(1000):
                if request.stage == "running":
                    request.stage = "cancelled"
                    request.error = "調査範囲が変更されたため中止しました。"
                    self._put(request)
            self.store.db.executemany(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                [
                    ("esi03_tool_policy", value.model_dump_json()),
                    ("esi03_tool_policy_revision", json.dumps(self.policy_revision)),
                ],
            )
        self.session.experience.trace(
            "tool_settings", "本人が探索とPC操作の範囲を設定", "ui:" + uid(), revision=self.policy_revision
        )
        self.session._write()

    @atomic
    def start(self, action, arguments, goal_id=None):
        permitted = {
            "web_search": self.policy.web_enabled,
            "fetch": self.policy.web_enabled,
            "read_folder": bool(self.policy.read_roots),
            "list_folder": bool(self.policy.read_roots),
            "read_file": bool(self.policy.read_roots),
            "open_url": self.policy.web_enabled and self.policy.allow_open_url,
            "open_file": bool(self.policy.read_roots) and self.policy.allow_open_file,
            "save_note": bool(self.policy.note_root) and self.policy.allow_save_note,
        }
        if action not in permitted or not permitted[action]:
            raise PermissionError("この機能は設定した操作範囲に含まれていません。")
        if not isinstance(arguments, dict) or any(
            not isinstance(value, str) or len(value) > 16000 for value in arguments.values()
        ):
            raise ValueError("操作の引数が正しくありません。")
        if any(request.stage == "running" for request in self.entries(1000)):
            raise RuntimeError("前の調査・PC操作が終わってから実行してください。")
        date = utc()[:10]
        usage = self.store.meta("esi03_tool_usage") or {"date": date, "count": 0}
        if usage["date"] != date:
            usage = {"date": date, "count": 0}
        if usage["count"] >= self.session.experience.state.preferences.daily_tool_budget:
            raise RuntimeError("今日の探索・操作回数が設定した上限に達しました。")
        request = Request(
            id=uid(),
            action=action,
            arguments=arguments,
            goal_id=goal_id,
            policy_revision=self.policy_revision,
            created_at=utc(),
        )
        usage["count"] += 1
        with self.store.lock, self.store.db:
            self._put(request)
            self.store.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('esi03_tool_usage',?)", (json.dumps(usage),)
            )
            self.store.db.execute(
                "DELETE FROM esi03_tool_jobs WHERE id NOT IN (SELECT id FROM esi03_tool_jobs ORDER BY rowid DESC LIMIT 1000)"
            )
        if goal_id:
            goal = next((item for item in self.session.experience.state.goals if item.id == goal_id), None)
            if goal is None:
                self.cancel(request.id, "探索目標が見つかりません。")
                raise ValueError("探索目標が見つかりません。")
            goal.stage = "running"
            goal.attempts += 1
            interest = next(
                item for item in self.session.experience.state.interests if item.id == goal.interest_id
            )
            interest.stage = "investigating"
        self.session.experience.trace(
            "tool_start", "実行する操作と範囲を保存", request.id, action=action, goal_id=goal_id
        )
        self.session._write([("tool_start", request.model_dump())])
        return request

    @atomic
    def cancel(self, identifier, reason="本人が操作を中止"):
        with self.session.lock, self.store.lock, self.store.db:
            row = self.store.db.execute(
                "SELECT payload FROM esi03_tool_jobs WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                return
            request = Request.model_validate_json(row[0])
            if request.stage != "running":
                return
            request.stage, request.error = "cancelled", reason[:600]
            self._put(request)
        self._goal_result(request, [], reason)
        self.session._write([("tool_cancel", {"id": identifier, "reason": reason})])

    def _goal_result(self, request, evidence, error):
        if not request.goal_id:
            return
        state = self.session.experience.state
        goal = next((goal for goal in state.goals if goal.id == request.goal_id), None)
        if not goal:
            return
        goal.stage = "cancelled" if request.stage == "cancelled" else "failed" if error else "complete"
        goal.error = error[:600] if error else ""
        goal.evidence_ids = [item["id"] for item in evidence][:24]
        interest = next(item for item in state.interests if item.id == goal.interest_id)
        interest.evidence_ids = list(dict.fromkeys(interest.evidence_ids + goal.evidence_ids))[-24:]
        interest.attempts += 1
        interest.next_at = state.elapsed + 600 * (2 ** min(3, interest.attempts))
        interest.stage = "paused" if error else "investigating"

    @atomic
    def finish(self, identifier, result=None, error=""):
        with self.session.lock:
            row = self.store.db.execute(
                "SELECT payload FROM esi03_tool_jobs WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                raise ValueError("実行記録のない結果は受け付けません。")
            request = Request.model_validate_json(row[0])
            if request.stage != "running" or request.policy_revision != self.policy_revision:
                return False
            result = result or {}
            evidence = [Evidence.model_validate(item).model_dump() for item in result.get("evidence", [])]
            if len(evidence) > 5:
                raise ValueError("調査結果は最大5資料です。")
            topic = ""
            if request.goal_id:
                goal = next(
                    goal for goal in self.session.experience.state.goals if goal.id == request.goal_id
                )
                topic = next(
                    item.topic
                    for item in self.session.experience.state.interests
                    if item.id == goal.interest_id
                )
            topic = topic or request.arguments.get("query", "")
            self.session.epoch += bool(evidence)
            novel_evidence = [item for item in evidence if self.evidence(item["id"]) is None]
            with self.store.lock, self.store.db:
                for item in evidence:
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO esi03_evidence VALUES (?,?)",
                        (item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)),
                    )
            learned_ids = []
            for item in evidence:
                try:
                    candidates = document_candidates(item, topic)
                    changed = self.session.knowledge.from_document(item, candidates, self.session.epoch)
                    if changed:
                        learned_ids.extend(
                            claim["id"]
                            for claim in self.session.knowledge.entries(include_old=True)
                            if claim.get("source_turn_id") == item["id"]
                        )
                except ValueError as exc:
                    self.session.status["tools_learning_notice"] = str(exc)
            request.stage = "failed" if error else "complete"
            request.error = error[:600]
            request.result = {key: value for key, value in result.items() if key != "evidence"}
            request.result.update(
                evidence_ids=[item["id"] for item in evidence], learned_claim_ids=learned_ids
            )
            with self.store.lock, self.store.db:
                self._put(request)
            self._goal_result(request, evidence, error)
            experience = self.session.experience
            if novel_evidence or learned_ids:
                experience.state.source_documents += len(novel_evidence)
                experience.state.acquired_claims += len(learned_ids)
                experience.state.last_novelty = experience.state.elapsed
                experience.outcome(
                    "research:" + identifier,
                    "research",
                    min(0.3, 0.08 + 0.04 * len(learned_ids)),
                    "retrieved_document",
                    identifier,
                    {"evidence_ids": [item["id"] for item in evidence], "truth_verified": False},
                )
                preferred = [
                    claim
                    for claim in self.session.knowledge.entries()
                    if claim["id"] in learned_ids or normalize(claim["label"]) == normalize(topic)
                ]
                for claim in preferred:
                    if claim.get("conflicted"):
                        interest = experience.interest(
                            claim["label"],
                            f"{claim['label']}について、教わったことと資料の説明が違うので一緒に確かめたい。どちらの説明をどう確かめたらいい？",
                            "資料と以前の教えの食い違いを再考したい",
                            "conflict",
                            identifier,
                            0.9,
                        )
                        if interest:
                            interest.stage = "open"
                            interest.next_at = experience.state.elapsed + 30
                    elif request.goal_id and preferred:
                        interest = next(
                            item for item in experience.state.interests if item.id == goal.interest_id
                        )
                        interest.stage = "resolved"
            experience.trace(
                "research_result",
                "調べた資料と未確認の説明を保存" if evidence else "操作の結果を保存",
                identifier,
                action=request.action,
                error=error,
                evidence_ids=[item["id"] for item in evidence],
                learned_claim_ids=learned_ids,
            )
            self.session._write([("tool_result", request.model_dump())])
            return True

    def evidence(self, identifier):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT payload FROM esi03_evidence WHERE id=?", (identifier,)
            ).fetchone()
            return json.loads(row[0]) if row else None
