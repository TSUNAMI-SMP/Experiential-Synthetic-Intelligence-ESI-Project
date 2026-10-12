"""Controlled arbitrary-cue task with known outcomes, separate from autonomous behavior."""

import json
from pathlib import Path

import numpy as np

from esi.core import Core
from esi.models import ActionKind, Observation, Outcome, Settings, utc

TASK_ACTIONS = (ActionKind.REPROCESS, ActionKind.INQUIRE)


def known_evidence(cue, action):
    return (0.5 + 0.4 * cue) if action == ActionKind.REPROCESS else (0.5 - 0.4 * cue)


def measure(core):
    """Temporary evaluation core: no updates and no writes to the live database."""
    initial = core.dump()
    errors, correct = [], []
    # Distinct held-out magnitudes; training uses -1/+1, evaluation uses these values.
    cues = [-0.85, -0.65, -0.35, 0.35, 0.65, 0.85]
    for cue in cues:
        event = Observation(text="held-out arbitrary signal", category="holdout", experiment_cue=cue)
        core.observe(event, record=False)
        predictions = [
            float(np.clip(core.predictor.predict(a, core.features(event.event_id))[1], 0, 1))
            for a in TASK_ACTIONS
        ]
        truths = [known_evidence(cue, a) for a in TASK_ACTIONS]
        errors.extend((p - y) ** 2 for p, y in zip(predictions, truths))
        correct.append(int(np.argmax(predictions) == np.argmax(truths)))
    core._restore(initial)
    return {
        "mse": float(np.mean(errors)),
        "choice_accuracy": float(np.mean(correct)),
        "trials": len(cues),
        "answers": "measurement pathway, not autonomous choices",
        "updated": False,
        "short_term_state_used": True,
        "long_term_state_written_back": False,
    }


def compare(directory, trials=80, seed=1):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if trials < 8:
        raise ValueError("Need at least eight training trials")
    reports = {}
    # Identical stimulus and selected-action schedule, learning is the only difference.
    for enabled, label in [(True, "learning"), (False, "frozen")]:
        path = directory / f"{label}.sqlite3"
        if path.exists():
            raise FileExistsError("Experiment already exists; choose a new directory")
        core = Core(
            path, Settings(seed=seed, llm_mode="off", exploration=0, speech_interval=0, learning=enabled)
        )
        baseline = measure(core)
        curve = []
        initial_weights = core.predictor.dump()
        try:
            for index in range(trials):
                cue = -1.0 if index % 4 < 2 else 1.0
                action = TASK_ACTIONS[index % 2]
                event = Observation(
                    text=f"novel signal {index % 4}", category="controlled_signal", experiment_cue=cue
                )
                core.observe(event)
                selected = core.choose(event.event_id, forced=action)
                core.feedback(
                    Outcome(
                        action_id=selected.action_id,
                        evidence=known_evidence(cue, action),
                        provenance="experiment_ground_truth",
                        note="Known outcome of arbitrary-cue measurement task",
                    )
                )
                core.finish(selected.action_id, "complete")
                core.tick(0.5)
                if (index + 1) % 8 == 0:
                    curve.append({"training_trials": index + 1, **measure(core)})
            reports[label] = {
                "baseline": baseline,
                "heldout": measure(core),
                "curve": curve,
                "weights_changed": core.predictor.dump() != initial_weights,
                "observed_components": ["pseudo_state_recovery", "experiment_evidence"],
                "progress_teacher": "none",
                "model": "action-specific masked ridge RLS",
            }
        finally:
            core.close()
    report = {
        "release": "ESI-01",
        "created": utc(),
        "task": "arbitrary-cue-v1",
        "seed": seed,
        "training_trials_per_condition": trials,
        "training_cues": [-1, 1],
        "heldout_cues": [-0.85, -0.65, -0.35, 0.35, 0.65, 0.85],
        "conditions": reports,
        "interpretation": "Controlled prediction learning; not an IQ, consciousness or free-conversation measurement",
    }
    (directory / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def evaluate(database, temporary_directory):
    """Evaluate a live checkpoint clone; never change the source experiment."""
    import sqlite3

    source = sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro", uri=True)
    target_path = Path(temporary_directory) / "evaluation.sqlite3"
    if target_path.exists():
        raise FileExistsError("Evaluation destination exists")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    core = Core(target_path)
    try:
        core.learning(False)
        return measure(core)
    finally:
        core.close()
