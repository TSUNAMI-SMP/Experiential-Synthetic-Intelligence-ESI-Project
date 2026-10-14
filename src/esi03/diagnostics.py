"""Hardware/readiness diagnostics exclude the character's words, beliefs and documents."""

from pathlib import Path

from esi.models import uid
from esi02.desktop_preferences import write_json
from esi02.diagnostics import collect_report as collect_previous
from esi02.diagnostics import sanitize
from esi03 import __version__
from esi03.session import inspect_database


def collect_report(
    root,
    settings,
    database=None,
    selections=None,
    observation=None,
    executable=None,
    verify_models=False,
    check_services=True,
):
    report = collect_previous(
        root,
        settings,
        database,
        selections,
        observation,
        executable,
        verify_models,
        check_services,
        save_inspector=inspect_database,
        data_directory="esi03",
        desktop_filename="esi03-desktop.json",
    )
    report.update(release="ESI-03", version=__version__, format_version=2)
    report["requested"].update(
        {key: getattr(settings, key) for key in ("flash_attention", "cache_type", "asr_compute_type")}
    )
    report["model_format"] = {"weights": "Q4_K_M", "nvfp4": False}
    return sanitize(report, root)


def save_report(root, report, destination=None):
    path = (
        Path(destination)
        if destination
        else Path(root) / "data/esi03/diagnostics" / ("diagnostic-" + uid() + ".json")
    )
    write_json(path, report)
    return path
