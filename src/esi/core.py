"""Numerical ESI-01 core. No network, microphone or GUI code runs here."""

import copy
import math
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from esi import RELEASE, __version__
from esi.learning import Predictor
from esi.models import (
    Action,
    ActionKind,
    Candidate,
    Interpretation,
    Observation,
    Outcome,
    Settings,
    Source,
    State,
    uid,
)
from esi.storage import Store

SPEECH = {ActionKind.INQUIRE, ActionKind.SPEAK}
COMPUTE_COST = {
    ActionKind.WAIT: 0,
    ActionKind.REPROCESS: 0.04,
    ActionKind.THINK: 0.06,
    ActionKind.INQUIRE: 0.04,
    ActionKind.SPEAK: 0.04,
    ActionKind.REST: 0,
}


class Core:
    def __init__(self, database: str | Path, settings: Settings | None = None, read_only=False):
        self.lock = threading.RLock()
        self.store = Store(database, read_only=read_only)
        try:
            self._initialize(settings, read_only)
        except Exception:
            self.store.close()
            raise

    def _initialize(self, settings, read_only):
        saved = self.store.load_snapshot()
        stored_settings = self.store.meta("settings")
        self.settings = settings or Settings.model_validate(stored_settings or {})
        if (
            stored_settings
            and Settings.model_validate(stored_settings).fingerprint != self.settings.fingerprint
        ):
            raise ValueError(
                "Configuration differs from saved experiment. Create a branch before changing it."
            )
        self.store.max_records = self.settings.max_records
        self.agent_id = self.store.meta("agent_id")
        self.branch_id = self.store.meta("branch_id")
        self.state = State()
        self.elapsed = 0.0
        self.context_version = 0
        self.last_speech = -1e12
        self.learning_enabled = self.settings.learning
        self.rng = np.random.default_rng(self.settings.seed)
        self.predictor = Predictor(self.settings.ridge)
        self.anchor = Predictor(self.settings.ridge)
        self.training = OrderedDict()
        self.events = OrderedDict()
        self.workspace = []
        self.actions = OrderedDict()
        self.interpretations = {}
        self.status = {
            "core": "ready",
            "llm": "disabled" if self.settings.llm_mode == "off" else "not_checked",
            "asr": "not_checked",
            "tts": "not_checked",
            "microphone": "not_started",
        }
        if saved:
            self._restore(saved["state"])
            # A crash can leave commands after the last checkpoint. Reapply input commands without network calls.
            tail = self.store.records("command", after=saved["seq"], limit=self.settings.max_records + 1)
            for record in tail:
                self.execute(record["payload"], record=False)
        else:
            if read_only:
                raise ValueError("No saved state in this database")
            self.store.set_meta("settings", self.settings.model_dump())
            self.checkpoint(initial=True)

    def _command(self, name, data, record=True):
        if record:
            self.store.append("command", {"name": name, "data": data})

    def _emit(self, kind, data, record=True):
        if record:
            self.store.append(kind, {"elapsed": self.elapsed, **data})

    def _restore(self, data):
        if data["release"] != RELEASE or data["config_hash"] != self.settings.fingerprint:
            raise ValueError("Snapshot release/configuration mismatch")
        self.state = State.model_validate(data["state"])
        self.elapsed = data["elapsed"]
        self.context_version = data["context_version"]
        self.last_speech = data["last_speech"]
        self.learning_enabled = data["learning_enabled"]
        self.rng.bit_generator.state = data["rng"]
        self.predictor.restore(data["predictor"])
        self.anchor.restore(data.get("anchor", data["predictor"]))
        self.training = OrderedDict(data.get("training", []))
        self.events = OrderedDict((e["event_id"], Observation.model_validate(e)) for e in data["events"])
        self.workspace = list(data["workspace"])
        self.actions = OrderedDict((a["action_id"], Action.model_validate(a)) for a in data["actions"])
        self.interpretations = {
            k: Interpretation.model_validate(v) for k, v in data.get("interpretations", {}).items()
        }

    def dump(self):
        with self.lock:
            return {
                "release": RELEASE,
                "code_version": __version__,
                "config_hash": self.settings.fingerprint,
                "state": self.state.model_dump(),
                "elapsed": self.elapsed,
                "context_version": self.context_version,
                "last_speech": self.last_speech,
                "learning_enabled": self.learning_enabled,
                "rng": copy.deepcopy(self.rng.bit_generator.state),
                "predictor": self.predictor.dump(),
                "anchor": self.anchor.dump(),
                "training": copy.deepcopy(list(self.training.items())),
                "events": [e.model_dump() for e in self.events.values()],
                "workspace": list(self.workspace),
                "actions": [a.model_dump() for a in self.actions.values()],
                "interpretations": {k: v.model_dump() for k, v in self.interpretations.items()},
            }

    def checkpoint(self, initial=False):
        with self.lock:
            return self.store.snapshot(self.dump(), initial=initial)

    def tick(self, dt: float, record=True):
        with self.lock:
            if not math.isfinite(dt) or dt < 0:
                raise ValueError("dt must be finite and nonnegative")
            self._command("tick", {"dt": dt}, record)
            self.elapsed += dt
            self.state.arousal *= math.exp(-dt / self.settings.decay_arousal)
            resting = any(a.kind == ActionKind.REST and a.stage == "selected" for a in self.actions.values())
            self.state.load *= math.exp(-dt * (1.5 if resting else 1) / self.settings.decay_load)
            self.state.habituation = {
                k: v * math.exp(-dt / self.settings.decay_habituation)
                for k, v in self.state.habituation.items()
            }
            for a in list(self.actions.values()):
                if a.stage == "selected" and a.kind not in SPEECH and self.elapsed - a.start_elapsed >= 0.5:
                    self.finish(a.action_id, "complete", record=False, trace=record)
                elif (
                    a.stage not in {"complete", "cancelled", "failed", "unknown"}
                    and self.elapsed >= a.deadline_elapsed
                ):
                    self.finish(a.action_id, "unknown", record=False, trace=record)
            self._emit("state", {"state": self.state.model_dump()}, record)

    def observe(self, event: Observation, record=True):
        with self.lock:
            if event.event_id in self.events:
                raise ValueError("Duplicate observation ID")
            self._command("observe", event.model_dump(), record)
            self.events[event.event_id] = event
            external = event.source in {Source.MICROPHONE, Source.RECORDING, Source.SIMULATED}
            if external:
                self.context_version += 1
                contacts = self.state.habituation.get(event.category, 0)
                novelty = 1 / (1 + contacts)
                self.state.habituation[event.category] = min(1000.0, contacts + 1)
                self.state.arousal = min(1.0, self.state.arousal + 0.25 * novelty)
                self.state.load = min(1.0, self.state.load + 0.05 + min(event.duration, 15) / 100)
                if "?" in event.text or "？" in event.text:
                    self.state.questions[event.event_id] = (
                        "Question punctuation detected (estimated; unresolved)"
                    )
                self.state.unresolved = min(1.0, len(self.state.questions) / 4)
                self.workspace = (self.workspace + [event.event_id])[-self.settings.workspace_capacity :]
                self._emit(
                    "workspace",
                    {
                        "selected_ids": list(self.workspace),
                        "reason": "new external observation; oldest capacity item replaced",
                    },
                    record,
                )
            while len(self.events) > self.settings.short_term_capacity:
                old, _ = self.events.popitem(last=False)
                self.interpretations.pop(old, None)
                self.workspace = [k for k in self.workspace if k != old]
            # Bounded live questions: shelving reduces load but is not resolution.
            if len(self.state.questions) > self.settings.short_term_capacity:
                old = next(iter(self.state.questions))
                del self.state.questions[old]
                self._emit("question_shelved", {"event_id": old, "resolved": False}, record)
            self._emit("observation", event.model_dump(), record)

    def context(self, event_id=None):
        with self.lock:
            ids = list(self.workspace) if self.settings.use_workspace else []
            ids = ids[-4:]  # Bound language input independently of numeric/log storage.
            observations = [
                {
                    "event_id": e.event_id,
                    "source": e.source.value,
                    "text": e.text[:320],
                    "text_truncated": len(e.text) > 320,
                    "duration": round(e.duration, 2),
                    "acoustics": e.acoustic_summary if self.settings.use_acoustics else {},
                    "missing": e.missing,
                }
                for k in ids
                if (e := self.events.get(k))
            ]
            event = self.events.get(event_id)
            text = event.text if event and self.settings.use_workspace else ""
            memory = self.retrieve(text) if self.settings.use_workspace else []
            for m in memory:
                m["text_truncated"] = len(m["text"]) > 180
                m["text"] = m["text"][:180]
            return {
                "observations": observations,
                "memories": memory,
                "state": {
                    "arousal": round(self.state.arousal, 3),
                    "load": round(self.state.load, 3),
                    "unresolved": round(self.state.unresolved, 3),
                    "questions": [q[:128] for q in list(self.state.questions.values())[-4:]],
                },
                "workspace_ids": ids,
                "purpose": "interpret observations; estimates are not facts",
            }

    def retrieve(self, text, limit=4):
        grams = {text[i : i + 2] for i in range(max(0, len(text) - 1))}
        matches = []
        for e in self.store.recent_observations(limit=200):
            if e["event_id"] in self.workspace or e["source"] == Source.SELF_OUTPUT:
                continue
            other = {e["text"][i : i + 2] for i in range(max(0, len(e["text"]) - 1))}
            similarity = len(grams & other) / max(1, len(grams | other))
            if similarity:
                matches.append(
                    (
                        similarity,
                        {
                            "event_id": e["event_id"],
                            "text": e["text"],
                            "source": e["source"],
                            "similarity": similarity,
                            "kind": "recorded_observation",
                        },
                    )
                )
        return [e for _, e in sorted(matches, key=lambda pair: pair[0], reverse=True)[:limit]]

    def interpret(self, event_id, result: Interpretation, expected_version=None, record=True):
        with self.lock:
            if expected_version is not None and expected_version != self.context_version:
                self._emit(
                    "stale_llm",
                    {
                        "event_id": event_id,
                        "expected_version": expected_version,
                        "current_version": self.context_version,
                    },
                    record,
                )
                return False
            context = self.context(event_id)
            valid = set(context["workspace_ids"]) | {m["event_id"] for m in context["memories"]}
            refs = set(result.referenced_event_ids) | {i for h in result.hypotheses for i in h.event_ids}
            if event_id not in self.events or not refs <= valid:
                raise ValueError("LLM references unavailable observations")
            self._command("interpret", {"event_id": event_id, "result": result.model_dump()}, record)
            self.interpretations[event_id] = result
            if result.unresolved_question:
                self.state.questions[event_id] = result.unresolved_question
                self.state.unresolved = min(1.0, len(self.state.questions) / 4)
            self._emit(
                "llm_hypothesis",
                {"event_id": event_id, "result": result.model_dump(), "observed_truth": False},
                record,
            )
            return True

    def features(self, event_id=None):
        event = self.events.get(event_id) if self.settings.use_workspace else None
        summary = event.acoustic_summary if event and self.settings.use_acoustics else {}
        rms = summary.get("rms")
        f0 = summary.get("f0_hz")
        interpretation = self.interpretations.get(event_id) if self.settings.use_workspace else None
        category = interpretation.category if interpretation else "unknown"
        return [
            1.0,
            self.state.arousal,
            self.state.load,
            self.state.unresolved,
            (event.experiment_cue or 0.0) if event else 0.0,
            min(1.0, event.duration / 15) if event else 0.0,
            min(1.0, rms * 10) if rms is not None else 0.0,
            min(1.0, f0 / 500) if f0 is not None else 0.0,
            float(rms is None),
            float(f0 is None),
        ] + [float(category == c) for c in ("unknown", "question", "statement", "request", "correction")]

    def candidates(self, event_id=None):
        x = self.features(event_id)
        event = self.events.get(event_id)
        contacts = self.state.habituation.get(event.category, 0) if event else 0
        proposal = self.interpretations.get(event_id)
        candidates = []
        for a in ActionKind:
            if a in SPEECH and event_id is None:
                continue
            if a in SPEECH and self.elapsed - self.last_speech < self.settings.speech_interval:
                continue
            if a in SPEECH and any(
                v.kind in SPEECH and v.stage not in {"complete", "failed", "cancelled", "unknown"}
                for v in self.actions.values()
            ):
                continue
            prediction = np.clip(self.predictor.predict(a, x), [-1, 0, -1], [1, 1, 1]).tolist()
            contributions = {
                "recovery": (0.15 + self.state.load) * prediction[0],
                "evidence": (0.2 + 1.3 * self.state.unresolved) * prediction[1],
                "progress": 0.25 * prediction[2],
                "novelty": (0.04 / (1 + contacts)) if a in {ActionKind.THINK, ActionKind.REPROCESS} else 0.0,
                "llm_suggestion": 0.01 if proposal and a in proposal.suggestions else 0.0,
                "compute_cost": -COMPUTE_COST[a] * (1 + self.state.load),
                "output_cost": -0.12 if a in SPEECH else 0.0,
            }
            candidates.append(
                Candidate(
                    kind=a,
                    predictions=prediction,
                    contributions=contributions,
                    score=sum(contributions.values()),
                )
            )
        return candidates

    def choose(self, event_id=None, forced: ActionKind | None = None, action_id=None, record=True):
        with self.lock:
            if event_id is not None and event_id not in self.events:
                raise ValueError("Unknown event ID")
            candidates = self.candidates(event_id)
            if forced and forced not in {c.kind for c in candidates}:
                raise ValueError("Requested measurement action currently unavailable")
            aid = action_id or uid()
            if aid in self.actions:
                raise ValueError("Duplicate action ID")
            self._command(
                "choose",
                {"event_id": event_id, "forced": forced.value if forced else None, "action_id": aid},
                record,
            )
            explored = not forced and self.rng.random() < self.settings.exploration
            kind = forced or (
                candidates[int(self.rng.integers(len(candidates)))].kind
                if explored
                else max(candidates, key=lambda c: c.score).kind
            )
            action = Action(
                action_id=aid,
                kind=kind,
                event_id=event_id,
                start_elapsed=self.elapsed,
                context_version=self.context_version,
                deadline_elapsed=self.elapsed + self.settings.outcome_deadline,
                features=self.features(event_id),
                start_state=self.state.model_copy(deep=True),
                candidates=candidates,
                used_workspace_ids=list(self.workspace) if self.settings.use_workspace else [],
                explored=bool(explored),
                exploration_probability=self.settings.exploration if not forced else 0.0,
            )
            self.actions[aid] = action
            self.state.load = min(1.0, self.state.load + COMPUTE_COST[kind])
            if kind in SPEECH:
                self.last_speech = self.elapsed
            if kind == ActionKind.REPROCESS:
                memories = self.retrieve(self.events[event_id].text if event_id else "")
                self._emit(
                    "recall",
                    {
                        "action_id": aid,
                        "memory_ids": [m["event_id"] for m in memories],
                        "is_new_external_evidence": False,
                    },
                    record,
                )
            while len(self.actions) > 256:
                oldest = next(iter(self.actions))
                if self.actions[oldest].stage not in {"complete", "failed", "cancelled", "unknown"}:
                    self.finish(oldest, "unknown", record=False, trace=record)
                self.actions.pop(oldest)
            self._emit(
                "decision",
                {
                    "action": action.model_dump(),
                    "measurement_forced": forced is not None,
                    "rng_after": self.rng.bit_generator.state,
                },
                record,
            )
            return action

    def stage(self, action_id, stage, record=True):
        with self.lock:
            action = self.actions[action_id]
            if action.stage in {"complete", "failed", "cancelled", "unknown"}:
                return False
            if stage not in {"generating", "synthesizing", "playing"}:
                raise ValueError("Invalid intermediate stage")
            self._command("stage", {"action_id": action_id, "stage": stage}, record)
            action.stage = stage
            self._emit("action_stage", {"action_id": action_id, "stage": stage}, record)
            return True

    def finish(self, action_id, stage, text=None, audio_ref=None, record=True, trace=True):
        with self.lock:
            action = self.actions[action_id]
            if stage not in {"complete", "failed", "cancelled", "unknown"}:
                raise ValueError("Invalid terminal stage")
            if action.stage in {"complete", "failed", "cancelled", "unknown"}:
                return False
            self._command(
                "finish",
                {"action_id": action_id, "stage": stage, "text": text, "audio_ref": audio_ref},
                record,
            )
            action.stage = stage
            action.text, action.audio_ref = text, audio_ref
            # The actual pseudo-load change is measurable; other components stay unknown.
            recovery = max(-1.0, min(1.0, action.start_state.load - self.state.load))
            outcome = Outcome(
                action_id=action_id,
                recovery=recovery,
                provenance="state_measurement",
                ambiguous=self.context_version != action.context_version,
                note="Measured pseudo-state change, not causal proof; evidence/progress unknown",
            )
            self.feedback(outcome, record=False, trace=trace)
            self._emit(
                "action_stage",
                {"action_id": action_id, "stage": stage, "text": text, "audio_ref": audio_ref},
                trace,
            )
            return True

    def feedback(self, outcome: Outcome, record=True, trace=True):
        with self.lock:
            action = self.actions.get(outcome.action_id)
            if action is None:
                raise ValueError("Feedback must refer to an available action_id")
            if outcome.provenance not in {
                "research_feedback",
                "experiment_ground_truth",
                "verified_holdout",
                "state_measurement",
            }:
                raise ValueError("Imagined, self-output and LLM-scored outcomes cannot train the predictor")
            if outcome.provenance == "state_measurement" and (
                outcome.evidence is not None or outcome.progress is not None
            ):
                raise ValueError("State measurements cannot establish external evidence/progress")
            if outcome.progress is not None and outcome.provenance != "verified_holdout":
                raise ValueError("Progress requires a held-out measurement")
            if outcome.resolve_event_id and outcome.resolve_event_id not in (
                {action.event_id} | set(action.used_workspace_ids)
            ):
                raise ValueError("Resolution must reference an observation used by this action")
            self._command("feedback", outcome.model_dump(), record)
            values = [outcome.recovery, outcome.evidence, outcome.progress]
            before = self.predictor.predict(action.kind, action.features).tolist()
            changes = []
            if self.learning_enabled and not outcome.ambiguous and any(v is not None for v in values):
                sample = self.training.get(
                    action.action_id,
                    {"kind": action.kind.value, "features": action.features, "values": [None, None, None]},
                )
                correction = action.action_id in self.training
                sample["values"] = [
                    new if new is not None else old for new, old in zip(values, sample["values"])
                ]
                self.training[action.action_id] = sample
                if correction:
                    # Rebuild from the bounded accepted history so corrected labels are not counted twice.
                    self.predictor.restore(self.anchor.dump())
                    for s in self.training.values():
                        changes = self.predictor.update(ActionKind(s["kind"]), s["features"], s["values"])
                    after = self.predictor.predict(action.kind, action.features).tolist()
                    changes = [
                        {
                            "dimension": i,
                            "target": v,
                            "before": before[i],
                            "after": after[i],
                            "correction_rebuilt": True,
                        }
                        for i, v in enumerate(values)
                        if v is not None
                    ]
                else:
                    changes = self.predictor.update(action.kind, action.features, values)
                if len(self.training) > 10_000:
                    _, oldest = self.training.popitem(last=False)
                    self.anchor.update(ActionKind(oldest["kind"]), oldest["features"], oldest["values"])
                # Observation-only proxy; not fed back into reward or score/features.
                errors = [v - before[i] for i, v in enumerate(values) if v is not None]
                self.state.valence_proxy = float(
                    np.clip(0.9 * self.state.valence_proxy + 0.1 * np.mean(errors), -1, 1)
                )
            if (
                outcome.evidence is not None
                and outcome.evidence > 0
                and outcome.resolve_event_id
                and not outcome.ambiguous
            ):
                self.state.questions.pop(outcome.resolve_event_id, None)
                self.state.unresolved = min(1.0, len(self.state.questions) / 4)
            self._emit(
                "outcome",
                {
                    "outcome": outcome.model_dump(),
                    "learning_enabled": self.learning_enabled,
                    "changes": changes,
                    "before": before,
                    "after": self.predictor.predict(action.kind, action.features).tolist(),
                },
                trace,
            )
            return changes

    def learning(self, enabled, record=True):
        with self.lock:
            self._command("learning", {"enabled": bool(enabled)}, record)
            self.learning_enabled = bool(enabled)
            self._emit("learning_switch", {"enabled": self.learning_enabled}, record)

    def execute(self, command, record=False):
        name, data = command["name"], command["data"]
        if name == "tick":
            self.tick(data["dt"], record=record)
        elif name == "observe":
            self.observe(Observation.model_validate(data), record=record)
        elif name == "interpret":
            self.interpret(data["event_id"], Interpretation.model_validate(data["result"]), record=record)
        elif name == "choose":
            self.choose(
                data["event_id"],
                ActionKind(data["forced"]) if data["forced"] else None,
                data["action_id"],
                record=record,
            )
        elif name == "finish":
            self.finish(**data, record=record, trace=record)
        elif name == "stage":
            self.stage(**data, record=record)
        elif name == "feedback":
            self.feedback(Outcome.model_validate(data), record=record, trace=record)
        elif name == "learning":
            self.learning(data["enabled"], record=record)
        else:
            raise ValueError("Unknown replay command")

    def branch(self, destination, **changes):
        with self.lock:
            self.checkpoint()
            path = self.store.fork(destination)
            other = Store(path)
            parent = self.branch_id
            other.set_meta("branch_id", uid())
            other.set_meta("parent_branch_id", parent)
            if changes:
                configured = Settings.model_validate({**self.settings.model_dump(), **changes})
                other.set_meta("settings", configured.model_dump())
                snapshot = other.load_snapshot()["state"]
                snapshot["config_hash"] = configured.fingerprint
                snapshot["learning_enabled"] = configured.learning
                sid = other.snapshot(snapshot)
                other.set_meta("replay_base", sid)
                other.append("configuration_branch", {"parent": parent, "changes": changes})
            other.close()
            return path

    def view(self):
        with self.lock:
            external = next(
                (
                    e
                    for e in reversed(self.events.values())
                    if e.source in {Source.MICROPHONE, Source.RECORDING, Source.SIMULATED}
                ),
                None,
            )
            output = next((e for e in reversed(self.events.values()) if e.source == Source.SELF_OUTPUT), None)
            action = next(
                (a for a in reversed(self.actions.values()) if external and a.event_id == external.event_id),
                None,
            )
            return {
                "release": RELEASE,
                "agent_id": self.agent_id,
                "branch_id": self.branch_id,
                "elapsed": self.elapsed,
                "state": self.state.model_dump(),
                "learning": self.learning_enabled,
                "workspace": [self.events[k].model_dump() for k in self.workspace if k in self.events],
                "actions": [a.model_dump() for a in list(self.actions.values())[-12:]],
                "status": dict(self.status),
                "latest_input": external.model_dump() if external else None,
                "latest_output": output.model_dump() if output else None,
                "input_action": action.model_dump() if action else None,
                "counts": copy.deepcopy(self.predictor.counts),
            }

    def close(self):
        if not self.store.read_only:
            self.checkpoint()
        self.store.close()


def replay(source, destination):
    """Strict replay reuses recorded LLM results and commands, not new model calls."""
    if not Path(source).is_file():
        raise FileNotFoundError("Replay source does not exist")
    store = Store(source, read_only=True)
    try:
        snapshot = store.load_snapshot(store.meta("replay_base"))
        if snapshot is None:
            raise ValueError("No replay boundary")
        config = Settings.model_validate(store.meta("settings"))
        if Path(destination).exists():
            raise FileExistsError("Replay destination exists")
        target = Core(destination, config)
        target._restore(snapshot["state"])
        target.store.set_meta("agent_id", store.meta("agent_id"))
        target.agent_id = store.meta("agent_id")
        target.store.set_meta("parent_branch_id", store.meta("branch_id"))
        sid = target.checkpoint()
        target.store.set_meta("replay_base", sid)
        commands = store.records("command", after=snapshot["seq"], limit=config.max_records + 1)
        for item in commands:
            target.execute(item["payload"], record=True)
        target.checkpoint()
        return target
    finally:
        store.close()
