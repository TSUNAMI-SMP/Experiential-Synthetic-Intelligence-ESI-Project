"""Background connection preparation with rollback to the still-open save."""

from dataclasses import dataclass


@dataclass
class PreparationResult:
    stopped: bool = False
    ready: bool = False
    recovered: bool = True
    error: str = ""


def restore_services(services, settings):
    if services is None:
        return ""
    try:
        services.cancel.clear()
        services.prepare(settings)
        return ""
    except Exception as exc:
        return f"元のセーブの接続を戻せません：{exc}"
    finally:
        services.cancel.clear()


def prepare_save(old, candidate, runtime, services, cancel, notify):
    result = PreparationResult()
    changed_services = False
    original_notify = services.notify if services else None
    try:
        if services:
            services.notify = notify
            services.cancel.clear()
        if cancel.is_set():
            raise RuntimeError("準備を中止しました。")
        if runtime:
            notify("会話を停止しています…")
            try:
                runtime.stop()
            except Exception as exc:
                raise RuntimeError(f"動作を停止できないため、セーブを保持しました：{exc}") from exc
        result.stopped = True
        old.checkpoint()  # A failed final save must leave the current Session usable.
        if cancel.is_set():
            raise RuntimeError("準備を中止しました。")
        if services:
            changed_services = True
            notify("選んだセーブの接続を準備しています…")
            services.prepare(candidate.settings)
        if cancel.is_set():
            raise RuntimeError("準備を中止しました。")
        candidate.checkpoint()
        result.ready = True
    except Exception as exc:
        result.error = str(exc)
        if changed_services:
            notify("元のセーブの接続を戻しています…")
            recovery_error = restore_services(services, old.settings)
            result.recovered = not recovery_error
            if recovery_error:
                result.error += "\n" + recovery_error
    finally:
        if services:
            services.notify = original_notify
            services.cancel.clear()
    return result
