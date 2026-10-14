"""ESI-03 developer and diagnostic commands, with read-only status preflight."""

import argparse
import json
from pathlib import Path

from esi02.dialogue import Cancellation
from esi03.characters import Characters
from esi03.config import Settings
from esi03.session import Session, inspect_database


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESI-03 会話・成長・探索")
    parser.add_argument("--database", default="data/esi03/session.sqlite3")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gui")
    commands.add_parser("launcher")
    commands.add_parser("status")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--output")
    doctor.add_argument("--verify-models", action="store_true")
    doctor.add_argument("--executable")
    migration = commands.add_parser("migrate-esi02")
    migration.add_argument("source")
    actors = commands.add_parser("characters")
    actors.add_argument("--directory", default="data/esi03/characters")
    new = commands.add_parser("new-character")
    new.add_argument("name")
    new.add_argument("--preset", choices=["curious", "steadfast", "flexible"], default="curious")
    new.add_argument("--directory", default="data/esi03/characters")
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--runs", type=int, choices=[1, 3, 5], default=3)
    args = parser.parse_args(argv)
    if args.command == "launcher":
        from esi03.launcher import main as launch

        return launch()
    path = Path(args.database)
    settings = (
        Settings.read(args.config) if args.config else inspect_database(path) if path.exists() else Settings()
    )
    if args.command == "migrate-esi02":
        from esi03.migration import migrate_esi02

        output(migrate_esi02(args.source, path, settings if args.config else None))
        return 0
    if args.command in {"characters", "new-character"}:
        roster = Characters(args.directory)
        if args.command == "new-character":
            output({"created": roster.create(args.name, args.preset, settings)})
        else:
            output(roster.entries())
        return 0
    if args.command == "status":
        from esi.storage import Store

        reader = Store(path, read_only=True)
        try:
            output(reader.meta("esi03_live"))
        finally:
            reader.close()
        return 0
    if args.command == "doctor":
        from esi03.diagnostics import collect_report, save_report

        report = collect_report(
            Path.cwd(), settings, path, executable=args.executable, verify_models=args.verify_models
        )
        if args.output:
            output({"saved": save_report(Path.cwd(), report, args.output)})
        else:
            output(report)
        return 0
    if args.command == "benchmark":
        from esi03.performance import benchmark_language

        output(benchmark_language(settings, Cancellation(), runs=args.runs))
        return 0
    from esi02.gui import QApplication
    from esi03.gui import Window
    from esi03.services import Services

    service = Services(Path.cwd(), {}, print)
    session = None
    try:
        service.prepare(settings)
        session = Session(path, settings)
        app = QApplication.instance() or QApplication([])
        window = Window(session, service.backend, service)
        window.show()
        result = app.exec()
        session = None  # Accepted Window.closeEvent closed its save.
        return result
    finally:
        if session:
            session.close()
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
