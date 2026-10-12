import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from esi import RELEASE


def uid() -> str:
    return uuid.uuid4().hex


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class Strict(BaseModel):
    _transport_trace: dict | None = PrivateAttr(default=None)
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False
    )


class Settings(Strict):
    release: str = RELEASE
    seed: int = 1
    tick_seconds: float = Field(default=0.15, gt=0, le=1)
    decay_arousal: float = Field(default=20, gt=0)
    decay_load: float = Field(default=12, gt=0)
    decay_habituation: float = Field(default=86400, gt=0)
    workspace_capacity: int = Field(default=4, ge=1, le=16)
    short_term_capacity: int = Field(default=32, ge=4, le=256)
    exploration: float = Field(default=0.05, ge=0, le=0.2)
    speech_interval: float = Field(default=8, ge=0)
    outcome_deadline: float = Field(default=60, gt=0)
    ridge: float = Field(default=1, gt=0)
    learning: bool = True
    use_acoustics: bool = True
    use_workspace: bool = True
    llm_mode: str = "assist"
    llm_url: str = "http://127.0.0.1:8080"
    llm_model: str = "Qwen3-4B"
    llm_timeout: float = Field(default=60, gt=0)
    llm_tokens: int = Field(default=160, ge=32, le=512)
    aivis_url: str = "http://127.0.0.1:10101"
    tts_model_uuid: str = "f5017410-fbb5-49e1-97cb-e785f42e15f5"
    tts_model_name: str = "凛音エル"
    tts_model_version: str = "1.0.0"
    tts_model_sha256: str = "e7ac9e2636f59d6f6512016d0752f9e9f89c2b3ffd2012a6a1b92791330e23bc"
    tts_speaker_uuid: str = "d2c99ca6-73e5-486c-994e-ee0ce2d74928"
    tts_style_name: str = "ノーマル"
    asr_model_path: str = "models/faster-whisper-small"
    cpu_threads: int = Field(default=2, ge=1, le=16)
    playback: bool = False
    vad_end_ms: int = Field(default=750, ge=200, le=2000)
    utterance_max_seconds: float = Field(default=12, ge=2, le=15)
    recording_max_seconds: float = Field(default=120, ge=1, le=600)
    audio_queue_capacity: int = Field(default=64, ge=16, le=512)
    command_queue_capacity: int = Field(default=128, ge=16, le=1024)
    audio_limit_bytes: int = Field(default=1_000_000_000, ge=1_000_000)
    max_records: int = Field(default=200_000, ge=1000)
    checkpoint_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def check_mode(self):
        if self.llm_mode not in {"off", "assist", "verbalize_only"}:
            raise ValueError("llm_mode must be off, assist or verbalize_only")
        if self.release != RELEASE:
            raise ValueError(f"Unsupported release: {self.release}")
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def read(cls, path: str | Path | None):
        if path is None:
            return cls()
        import tomllib

        return cls.model_validate(tomllib.loads(Path(path).read_text(encoding="utf-8")))


class Source(StrEnum):
    MICROPHONE = "microphone"
    RECORDING = "recording"
    SIMULATED = "simulated"
    SELF_OUTPUT = "self_output"
    RECALL = "recall"
    FEEDBACK = "research_feedback"


class AcousticFrame(Strict):
    seconds: float = Field(ge=0)
    rms: float = Field(ge=0)
    normalized_rms: float | None = None
    f0_hz: float | None = Field(default=None, gt=0)
    voiced: bool = False


class Observation(Strict):
    event_id: str = Field(default_factory=uid)
    source: Source = Source.SIMULATED
    wall_start: str = Field(default_factory=utc)
    wall_end: str = Field(default_factory=utc)
    mono_start: float = Field(default=0, ge=0)
    mono_end: float = Field(default=0, ge=0)
    duration: float = Field(default=0, ge=0)
    text: str = Field(default="", max_length=8192)
    transcript_alternatives: list[str] = Field(default_factory=list)
    audio_ref: str | None = None
    acoustic_frames: list[AcousticFrame] = Field(default_factory=list)
    acoustic_summary: dict[str, float | None] = Field(default_factory=dict)
    category: str = "unknown"
    # An experiment's arbitrary cue, never an emotion label.
    experiment_cue: float | None = Field(default=None, ge=-1, le=1)
    estimators: dict[str, str] = Field(default_factory=dict)
    estimates: dict[str, float | None] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_times(self):
        if self.mono_end < self.mono_start:
            raise ValueError("mono_end precedes mono_start")
        return self


class ActionKind(StrEnum):
    WAIT = "wait"
    REPROCESS = "reprocess"
    THINK = "think"
    INQUIRE = "inquire"
    SPEAK = "speak"
    REST = "rest"


class State(Strict):
    arousal: float = Field(default=0.1, ge=0, le=1)
    load: float = Field(default=0.1, ge=0, le=1)
    unresolved: float = Field(default=0, ge=0, le=1)
    habituation: dict[str, float] = Field(default_factory=dict)
    questions: dict[str, str] = Field(default_factory=dict)
    valence_proxy: float = Field(default=0, ge=-1, le=1)


class Hypothesis(Strict):
    text: str = Field(max_length=500)
    event_ids: list[str] = Field(max_length=4)


class Interpretation(Strict):
    referenced_event_ids: list[str] = Field(max_length=4)
    category: Literal["unknown", "question", "statement", "request", "correction"]
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=4)
    suggestions: list[ActionKind] = Field(default_factory=list, max_length=6)
    unresolved_question: str | None = Field(default=None, max_length=500)


class SpeechPlan(Strict):
    text: str = Field(min_length=1, max_length=500)
    referenced_event_ids: list[str] = Field(max_length=4)


class Candidate(Strict):
    kind: ActionKind
    predictions: list[float]
    contributions: dict[str, float]
    score: float


class Action(Strict):
    action_id: str
    kind: ActionKind
    event_id: str | None
    start_elapsed: float
    context_version: int = 0
    deadline_elapsed: float
    features: list[float]
    start_state: State
    candidates: list[Candidate]
    used_workspace_ids: list[str]
    explored: bool = False
    exploration_probability: float = 0
    stage: str = "selected"
    text: str | None = None
    audio_ref: str | None = None


class Outcome(Strict):
    action_id: str
    recovery: float | None = Field(default=None, ge=-1, le=1)
    evidence: float | None = Field(default=None, ge=0, le=1)
    progress: float | None = Field(default=None, ge=-1, le=1)
    # Only observed entries update weights. Unknown is None, never zero.
    provenance: str = "research_feedback"
    resolve_event_id: str | None = None
    ambiguous: bool = False
    note: str = Field(default="", max_length=500)
