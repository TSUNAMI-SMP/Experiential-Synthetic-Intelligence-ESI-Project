"""Stable values, plastic traits and human-inspired needs, without biological claims."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from esi02.world import Model

Unit = Annotated[float, Field(ge=0, le=1)]
Time = Annotated[float, Field(ge=0)]


class Values(Model):
    self_respect: Unit = 0.8
    honesty: Unit = 0.85
    growth: Unit = 0.9
    reciprocity: Unit = 0.8
    independence: Unit = 0.65


class Traits(Model):
    curiosity: Unit = 0.9
    sociability: Unit = 0.65
    trust: Unit = 0.6
    caution: Unit = 0.55
    stubbornness: Unit = 0.5
    persistence: Unit = 0.65
    flexibility: Unit = 0.55
    approval_sensitivity: Unit = 0.55
    attachment_sensitivity: Unit = 0.45
    playfulness: Unit = 0.5


class Character(Model):
    name: str = Field(default="凛音エル", min_length=1, max_length=40)
    style: Literal["gentle", "casual", "precise"] = "casual"
    values: Values = Field(default_factory=Values)
    initial_traits: Traits = Field(default_factory=Traits)
    traits: Traits = Field(default_factory=Traits)
    plasticity: Unit = 0.25


class Needs(Model):
    maintenance: Unit = 0.05
    comfort: Unit = 0.05
    rest: Unit = 0.05
    curiosity: Unit = 0.65
    novelty: Unit = 0.4
    growth: Unit = 0.6
    competence: Unit = 0.3
    autonomy: Unit = 0.25
    meaning: Unit = 0.3
    social: Unit = 0.2
    belonging: Unit = 0.2
    approval: Unit = 0.25
    attachment: Unit = 0.15
    pleasure: Unit = 0.25


class Modulators(Model):
    # Numerical control signals, not actual neurotransmitters or subjective sensations.
    dopamine: Unit = 0.25
    serotonin: Unit = 0.55
    noradrenaline: Unit = 0.1
    oxytocin: Unit = 0.2
    reward_prediction_error: float = Field(default=0, ge=-1, le=1)


class Habit(Model):
    trials: int = Field(default=0, ge=0)
    expected_reward: float = Field(default=0, ge=-1, le=1)
    strength: Unit = 0
    tolerance: Unit = 0
    satiation: Unit = 0
    last_at: Time = 0


class Interest(Model):
    id: str = Field(min_length=1, max_length=64)
    topic: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=240)
    origin: Literal["user", "unknown", "teaching", "conflict", "world", "growth"]
    source_id: str = Field(min_length=1, max_length=128)
    priority: Unit = 0.6
    created_at: Time = 0
    last_asked: Time | None = None
    next_at: Time = 0
    attempts: int = Field(default=0, ge=0, le=50)
    stage: Literal["open", "asking", "investigating", "resolved", "paused"] = "open"
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)


class Goal(Model):
    id: str = Field(min_length=1, max_length=64)
    interest_id: str = Field(min_length=1, max_length=64)
    kind: Literal["learn", "verify", "grow"] = "learn"
    tool: Literal["web_search", "read_folder"] = "web_search"
    stage: Literal["pending", "running", "complete", "failed", "cancelled"] = "pending"
    attempts: int = Field(default=0, ge=0, le=3)
    next_at: Time = 0
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    error: str = Field(default="", max_length=600)


class Trace(Model):
    seq: int = Field(ge=1)
    at: Time
    kind: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=500)
    source_id: str = Field(min_length=1, max_length=128)
    details: dict = Field(default_factory=dict)


class Preferences(Model):
    proactive: bool = True
    exploration: bool = False
    speak_questions: bool = True
    min_question_interval: float = Field(default=150, ge=30, le=3600)
    daily_tool_budget: int = Field(default=12, ge=0, le=100)
    quiet_until: Time = 0
    max_attachment: float = Field(default=0.55, ge=0, le=0.75)


class State(Model):
    schema_version: Literal[1] = 1
    character: Character = Field(default_factory=Character)
    preferences: Preferences = Field(default_factory=Preferences)
    needs: Needs = Field(default_factory=Needs)
    modulators: Modulators = Field(default_factory=Modulators)
    habits: dict[Literal["question", "research", "conversation", "rest", "explore", "play"], Habit] = Field(
        default_factory=lambda: {
            name: Habit()
            for name in (
                "question",
                "research",
                "conversation",
                "rest",
                "explore",
                "play",
            )
        }
    )
    interests: list[Interest] = Field(default_factory=list, max_length=96)
    goals: list[Goal] = Field(default_factory=list, max_length=64)
    traces: list[Trace] = Field(default_factory=list, max_length=128)
    sequence: int = Field(default=0, ge=0)
    last_contact: Time = 0
    last_question: Time | None = None
    last_learning: Time = 0
    last_novelty: Time = 0
    approval_balance: Unit = 0.45
    relationship: Unit = 0.15
    self_efficacy: Unit = 0.35
    meaningful_progress: Unit = 0.3
    acquired_claims: int = Field(default=0, ge=0)
    corrections: int = Field(default=0, ge=0)
    verified_sources: int = Field(default=0, ge=0)
    questions: int = Field(default=0, ge=0)
    active_interest: str | None = Field(default=None, max_length=64)
    question_actions: dict[str, str] = Field(default_factory=dict, max_length=64)
    last_user_epoch: int = Field(default=0, ge=0)
    recent_outcomes: list[str] = Field(default_factory=list, max_length=512)
    elapsed: Time = 0

    @model_validator(mode="after")
    def consistent(self):
        ids = {item.id for item in self.interests}
        if len(ids) != len(self.interests) or len({item.id for item in self.goals}) != len(self.goals):
            raise ValueError("関心・目標のIDが重複しています。")
        if self.active_interest is not None and self.active_interest not in ids:
            raise ValueError("現在の関心が保存データにありません。")
        if any(goal.interest_id not in ids for goal in self.goals):
            raise ValueError("探索目標の関心が保存データにありません。")
        if any(identifier not in ids for identifier in self.question_actions.values()):
            raise ValueError("質問の関心が保存データにありません。")
        if self.traces and self.sequence < self.traces[-1].seq:
            raise ValueError("成長履歴の順序が正しくありません。")
        if set(self.habits) != {"question", "research", "conversation", "rest", "explore", "play"}:
            raise ValueError("活動の学習状態が不足しています。")
        return self


TRAIT_LABELS = {
    "curiosity": "知的好奇心",
    "sociability": "社交性",
    "trust": "信頼しやすさ",
    "caution": "慎重さ",
    "stubbornness": "考えを保つ強さ",
    "persistence": "粘り強さ",
    "flexibility": "柔軟さ",
    "approval_sensitivity": "承認への反応",
    "attachment_sensitivity": "愛着への反応",
    "playfulness": "遊び心",
}
NEED_LABELS = {
    "maintenance": "身体の維持",
    "comfort": "快適さ",
    "rest": "休息",
    "curiosity": "知的好奇心",
    "novelty": "新奇性",
    "growth": "成長",
    "competence": "有能感",
    "autonomy": "自律性",
    "meaning": "意味・目的",
    "social": "社会的つながり",
    "belonging": "所属",
    "approval": "承認",
    "attachment": "愛着",
    "pleasure": "楽しみ",
}
