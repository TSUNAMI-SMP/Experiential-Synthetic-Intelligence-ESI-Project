"""Independent character saves, sharing service/model files rather than GPU allocations."""

from pathlib import Path

from esi.models import uid
from esi03.personality import Character, Traits
from esi03.session import Session, inspect_database

PRESETS = {
    "curious": Traits(),
    "steadfast": Traits(stubbornness=0.85, flexibility=0.25, trust=0.4, persistence=0.9),
    "flexible": Traits(stubbornness=0.2, flexibility=0.85, trust=0.8, sociability=0.8),
}


class Characters:
    def __init__(self, directory):
        self.directory = Path(directory)

    def entries(self):
        if not self.directory.exists():
            return []
        result = []
        for path in sorted(self.directory.glob("*/session.sqlite3")):
            try:
                inspect_database(path)
                from esi.storage import Store

                reader = Store(path, read_only=True)
                try:
                    state = reader.meta("esi03_live")
                    result.append(
                        {
                            "id": reader.meta("agent_id"),
                            "name": state["experience"]["character"]["name"],
                            "path": str(path.resolve()),
                        }
                    )
                finally:
                    reader.close()
            except (OSError, ValueError):
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
