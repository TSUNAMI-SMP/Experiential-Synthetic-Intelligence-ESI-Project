"""Read-only save discovery and transcript browsing; recovery always keeps the source."""

import json
import sqlite3
from pathlib import Path, PureWindowsPath

from esi02.launcher import read_preferences


def saved_path(root, value):
    """Resolve portable preferences and missing paths from an older ZIP directory."""
    root = Path(root).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if path.is_file():
        return path.resolve()
    # Old Windows preferences used absolute paths. Only rebase the data suffix;
    # a missing arbitrary custom save must still be selected by its owner.
    parts = PureWindowsPath(value).parts if "\\" in str(value) else Path(value).parts
    for index, part in enumerate(parts):
        if part.lower() == "data":
            relocated = root.joinpath(*parts[index:])
            if relocated.is_file():
                return relocated.resolve()
    return path


def previous_source(root, release):
    root = Path(root)
    preferences = read_preferences(root / "data" / release / "launcher.json")
    if preferences.save_path:
        selected = saved_path(root, preferences.save_path)
        if selected.is_file():
            return selected
        # Do not silently migrate a different character if a selected save is missing.
        raise FileNotFoundError(f"以前に選んだセーブが見つかりません：{preferences.save_path}。セーブを選び直してください。")
    default = root / "data" / release / "session.sqlite3"
    return default if default.is_file() else None


def save_summary(path):
    path = Path(path).resolve(strict=True)
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        rows = dict(db.execute("SELECT key,value FROM metadata WHERE key IN "
                               "('esi04_live','esi03_live','agent_id','esi02_settings')"))
        live_key = next((key for key in ("esi04_live", "esi03_live") if key in rows), None)
        if live_key:
            state = json.loads(rows[live_key])
            release = "ESI-04" if live_key == "esi04_live" else "ESI-03"
        elif "esi02_settings" in rows:
            snapshot = db.execute("SELECT payload FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            state, release = (json.loads(snapshot[0]) if snapshot else {}), "ESI-02"
        else:
            raise ValueError("ESI-02以降のセーブではありません。")
        if not isinstance(state, dict):
            raise ValueError("保存状態の形式が正しくありません。")
        experience = state.get("experience", {})
        character = experience.get("character", {}) if isinstance(experience, dict) else None
        if not isinstance(character, dict):
            raise ValueError("キャラクターの保存形式が正しくありません。")
        archive = "UNION SELECT id FROM esi04_conversations " if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='esi04_conversations'").fetchone() else ""
        turns = db.execute("SELECT COUNT(*) FROM (SELECT json_extract(payload,'$.turn.id') FROM records "
                           "WHERE kind='turn_state' "+archive+"UNION SELECT json_extract(value,'$.id') "
                           "FROM json_each(?, '$.turns'))", (json.dumps(state),)).fetchone()[0]
        utterances = db.execute("SELECT COUNT(*) FROM esi04_utterances").fetchone()[0] if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='esi04_utterances'").fetchone() else 0
        return {"path": path, "release": release, "agent_id": json.loads(rows.get("agent_id", '""')),
                "name": character.get("name", "凛音エル"),
                "turns": max(turns, len(state.get("turns", []))), "utterances": utterances}


def discover_saves(root, current=None):
    root = Path(root)
    paths = [Path(current)] if current else []
    for release in ("esi04", "esi03", "esi02"):
        directory = root / "data" / release
        try:
            selected = previous_source(root, release)
            if selected:
                paths.append(selected)
        except (OSError, ValueError):
            pass  # Default and other known saves remain available for manual recovery.
        paths.extend(directory.glob("*.sqlite3"))
        paths.extend(directory.glob("characters/*/*.sqlite3"))
    results, seen = [], set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            results.append(save_summary(resolved))
        except (OSError, ValueError, sqlite3.Error):
            continue
    return sorted(results, key=lambda item: (item["turns"] == 0, item["release"], str(item["path"])))


def history_page(path, before=None, limit=100):
    """Read archived turn states, independently of the 12-turn conversation preview."""
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        live = db.execute("SELECT value FROM metadata WHERE key IN ('esi04_live','esi03_live') "
                          "ORDER BY key DESC LIMIT 1").fetchone()
        if not live:
            live = db.execute("SELECT payload FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
        state = json.loads(live[0]) if live else {}
        archive = "UNION ALL SELECT 9223372036854775806,payload FROM esi04_conversations " if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='esi04_conversations'").fetchone() else ""
        # Metadata/snapshots retain the recent turns even if event-log capacity
        # has compacted their records. Prefer their current state on duplicate IDs.
        rows = db.execute("""
            WITH candidates AS (
                SELECT seq,json_extract(payload,'$.turn') AS turn FROM records WHERE kind='turn_state'
                """+archive+"""UNION ALL SELECT 9223372036854775807,value FROM json_each(?, '$.turns')
            ), latest AS (
                SELECT turn,ROW_NUMBER() OVER (
                    PARTITION BY json_extract(turn,'$.id') ORDER BY seq DESC) AS rank FROM candidates
            )
            SELECT json_extract(turn,'$.epoch'),turn FROM latest
            WHERE rank=1 AND json_extract(turn,'$.epoch')<?
            ORDER BY json_extract(turn,'$.epoch') DESC LIMIT ?
        """, (json.dumps(state), before if before is not None else 2**63-1, min(200, max(1, limit)))).fetchall()
    return [(epoch, json.loads(turn)) for epoch, turn in rows]


def utterance_page(path, low=0, high=2**63-1):
    """Assistant-only speech in a page's epoch range, independently of event compaction."""
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='esi04_utterances'").fetchone():
            return []
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM esi04_utterances WHERE after_epoch>=? AND after_epoch<? "
            "ORDER BY after_epoch,rowid", (low, high))]
