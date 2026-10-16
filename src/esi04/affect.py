"""Bounded, recoverable affect appraisals; these are simulation control signals."""

import math
from typing import Literal

from pydantic import Field

from esi02.world import Model

Emotion = Literal["neutral", "interested", "pleased", "uncertain", "surprised", "tired", "concerned",
                  "anxious", "impatient", "angry"]
NEGATIVE = {"anxious": "anxiety", "impatient": "agitation", "angry": "anger"}
LABELS = {"anxiety": "不安", "agitation": "焦り", "anger": "怒り"}


class Affect(Model):
    anxiety: float = Field(default=0, ge=0, le=.45)
    agitation: float = Field(default=0, ge=0, le=.45)
    anger: float = Field(default=0, ge=0, le=.45)
    reason: str = Field(default="", max_length=100)
    source_turn: str = Field(default="", max_length=64)


def recover(cognition, seconds):
    affect = cognition.affect
    for name in LABELS:
        value = getattr(affect, name)*math.exp(-max(0, seconds)/90)
        setattr(affect, name, 0 if value < .01 else value)
    if cognition.emotion in NEGATIVE:
        value = getattr(affect, NEGATIVE[cognition.emotion])
        cognition.emotion_strength = value
        if value < .08:
            cognition.emotion, cognition.emotion_strength = "neutral", .2
    if not any(getattr(affect, name) for name in LABELS):
        affect.reason, affect.source_turn = "", ""


def appraise(cognition, emotion, reason, turn_id, recovering=False):
    if emotion not in Emotion.__args__:
        return
    affect = cognition.affect
    previous_total = sum(getattr(affect, name) for name in LABELS)
    for name in LABELS:
        setattr(affect, name, getattr(affect, name)*.6)
    if emotion in NEGATIVE:
        name = NEGATIVE[emotion]
        if recovering:
            for key in LABELS:
                setattr(affect, key, 0.0)
            setattr(affect, name, min(.45, previous_total*.6))
        else:
            setattr(affect, name, min(.45, getattr(affect, name)+.22))
        affect.reason, affect.source_turn = reason[:100], turn_id
        cognition.emotion_strength = getattr(affect, name)
    else:
        cognition.emotion_strength = .45 if emotion != "neutral" else .2
    cognition.emotion = emotion


def sustain(cognition):
    strength, emotion = max((getattr(cognition.affect, name), emotion) for emotion, name in NEGATIVE.items())
    if strength >= .12:
        cognition.emotion, cognition.emotion_strength = emotion, strength
