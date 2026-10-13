"""Copy ESI-01 consistently, archive its numerical learning, import external dialogue only."""

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

from esi.core import Core
from esi.models import Observation, Source, utc
from esi.storage import Store
from esi02.config import Settings
from esi02.session import Session, inspect_database


def validate_save(path):
    """Read-only preflight; a failed selection never stops the current session or edits the file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("選んだセーブが見つかりません。")
    store = Store(path, read_only=True)
    try:
        if store.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("セーブの整合性を確認できません。")
        if store.meta("esi02_settings"):
            settings = inspect_database(path)
            snapshot = store.load_snapshot()
            if not snapshot or snapshot["state"].get("release") != "ESI-02":
                raise ValueError("ESI-02の保存状態を確認できません。")
            return {"release": "ESI-02", "settings": settings}
    finally:
        store.close()
    core = Core(path, read_only=True)
    try:
        return {"release": "ESI-01", "elapsed": core.elapsed, "agent_id": core.agent_id}
    finally:
        core.close()


def _publish_new(source, destination):
    # O_EXCL avoids replacing an existing save, including a file created during conversion.
    import shutil

    created = False
    try:
        with Path(source).open("rb") as src, Path(destination).open("xb") as dst:
            created = True
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    except Exception:
        if created:
            Path(destination).unlink()
        raise


def migrate_esi01(source, destination, settings=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise FileExistsError("移行先には新しいファイル名を選んでください。元のセーブは保持しました。")
    if validate_save(source)["release"] != "ESI-01":
        raise ValueError("ESI-01から移行する操作です。ESI-02はそのまま開けます。")
    archive = destination.with_name(destination.stem + ".esi01-original.sqlite3")
    if archive.exists():
        raise FileExistsError("移行用の元データのコピーがすでにあります。別のファイル名を選んでください。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".esi02-migration-", dir=destination.parent) as folder:
        backup, converted = Path(folder) / "original.sqlite3", Path(folder) / "converted.sqlite3"
        original = Store(source, read_only=True)
        try:
            target = sqlite3.connect(backup)
            try:
                # SQLite backup includes the committed WAL, unlike copying only the main file.
                original.db.backup(target)
            finally:
                target.close()
        finally:
            original.close()
        with backup.open("rb") as stream:
            original_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        core = Core(backup, read_only=True)
        session = None
        try:
            state = core.dump()  # includes durable input commands after the last snapshot
            session = Session(converted, settings or Settings())
            session.agent_id = core.agent_id
            session.elapsed = core.elapsed
            session.store.set_meta("agent_id", core.agent_id)
            session.store.set_meta("parent_branch_id", core.branch_id)
            session.store.set_meta(
                "esi01_import",
                {
                    "created_at": utc(),
                    "source": str(source),
                    "archive": str(archive),
                    "archive_sha256": original_sha,
                    "release": "ESI-01",
                    "settings": core.settings.model_dump(),
                    "state": state,
                    "note": "旧予測器と報酬は資料として保存。新しい身体や学習器へ流用しない。",
                },
            )
            # Imported actions/self-output are retained in the archive, never asserted as user facts.
            events = {m["event_id"]: m for m in reversed(core.store.recent_observations(limit=512))}
            events.update({e.event_id: e.model_dump(mode="json") for e in core.events.values()})
            external = [
                e
                for e in events.values()
                if e["source"] in {Source.MICROPHONE, Source.RECORDING, Source.SIMULATED} and e.get("text")
            ]
            external.sort(key=lambda e: e["wall_start"])
            for data in external[-session.settings.retained_turns :]:
                event = Observation.model_validate(data)
                text = event.text[:2000]
                remember = event.source != Source.SIMULATED and not event.metadata.get(
                    "tts_contamination_possible"
                )
                turn = session.create_turn(
                    text, "ESI-01/" + event.source.value, event.event_id, remember=remember
                )
                session.update_turn(
                    turn["id"],
                    stage="complete",
                    created_at=event.wall_start,
                    legacy_import=True,
                    reply="",
                    legacy_text_truncated=len(event.text) > 2000,
                )
                # Simulated research stimuli are not the user's biography. Do not analyze them.
            session.checkpoint()
            session.close()
            session = None
        finally:
            if session:
                session.close()
            core.close()
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
        "archive_sha256": original_sha,
        "imported_turns": len(external[-(settings or Settings()).retained_turns :]),
    }
