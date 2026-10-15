"""ESI-04 commands keep status/diagnostics read-only and migration explicit."""

import argparse
import json
from pathlib import Path

from esi04.config import Settings
from esi04.session import Session, inspect_database


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESI-04 会話・空間・画面・PC操作")
    parser.add_argument("--database", default="data/esi04/session.sqlite3")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("gui", "launcher", "status", "prepare-vision"):
        commands.add_parser(name)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--output")
    doctor.add_argument("--verify-models", action="store_true")
    doctor.add_argument("--executable")
    migration = commands.add_parser("migrate-esi03")
    migration.add_argument("source")
    actors = commands.add_parser("characters")
    actors.add_argument("--directory", default="data/esi04/characters")
    new = commands.add_parser("new-character")
    new.add_argument("name")
    new.add_argument("--preset", choices=["curious", "steadfast", "flexible"], default="curious")
    new.add_argument("--directory", default="data/esi04/characters")
    args = parser.parse_args(argv)
    if args.command == "launcher":
        from esi04.launcher import main as launch
        return launch()
    path = Path(args.database)
    settings = Settings.read(args.config) if args.config else inspect_database(path) if path.exists() else Settings()
    if args.command == "migrate-esi03":
        from esi04.migration import migrate_esi03
        output(migrate_esi03(args.source, path, settings if args.config else None))
        return 0
    if args.command == "prepare-vision":
        from esi04.setup_assets import prepare_vision
        prepare_vision(Path.cwd())
        return 0
    if args.command in {"characters", "new-character"}:
        from esi04.characters import Characters
        characters = Characters(args.directory)
        output({"created": characters.create(args.name, args.preset, settings)}
               if args.command == "new-character" else characters.entries())
        return 0
    if args.command == "status":
        from esi.storage import Store
        reader = Store(path, read_only=True)
        try:
            output(reader.meta("esi04_live"))
        finally:
            reader.close()
        return 0
    if args.command == "doctor":
        from esi04.diagnostics import collect_report, save_report
        report = collect_report(Path.cwd(), settings, path, executable=args.executable, verify_models=args.verify_models)
        output({"saved": save_report(Path.cwd(), report, args.output)} if args.output else report)
        return 0
    from PySide6.QtWidgets import QApplication

    from esi04.gui import Window
    from esi04.services import Services
    services = Services(Path.cwd(), {}, print)
    session = None
    try:
        services.prepare(settings)
        session = Session(path, settings)
        app = QApplication.instance() or QApplication([])
        window = Window(session, services.backend, services)
        window.show()
        result = app.exec()
        session = None
        return result
    finally:
        if session:
            session.close()
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
