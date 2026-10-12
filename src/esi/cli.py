import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from esi import RELEASE, __version__
from esi.core import Core, replay
from esi.experiments import compare, evaluate
from esi.models import Observation, Settings
from esi.runtime import Runtime


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor(settings):
    results = {
        "release": RELEASE,
        "python": sys.version,
        "asr_model_present": Path(settings.asr_model_path, "model.bin").is_file(),
        "llm_mode": settings.llm_mode,
        "playback": settings.playback,
    }
    for name, url, endpoint in [
        ("llm", settings.llm_url, "/health"),
        ("tts", settings.aivis_url, "/version"),
    ]:
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=3) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                results[name] = {"status": "reachable", "response": response.json()}
                if name == "tts":
                    from esi.adapters import TTS

                    results[name]["voice"] = TTS(settings).resolve_voice(client)
        except Exception as exc:
            results[name] = {"status": "failed", "error": str(exc)}
    try:
        import sounddevice as sd

        results["audio_devices"] = str(sd.query_devices())
        results["audio_device_count"] = len(sd.query_devices())
    except Exception as exc:
        results["audio_error"] = str(exc)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESI-01 — 体験型合成知能の研究用実行・観察")
    parser.add_argument("--version", action="version", version=f"{RELEASE} ({__version__})")
    parser.add_argument("--config", help="TOML 設定。再開時は保存設定を使用できます")
    parser.add_argument("--database", default="data/esi-01.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="観察画面")
    run = sub.add_parser("run", help="常駐処理（Ctrl+C で保存終了）")
    run.add_argument("--microphone", action="store_true")
    run.add_argument("--audio", help="録音ファイル入力")
    run.add_argument("--seconds", type=float, default=0, help="0 は停止操作まで継続")
    simulation = sub.add_parser("simulate", help="モデルなしの模擬観測・状態更新")
    simulation.add_argument("--text", default="未知の音は次にも鳴る？")
    simulation.add_argument("--steps", type=int, default=20)
    sub.add_parser("doctor", help="モデル・音声機器の接続診断")
    voice = sub.add_parser("install-voice", help="検証済み凛音エル AIVMX を AivisSpeech に導入")
    voice.add_argument("--file", default="models/aivis/f5017410-fbb5-49e1-97cb-e785f42e15f5.aivmx")
    sub.add_parser("status", help="保存済み状態")
    study = sub.add_parser("compare", help="既知の模擬課題で学習あり／停止を比較")
    study.add_argument("--output", required=True)
    study.add_argument("--trials", type=int, default=80)
    study.add_argument("--seed", type=int, default=1)
    test = sub.add_parser("evaluate", help="保存状態のコピーで留保課題を測定")
    test.add_argument("--output", required=True)
    branch = sub.add_parser("branch", help="現在状態を別実験に複製")
    branch.add_argument("--output", required=True)
    branch.add_argument("--freeze", action="store_true")
    branch.add_argument("--no-acoustics", action="store_true")
    branch.add_argument("--no-workspace", action="store_true")
    strict = sub.add_parser("replay", help="記録済みモデル出力を使う厳密な処理再生")
    strict.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        settings = Settings.read(args.config) if args.config else None
        if args.command == "install-voice":
            import hashlib

            from esi.adapters import TTS

            configured = settings or Settings()
            path = Path(args.file)
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != configured.tts_model_sha256:
                raise ValueError("Voice model SHA-256 differs from the pinned experiment configuration")
            with httpx.Client(base_url=configured.aivis_url, trust_env=False, timeout=90) as client:
                existing = client.get(f"/aivm_models/{configured.tts_model_uuid}")
                if existing.status_code == 404:
                    with path.open("rb") as stream:
                        response = client.post(
                            "/aivm_models/install",
                            files={"file": (path.name, stream, "application/octet-stream")},
                        )
                    response.raise_for_status()
                    state = "installed"
                else:
                    existing.raise_for_status()
                    state = "already_installed"
                output({"status": state, "sha256": digest, "voice": TTS(configured).resolve_voice(client)})
            return 0
        if args.command == "doctor":
            result = doctor(settings or Settings())
            output(result)
            return (
                0
                if result["asr_model_present"]
                and all(result[n]["status"] == "reachable" for n in ("llm", "tts"))
                else 1
            )
        if args.command == "compare":
            output(compare(args.output, args.trials, args.seed))
            return 0
        if args.command == "evaluate":
            output(evaluate(args.database, args.output))
            return 0
        if args.command == "replay":
            core = replay(args.database, args.output)
            output(core.view())
            core.close()
            return 0
        if args.command == "simulate":
            settings = (settings or Settings()).model_copy(update={"llm_mode": "off"})
        core = Core(args.database, settings, read_only=args.command == "status")
        if args.command == "gui":
            from esi.gui import launch

            return launch(core)
        try:
            if args.command == "status":
                output(core.view())
            elif args.command == "branch":
                changes = {}
                if args.freeze:
                    changes["learning"] = False
                if args.no_acoustics:
                    changes["use_acoustics"] = False
                if args.no_workspace:
                    changes["use_workspace"] = False
                output({"branch": str(core.branch(args.output, **changes))})
            elif args.command == "simulate":
                event = Observation(text=args.text, category="simulation")
                core.observe(event)
                action = core.choose(event.event_id)
                for _ in range(args.steps):
                    core.tick(0.2)
                output(
                    {
                        "mode": "model-free simulation; no microphone or generated speech",
                        "selected": action.model_dump(),
                        "view": core.view(),
                    }
                )
            elif args.command == "run":
                runtime = Runtime(core)
                runtime.start(args.microphone)
                if args.audio:
                    runtime.send("recording", args.audio)
                start = time.monotonic()
                try:
                    while not args.seconds or time.monotonic() - start < args.seconds:
                        time.sleep(0.2)
                        if core.status["core"].startswith("failed"):
                            raise RuntimeError(core.status["core"])
                except KeyboardInterrupt:
                    pass
                finally:
                    runtime.stop()
                output(core.view())
                if any(str(v).startswith("failed") for v in core.status.values()):
                    return 1
        finally:
            core.close()
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, httpx.HTTPError) as exc:
        parser.exit(1, f"ESI-01: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
