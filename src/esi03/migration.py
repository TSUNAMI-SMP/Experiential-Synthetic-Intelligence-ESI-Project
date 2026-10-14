"""Consistent copy migration, preserving the entire ESI-02 database as an archive."""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from esi.models import uid, utc
from esi.storage import Store
from esi02.migration import _publish_new
from esi02.session import Session as PreviousSession
from esi02.session import inspect_database as inspect_previous
from esi03.config import Settings
from esi03.experience import Experience
from esi03.session import Session, inspect_database


def validate_save(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("選択したセーブがありません。")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        keys = {row[0] for row in db.execute("SELECT key FROM metadata")}
    if "esi03_settings" in keys:
        return {"release": "ESI-03", "settings": inspect_database(path)}
    if "esi02_settings" in keys:
        return {"release": "ESI-02", "settings": inspect_previous(path)}
    raise ValueError("このセーブはESI-02かESI-03ではありません。ESI-01はまずESI-02へコピー移行してください。")


def migrate_esi02(source, destination, settings=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise FileExistsError("元データを保持するため、移行先には新しいファイル名を指定してください。")
    original_settings = inspect_previous(source)
    archive = destination.with_name(destination.stem + ".esi02-original.sqlite3")
    if archive.exists():
        raise FileExistsError("元セーブの保存先が存在します。別の移行先を選んでください。")
    settings = settings or Settings.model_validate({**original_settings.model_dump(), "release": "ESI-03"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".esi03-migration-", dir=destination.parent) as directory:
        backup, converted = Path(directory) / "original.sqlite3", Path(directory) / "converted.sqlite3"
        reader = Store(source, read_only=True)
        try:
            db = sqlite3.connect(backup)
            try:
                reader.db.backup(db)  # Includes committed WAL; source is never opened as a writer.
            finally:
                db.close()
        finally:
            reader.close()
        with backup.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        original = sqlite3.connect(backup)
        target = sqlite3.connect(converted)
        try:
            original.backup(target)
        finally:
            target.close()
            original.close()
        # Recovery touches only the private copy. Retain uncheckpointed ESI-02 commands.
        old = PreviousSession(converted)
        try:
            old.checkpoint()
            state = old.store.load_snapshot()["state"]
            beliefs = old.knowledge.entries(include_old=True)
            parent_branch = old.store.meta("branch_id")
        finally:
            old.close()
        experience = Experience()
        experience.state.elapsed = state["world"]["elapsed"]
        experience.state.last_contact = state["motivation"].get("last_contact", 0)
        experience.state.acquired_claims = sum(item["status"] == "active" for item in beliefs)
        experience.trace(
            "migration",
            "ESI-02の記憶・教わった知識・身体の学習をコピー移行",
            "migration:" + digest,
            source_release="ESI-02",
        )
        state.update(release="ESI-03", schema_version=1, experience=experience.state.model_dump())
        writer = Store(converted)
        try:
            writer.db.execute(
                "CREATE TABLE IF NOT EXISTS esi03_beliefs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            for item in beliefs:
                item.update(
                    confidence=0.9
                    if item["trust"] == "confirmed"
                    else 0.35
                    if item["trust"] == "inferred"
                    else 0.65,
                    conflicted=False,
                    evidence=[
                        {
                            "id": item["source_turn_id"] or "migration:" + item["id"],
                            "source": item["source"],
                            "quote": item["quote"],
                            "at": item["created_at"],
                            "basis": "migration",
                            "url": None,
                        }
                    ],
                )
                writer.db.execute(
                    "INSERT INTO esi03_beliefs VALUES (?,?)",
                    (item["id"], json.dumps(item, ensure_ascii=False)),
                )
            writer.db.commit()
            writer.set_meta("esi03_settings", settings.model_dump())
            writer.set_meta("esi03_live", state)
            writer.set_meta("esi03_beliefs_revision", writer.meta("esi02_knowledge_revision") or 0)
            writer.set_meta("esi03_beliefs_history_floor", writer.meta("esi02_knowledge_history_floor") or 0)
            writer.set_meta("parent_branch_id", parent_branch)
            writer.set_meta("branch_id", uid())
            writer.set_meta(
                "esi02_import",
                {"source": str(source), "archive": str(archive), "archive_sha256": digest, "at": utc()},
            )
            writer.snapshot(state, initial=True)
        finally:
            writer.close()
        converted_session = Session(converted)
        try:
            converted_session.checkpoint()
        finally:
            converted_session.close()
        try:
            _publish_new(backup, archive)
            published.append(archive)
            _publish_new(converted, destination)
            published.append(destination)
        except Exception:
            for path in published:
                path.unlink()
            raise
    return {
        "destination": destination,
        "archive": archive,
        "archive_sha256": digest,
        "imported_beliefs": len(beliefs),
        "release": "ESI-03",
    }
