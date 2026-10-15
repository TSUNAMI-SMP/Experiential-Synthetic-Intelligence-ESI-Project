"""Separate ESI-04 character saves; model processes are shared rather than duplicated."""

import sqlite3
from pathlib import Path

from esi.models import uid
from esi.storage import Store
from esi03.characters import PRESETS
from esi03.personality import Character
from esi04.session import Session, inspect_database


class Characters:
    def __init__(self, directory):
        self.directory = Path(directory)

    def entries(self):
        result = []
        for path in sorted(self.directory.glob("*/session.sqlite3")):
            try:
                inspect_database(path)
                reader = Store(path, read_only=True)
                try:
                    state = reader.meta("esi04_live")
                    result.append({"id": reader.meta("agent_id"), "name": state["experience"]["character"]["name"],
                                   "path": str(path.resolve())})
                finally:
                    reader.close()
            except (OSError, ValueError, sqlite3.Error):
                continue
        return result

    def create(self, name="凛音エル", preset="curious", settings=None):
        if preset not in PRESETS:
            raise ValueError("性格の初期設定が見つかりません。")
        traits = PRESETS[preset].model_copy(deep=True)
        character = Character(name=name, traits=traits, initial_traits=traits.model_copy(deep=True))
        path = self.directory / uid() / "session.sqlite3"
        session = Session(path, settings=settings, character=character.model_dump())
        try:
            session.checkpoint()
        finally:
            session.close()
        return path
