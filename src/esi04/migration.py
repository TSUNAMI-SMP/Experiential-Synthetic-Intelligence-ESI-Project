"""Consistent ESI-02/03 copy migration; preserve the source and its full history."""

import hashlib
import sqlite3
import tempfile
from pathlib import Path

from esi.models import uid, utc
from esi.storage import Store
from esi02.migration import _publish_new
from esi02.session import inspect_database as inspect_esi02
from esi02.world import Point
from esi03.migration import migrate_esi02
from esi03.session import Session as PreviousSession
from esi03.session import inspect_database as inspect_previous
from esi04.config import Settings
from esi04.experience import Experience
from esi04.session import Session, inspect_database
from esi04.state import Cognition
from esi04.world import World


def validate_save(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("選択したセーブがありません。")
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True) as db:
        keys = {r[0] for r in db.execute("SELECT key FROM metadata")}
    if "esi04_settings" in keys:
        return {"release": "ESI-04", "settings": inspect_database(path)}
    if "esi03_settings" in keys:
        return {"release": "ESI-03", "settings": inspect_previous(path)}
    if "esi02_settings" in keys:
        return {"release": "ESI-02", "settings": inspect_esi02(path)}
    raise ValueError("ESI-02、ESI-03、ESI-04のセーブを選んでください。")


def migrate_esi03(source, destination, settings=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise FileExistsError("元のセーブを保持するため、新しい移行先を選んでください。")
    description = validate_save(source)
    if description["release"] == "ESI-04":
        raise ValueError("ESI-04はコピー移行せず、そのまま選んで開けます。")
    old_settings, source_release = description["settings"], description["release"]
    settings = settings or Settings.model_validate(old_settings.model_dump() | {"release": "ESI-04"})
    archive = destination.with_name(destination.stem+"."+source_release.lower().replace("-", "")+"-original.sqlite3")
    if archive.exists():
        raise FileExistsError("元セーブの保存先が存在します。新しい移行先を選んでください。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".esi04-migration-", dir=destination.parent) as temporary:
        original, converted = Path(temporary)/"original.sqlite3", Path(temporary)/"converted.sqlite3"
        reader = Store(source, read_only=True)
        try:
            target = sqlite3.connect(original)
            try:
                reader.db.backup(target)
            finally:
                target.close()
        finally:
            reader.close()
        with original.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        source_db, target_db = sqlite3.connect(original), sqlite3.connect(converted)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        if source_release == "ESI-02":
            intermediate = Path(temporary) / "intermediate-esi03.sqlite3"
            migrate_esi02(converted, intermediate)
            converted = intermediate
        old = PreviousSession(converted)
        try:
            old.checkpoint()
            state = old.dump()
            parent_branch = old.store.meta("branch_id")
        finally:
            old.close()
        world_data = state["world"] | {"version": 2, "destination": None, "path": []}
        world = World.model_validate(world_data)
        relocated = None
        if not world.free(world.position.x, world.position.z):
            relocated = world.position.model_dump()
            cells = [Point(x=i/4, z=j/4) for i in range(1, 24) for j in range(1, 20) if world.free(i/4, j/4)]
            world.position = min(cells, key=world.position.distance)
        world.body.activity = "idle"
        state["experience"]["preferences"]["min_question_interval"] = 0.0
        experience = Experience(state["experience"])
        for item in experience.state.interests:
            if item.last_asked is not None:
                experience.state.question_contexts[item.id] = experience.context_signature(item)
        experience.recover()
        experience.trace("migration", source_release+"の履歴・人格・知識・身体を元ファイルを残してコピー移行",
                          "migration:"+digest, source_release=source_release, position_adjusted_from=relocated)
        active = state["motivation"]["active_id"]
        for action in state["motivation"]["actions"]:
            if action["id"] == active:
                action.update(stage="cancelled", reason="ESI-04へ移行したため途中の動作は自動再実行しない")
        state["motivation"]["active_id"] = None
        state.update(release="ESI-04", world=world.model_dump(), experience=experience.state.model_dump(),
                     cognition=Cognition().model_dump())
        writer = Store(converted)
        try:
            writer.set_meta("esi04_settings", settings.model_dump())
            writer.set_meta("esi04_live", state)
            writer.set_meta("parent_branch_id", parent_branch)
            writer.set_meta("branch_id", uid())
            writer.set_meta("esi04_import", {"source": str(source), "archive": str(archive),
                                             "source_release": source_release, "archive_sha256": digest, "at": utc()})
            writer.snapshot(state, initial=True)
        finally:
            writer.close()
        migrated = Session(converted)
        migrated.close()
        try:
            _publish_new(original, archive)
            published.append(archive)
            _publish_new(converted, destination)
            published.append(destination)
        except Exception:
            for path in published:
                path.unlink()
            raise
    return {"release": "ESI-04", "destination": destination, "archive": archive,
            "archive_sha256": digest, "position_adjusted_from": relocated}
