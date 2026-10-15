"""Portable selection of the bundled character; custom file paths stay separate."""

from pathlib import Path

BUNDLED_MODEL = "builtin:rinne-el"


def bundled_model_path():
    return Path(__file__).resolve().parents[1] / "esi/resources/rinne-el/rinne-el.model3.json"


def model_file(selection):
    if selection == BUNDLED_MODEL:
        return bundled_model_path()
    if selection == "builtin:rinne-el04":
        return Path(__file__).resolve().parents[1] / "esi/resources/rinne-el04/rinne-el.model3.json"
    return Path(selection) if selection else None
