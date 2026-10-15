"""Capture startup errors before Qt or native inference modules can load."""

import ctypes
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path


def failure(message):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "ESI-04 起動エラー", 0x10)


def main(root=None, launch=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    path = root / "data/esi04/logs/bootstrap.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        failure("起動記録を保存できません。フォルダーへの書込みを確認してください。\n" + str(exc))
        return 1
    with stream as log, redirect_stdout(log), redirect_stderr(log):
        print(datetime.now(UTC).isoformat() + " ESI-04 startup / Python " + sys.version.split()[0])
        try:
            if launch is None:
                from esi04.launcher import main as launch
            return launch(root)
        except Exception:
            traceback.print_exc()
            failure("ESI-04を起動できませんでした。記録: " + str(path))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
