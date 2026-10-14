import hashlib
import json

import pytest

from esi02.session import Session as PreviousSession
from esi03.characters import Characters
from esi03.migration import migrate_esi02
from esi03.personality import Character, Traits
from esi03.session import Session, inspect_database


def test_correction_preserves_wrong_teaching_and_provenance(tmp_path):
    session = Session(tmp_path / "character.db")
    try:
        session.create_turn("星砂村は月の国です。")
        wrong = session.knowledge.entries()[0]
        session.create_turn("星砂村は月の国ではなく海の国です。")
        correct = session.knowledge.entries()[0]
        assert correct["value"] == "海の国"
        assert wrong["id"] != correct["id"]
        assert len(session.knowledge.alternatives(correct["key"])) == 2
        assert session.knowledge.history(correct["key"])
        assert session.experience.state.corrections == 1
    finally:
        session.close()


def test_stubbornness_changes_belief_updates_not_only_tone(tmp_path):
    stubborn = Character(traits=Traits(stubbornness=0.98, trust=0.15, flexibility=0.1))
    flexible = Character(traits=Traits(stubbornness=0.05, trust=0.95, flexibility=0.95))
    choices = []
    for number, character in enumerate((stubborn, flexible)):
        session = Session(tmp_path / f"{number}.db", character=character.model_dump())
        try:
            session.create_turn("星砂村は月の国です。")
            session.create_turn("星砂村は海の国です。")
            choices.append(session.knowledge.entries()[0]["value"])
            assert len(session.knowledge.entries(include_old=True)) == 2
        finally:
            session.close()
    assert choices == ["月の国", "海の国"]


def test_crash_recovers_uncheckpointed_learning_and_cancels_speech(tmp_path):
    path = tmp_path / "character.db"
    session = Session(path)
    turn = session.create_turn("星砂村は月の国です。")
    session.advance_world(30)
    session.update_turn(turn["id"], stage="playing")
    state = session.experience.state.model_dump()
    session.store.close()  # Simulate loss before the next periodic checkpoint.
    session = Session(path)
    try:
        assert session.world.elapsed == 30
        assert session.experience.state.character.model_dump() == state["character"]
        assert session.knowledge.entries()[0]["value"] == "月の国"
        assert session.turns[turn["id"]]["stage"] == "cancelled"
        assert session.experience.state.acquired_claims == 1
    finally:
        session.close()


def test_uncertain_asr_does_not_teach_or_reward(tmp_path):
    session = Session(tmp_path / "character.db")
    try:
        session.create_turn("星砂村は月の国です。ありがとう", "microphone", remember=False)
        assert not session.knowledge.entries()
        assert session.experience.state.last_user_epoch == 0
        assert session.experience.state.habits["conversation"].trials == 0
    finally:
        session.close()


def test_migration_copies_wal_and_keeps_source_unchanged(tmp_path):
    source, destination = tmp_path / "old.db", tmp_path / "new.db"
    old = PreviousSession(source)
    old.create_turn("星砂村は月の国です。")
    old.advance_world(30)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = migrate_esi02(source, destination)
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["archive"].is_file()
    assert inspect_database(destination).release == "ESI-03"
    migrated = Session(destination)
    try:
        assert migrated.world.elapsed == 30
        assert migrated.agent_id == old.agent_id
        assert migrated.knowledge.entries()[0]["value"] == "月の国"
        assert migrated.experience.state.acquired_claims == 1
    finally:
        migrated.close()
        old.close()


def test_migration_never_replaces_existing_data(tmp_path):
    source, destination = tmp_path / "old.db", tmp_path / "new.db"
    old = PreviousSession(source)
    old.close()
    destination.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        migrate_esi02(source, destination)
    assert destination.read_bytes() == b"preserve"


def test_characters_have_independent_personalities_and_memories(tmp_path):
    roster = Characters(tmp_path / "characters")
    first = roster.create("エル", "curious")
    second = roster.create("リオン", "steadfast")
    left, right = Session(first), Session(second)
    try:
        left.create_turn("星砂村は月の国です。")
        assert not right.knowledge.entries()
        assert left.agent_id != right.agent_id
        assert (
            left.experience.state.character.traits.stubbornness
            != right.experience.state.character.traits.stubbornness
        )
        assert len(roster.entries()) == 2
    finally:
        left.close()
        right.close()


def test_invalid_save_fails_readonly_preflight(tmp_path):
    path = tmp_path / "character.db"
    session = Session(path)
    state = session.dump()
    state["experience"]["relationship"] = 100
    session.store.set_meta("esi03_live", state)
    session.store.close()
    before = path.read_bytes()
    with pytest.raises(ValueError):
        inspect_database(path)
    assert path.read_bytes() == before


def test_deleted_or_ui_edited_belief_cannot_return_from_old_extraction(tmp_path):
    session = Session(tmp_path / "character.db")
    try:
        old = session.create_turn("星砂村は月の国です。")
        identifier = session.knowledge.entries()[0]["id"]
        session.memory_operation("edit", {"scope": "knowledge", "id": identifier, "value": "海の国"})
        from esi02.knowledge import teaching_candidates

        assert not session.knowledge.apply(old, teaching_candidates(old["input"]))
        assert session.knowledge.entries()[0]["value"] == "海の国"
        session.memory_operation("delete", {"scope": "knowledge", "id": identifier})
        assert not session.knowledge.apply(old, teaching_candidates(old["input"]))
        assert not session.knowledge.entries()
    finally:
        session.close()


def test_outcome_ledger_survives_recent_cache_eviction(tmp_path):
    session = Session(tmp_path / "character.db")
    try:
        assert session.experience.outcome("first", "research", 0.1, "retrieved_document", "doc")
        session.checkpoint()
        session.experience.state.recent_outcomes = []
        assert not session.experience.outcome("first", "research", 0.1, "retrieved_document", "doc")
        json.dumps(session.dump(), allow_nan=False)
    finally:
        session.close()


def test_failed_transaction_rolls_back_beliefs_and_in_memory_core(tmp_path, monkeypatch):
    session = Session(tmp_path / "character.db")
    before = session.dump()
    original = session._write

    def fail(events=()):
        original(events)
        raise RuntimeError("injected save failure")

    monkeypatch.setattr(session, "_write", fail)
    with pytest.raises(RuntimeError):
        session.create_turn("星砂村は月の国です。")
    assert session.dump() == before
    assert not session.knowledge.entries()
    monkeypatch.setattr(session, "_write", original)
    session.close()


def test_process_loss_inside_learning_does_not_commit_half_a_turn(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "character.db"
    session = Session(path)
    session.close()
    script = """
import os, sys
from esi03.session import Session
session = Session(sys.argv[1])
remember = session._remember
def interrupted(turn):
    remember(turn)
    os._exit(17)
session._remember = interrupted
session.create_turn('星砂村は月の国です。ありがとう')
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=10)
    assert result.returncode == 17
    session = Session(path)
    try:
        assert not session.turns
        assert not session.knowledge.entries()
        assert session.experience.state.acquired_claims == 0
        assert session.experience.state.habits["conversation"].trials == 0
    finally:
        session.close()
