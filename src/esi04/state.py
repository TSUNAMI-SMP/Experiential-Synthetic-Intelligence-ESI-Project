"""Inspectable summaries and pending dialogue, distinct from private model reasoning."""

from typing import Literal

from pydantic import Field

from esi02.world import Model
from esi03.personality import Preferences as PreviousPreferences
from esi03.personality import State as PreviousState


class Preferences(PreviousPreferences):
    # Retained for old config import; the adaptive scheduler never uses a fixed interval.
    min_question_interval: float = Field(default=0, ge=0, le=3600)
    quiet_until: float = Field(default=0, ge=0)
    idle_activity: bool = True
    autonomous_motion: bool = True
    cursor_tracking: bool = False
    thought_bubbles: bool = True


class Question(Model):
    id: str
    text: str = Field(max_length=300)
    topic: str = Field(max_length=120)
    reason: str = Field(max_length=300)
    interest_id: str | None = None
    action_id: str | None = None
    asked_at: float = Field(ge=0)
    stage: Literal["pending", "answered", "uncertain", "declined", "deferred", "unobserved", "cancelled"] = "pending"
    reply_turn: str | None = None
    reply_quote: str = Field(default="", max_length=2000)


class ExperienceState(PreviousState):
    preferences: Preferences = Field(default_factory=Preferences)
    dialogue_questions: list[Question] = Field(default_factory=list, max_length=64)
    availability: Literal["unknown", "present", "busy", "unobserved"] = "unknown"
    question_contexts: dict[str, str] = Field(default_factory=dict, max_length=96)


class Summary(Model):
    id: str
    at: float = Field(ge=0)
    activity: str = Field(max_length=60)
    subject: str = Field(max_length=160)
    reason: str = Field(max_length=400)
    alternatives: list[str] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    outcome: str = Field(default="", max_length=400)
    certainty: Literal["observed", "inferred", "planned"] = "planned"


class Cognition(Model):
    summaries: list[Summary] = Field(default_factory=list, max_length=128)
    processed: list[str] = Field(default_factory=list, max_length=512)
    activity: str = "待機"
    attention: str = "相手"
    attention_object: str | None = None
    intention: Literal["listen", "think", "inspect", "ask", "rest", "explore", "operate", "play", "enjoy"] = "listen"
    emotion: Literal["neutral", "interested", "pleased", "uncertain", "surprised", "tired", "concerned"] = "neutral"
    emotion_strength: float = Field(default=0.2, ge=0, le=1)
    active_object: str | None = None
    object_action: str | None = None
    object_started: float = Field(default=0, ge=0)
    processed_revision: int = Field(default=-1, ge=-1)
    deliberations: int = Field(default=0, ge=0)
    pending_computer_task: str = Field(default="", max_length=1000)
