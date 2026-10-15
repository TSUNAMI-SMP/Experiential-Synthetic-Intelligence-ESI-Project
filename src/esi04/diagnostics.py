"""Readiness only: no screenshots, captions, app titles, typed text or taught skills in reports."""

from pathlib import Path

from esi.models import uid
from esi02.assets import verify_file
from esi02.avatar_assets import model_file
from esi02.desktop_preferences import read_preferences, write_json
from esi02.diagnostics import collect_report as collect_previous
from esi02.diagnostics import sanitize
from esi04 import __version__
from esi04.session import inspect_database
from esi04.setup_assets import VISION_FILES


def collect_report(root, settings, database=None, selections=None, observation=None, executable=None,
                   verify_models=False, check_services=True, vision_backend=None):
    report = collect_previous(root, settings, database, selections, observation, executable, verify_models,
                              check_services, save_inspector=inspect_database, data_directory="esi04",
                              desktop_filename="esi04-desktop.json")
    report.update(release="ESI-04", version=__version__, format_version=3)
    report["requested"].update({key: getattr(settings, key) for key in
                                ("flash_attention", "cache_type", "asr_compute_type", "vision_device",
                                 "vision_context_tokens", "idle_deliberation")})
    files = []
    for configured, (name, size, digest) in zip(
            (settings.vision_model_path, settings.vision_projector_path), VISION_FILES, strict=True):
        path = Path(root) / configured
        entry = {"name": name, "present": path.is_file(), "expected_bytes": size, "sha256_checked": False}
        if path.is_file():
            entry["bytes"] = path.stat().st_size
            if verify_models:
                entry.update(sha256_checked=True, sha256_matches=verify_file(path, digest, None))
        files.append(entry)
    report["checks"]["vision_assets"] = files
    report["observations"]["vision"] = {key: (vision_backend or {}).get(key) for key in
                                        ("device", "verified", "inference_verified", "offloaded_layers", "source")}
    report["computer"] = {"grants_restored_on_start": False, "screen_contents_included": False,
                          "actual_input_checked": False, "windows_demo_checked": False}
    if database:
        try:
            display = read_preferences(Path(database).parent / "esi04-desktop.json")
            report["checks"]["avatar"]["model_file_present"] = bool(display.model_path and model_file(display.model_path).is_file())
        except (OSError, ValueError):
            report["checks"]["avatar"].update(state="invalid_preferences", model_file_present=False)
    report["model_format"] = {"language_weights": "Q4_K_M", "vision_weights": "Q4_K_M", "nvfp4": False}
    return sanitize(report, root)


def save_report(root, report, destination=None):
    path = Path(destination) if destination else Path(root) / "data/esi04/diagnostics" / ("diagnostic-"+uid()+".json")
    write_json(path, report)
    return path
