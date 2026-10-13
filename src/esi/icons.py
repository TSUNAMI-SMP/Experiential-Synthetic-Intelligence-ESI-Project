"""Small bundled SVG icons: readable without platform emoji fonts."""

from pathlib import Path

from PySide6.QtGui import QIcon

RESOURCES = Path(__file__).with_name("resources")


def icon(name):
    return QIcon(str(RESOURCES / "icons" / f"{name}.svg"))


def activity_symbol(view, phase):
    if phase == "decided" and view.get("input_action"):
        action = view["input_action"]["kind"]
        return {
            "rest": "rest",
            "wait": "wait",
            "reprocess": "reprocess",
            "think": "think",
            "inquire": "speak",
            "speak": "speak",
        }[action]
    return {
        "stopped": "stop",
        "error": "error",
        "waiting": "wait",
        "queued": "wait",
        "listening": "microphone",
        "recognizing": "ear",
        "interpreting": "think",
        "generating": "think",
        "synthesizing": "speak",
        "playing": "speak",
    }.get(phase, "wait")
