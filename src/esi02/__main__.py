"""Developer entry point. No completed ESI-02 distribution is published yet."""

import argparse
import json
from pathlib import Path

from esi02.config import Settings
from esi02.gpu import plan_llama
from esi02.session import Session, inspect_database


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESI-02 開発中")
    parser.add_argument("--database", default="data/esi02/session.sqlite3")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gui")
    commands.add_parser("status")
    migration = commands.add_parser("migrate-esi01")
    migration.add_argument("source")
    probe = commands.add_parser("gpu-plan")
    probe.add_argument("--executable", required=True)
    probe.add_argument("--model", default="models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf")
    args = parser.parse_args(argv)
    if args.command == "migrate-esi01":
        from esi02.migration import migrate_esi01

        result = migrate_esi01(
            args.source, args.database, Settings.read(args.config) if args.config else None
        )
        print(
            json.dumps(
                {k: str(v) if isinstance(v, Path) else v for k, v in result.items()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    path = Path(args.database)
    settings = (
        Settings.read(args.config) if args.config else inspect_database(path) if path.exists() else Settings()
    )
    if args.command == "gpu-plan":
        placement, diagnosis = plan_llama(args.executable, args.model, settings)
        print(json.dumps({"plan": placement.dump(), "diagnosis": diagnosis}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        if not path.exists():
            raise FileNotFoundError("ESI-02のセーブがありません。")
        # Status does not open a writer or checkpoint an experiment.
        from esi.storage import Store

        store = Store(path, read_only=True)
        try:
            print(json.dumps(store.load_snapshot(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    from esi02.gui import launch
    from esi02.services import Services

    service = Services(Path.cwd(), {}, print)
    try:
        service.prepare(settings)
        session = Session(path, settings)
        return launch(session, service.backend, service)
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
