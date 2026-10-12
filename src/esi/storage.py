import json
import sqlite3
import threading
from pathlib import Path

from esi.models import uid, utc


class Store:
    """Serialized writer; WAL and transactional snapshots. No natural-language admin operations."""

    def __init__(self, path: str | Path, max_records=200_000, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        self.lock = threading.RLock()
        self.max_records = max_records
        if read_only:
            self.db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("BEGIN")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS records_kind ON records(kind,seq);
        """)
        self.db.commit()
        for key in ("agent_id", "branch_id"):
            if self.meta(key) is None:
                self.set_meta(key, uid())

    def meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False))
            )

    def append(self, kind, payload):
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO records(kind,payload) VALUES (?,?)",
                (kind, json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )
            return cursor.lastrowid

    def records(self, kind=None, after=0, limit=1000):
        with self.lock:
            query = "SELECT seq,kind,payload FROM records WHERE seq>?"
            args = [after]
            if kind is not None:
                query += " AND kind=?"
                args.append(kind)
            query += " ORDER BY seq LIMIT ?"
            args.append(limit)
            return [
                {"seq": r[0], "kind": r[1], "payload": json.loads(r[2])} for r in self.db.execute(query, args)
            ]

    def recent_observations(self, limit=200):
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM records WHERE kind='observation' ORDER BY seq DESC LIMIT ?", (limit,)
            )
            return [json.loads(row[0]) for row in rows]

    def recent_records(self, kind, limit=3):
        with self.lock:
            rows = list(
                self.db.execute(
                    "SELECT seq,kind,payload FROM records WHERE kind=? ORDER BY seq DESC LIMIT ?",
                    (kind, limit),
                )
            )
            return [{"seq": r[0], "kind": r[1], "payload": json.loads(r[2])} for r in reversed(rows)]

    def snapshot(self, state, initial=False):
        sid = uid()
        with self.lock, self.db:
            seq = self.db.execute("SELECT coalesce(max(seq),0) FROM records").fetchone()[0]
            self.db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?)",
                (sid, seq, utc(), json.dumps(state, ensure_ascii=False, allow_nan=False)),
            )
            count = self.db.execute("SELECT count(*) FROM records").fetchone()[0]
            if count > self.max_records:
                # Retain a full replay boundary: keep all commands after this checkpoint.
                self.db.execute("DELETE FROM records WHERE seq<=?", (seq,))
                self.db.execute("DELETE FROM snapshots WHERE id<>?", (sid,))
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('replay_base',?)", (json.dumps(sid),)
                )
            elif initial:
                self.db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('replay_base',?)", (json.dumps(sid),)
                )
            base = self.meta("replay_base")
            self.db.execute(
                "DELETE FROM snapshots WHERE id<>? AND id NOT IN (SELECT id FROM snapshots ORDER BY rowid DESC LIMIT 64)",
                (base or sid,),
            )
        return sid

    def load_snapshot(self, snapshot_id=None):
        with self.lock:
            if snapshot_id:
                row = self.db.execute(
                    "SELECT id,seq,payload FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT id,seq,payload FROM snapshots ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            return {"id": row[0], "seq": row[1], "state": json.loads(row[2])} if row else None

    def fork(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("Branch destination already exists; existing data preserved")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            target = sqlite3.connect(destination)
            try:
                self.db.backup(target)
            finally:
                target.close()
        return destination

    def close(self):
        with self.lock:
            self.db.close()
