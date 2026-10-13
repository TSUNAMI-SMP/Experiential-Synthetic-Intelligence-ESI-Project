"""Capture startup failures even before Qt/the inference libraries can import."""

import ctypes
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path


def notify_failure(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "ESI-02 起動エラー", 0x10)


def main(root=None, launch=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    logs = root / "data/esi02/logs"
    path = logs / "bootstrap.log"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        log_file = path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        notify_failure(f"ESI-02の起動記録を保存できません。フォルダーへの書込みを確認してください。\n{exc}")
        return 1
    with log_file as log, redirect_stdout(log), redirect_stderr(log):
        print(f"\n{datetime.now(UTC).isoformat()} ESI-02 startup / Python {sys.version.split()[0]}")
        try:
            if launch is None:
                from esi02.launcher import main as launch
            return launch(root)
        except Exception:
            traceback.print_exc()
            message = f"ESI-02を起動できませんでした。記録を保存しました。\n{path}"
            notify_failure(message)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
