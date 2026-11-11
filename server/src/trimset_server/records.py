"""record/1 and question/1 as the server checks them (FR-01, FR-02).

These mirror docs/03-api/schemas/{record,question}.schema.json. They are strict: a
probability sent as "0.5", a boolean where a number belongs, or a timestamp without a
time zone is refused, never coerced.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

QuestionType = Literal["noul", "choice", "score"]
SUM_TOLERANCE = 1e-3  # wire-formats.md §3


def _uuid(value: Any) -> uuid.UUID:
    if not isinstance(value, str):
        raise ValueError("must be a UUID string")
    return uuid.UUID(value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be an RFC 3339 date-time string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("needs a time zone")
    return parsed


Uuid = Annotated[uuid.UUID, BeforeValidator(_uuid)]
Timestamp = Annotated[datetime, BeforeValidator(_timestamp)]
Number = StrictFloat | StrictInt
Text = Annotated[StrictStr, Field(min_length=1, max_length=256)]
TagKey = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
TagValue = Annotated[StrictStr, Field(max_length=256)]


class RecordIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    schema_: Literal["record/1"] = Field(alias="schema")
    decision_id: Uuid
    request_id: Uuid | None = None
    ts: Timestamp
    question_id: Annotated[StrictStr, Field(pattern=r"^q_[0-9a-f]{32}$")]
    question_name: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    type: QuestionType
    format: Literal["systemone", "openai-decisions", "direct"] = "direct"
    model_requested: Text
    model_served: Text
    probabilities: list[Number] | None
    refusal: StrictBool = False
    latency_ms: Number | None = None
    cost_usd: Number | None = None
    policy_version: Annotated[StrictInt, Field(ge=1)] | None = None
    policy_fingerprint: Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    action_taken: (
        Annotated[StrictStr, Field(pattern=r"^(ACCEPT|BLOCK|ESCALATE|SELECT:.{1,256})$")] | None
    ) = None
    action_source: Literal["policy", "audit", "default"] | None = None
    audit: StrictBool = False
    tags: Annotated[dict[TagKey, TagValue], Field(max_length=20)] = {}
    external_ref: Text | None = None
    state_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    state_hash_alg: Literal["hmac-sha256/1"] | None = None
    role: Literal["primary", "shadow"] = "primary"
    shadow_of: Uuid | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "RecordIn":
        if self.refusal != (self.probabilities is None):
            raise ValueError("a refusal has no probabilities, and anything else has them")
        if self.probabilities is not None:
            p = self.probabilities
            limit = {"noul": 1, "choice": 255, "score": 10}[self.type]
            if not 1 <= len(p) <= limit:
                raise ValueError(f"a {self.type} record has 1 to {limit} probabilities")
            if not all(math.isfinite(x) and 0 <= x <= 1 for x in p):
                raise ValueError("probabilities must lie in [0, 1]")
        for name, ceiling in (("latency_ms", 1e9), ("cost_usd", 1e9)):
            value = getattr(self, name)
            if value is not None and not (math.isfinite(value) and 0 <= value <= ceiling):
                raise ValueError(f"{name} must be a number from 0 to {ceiling:g}")
        if (self.role == "shadow") != (self.shadow_of is not None):
            raise ValueError("a shadow decision needs shadow_of, and only a shadow has one")
        return self


class Noul(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)
    true: Any = Field(alias="true")
    false: Any = Field(alias="false")


class Option(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: StrictStr | StrictBool
    description: Any


class Level(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    label: StrictStr | None
    description: Any


class QuestionIn(BaseModel):
    """question/1: the canonical form whose hash is the question_id."""

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    schema_: Literal["question/1"] = Field(alias="schema")
    type: QuestionType
    instructions: Any
    noul: Noul | None = None
    options: Annotated[list[Option], Field(min_length=1, max_length=255)] | None = None
    levels: Annotated[list[Level], Field(min_length=1, max_length=10)] | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> "QuestionIn":
        present = {k for k in ("noul", "options", "levels") if getattr(self, k) is not None}
        wanted = {"noul": "noul", "choice": "options", "score": "levels"}[self.type]
        if present != {wanted}:
            raise ValueError(f"a {self.type} question has `{wanted}` and nothing else of its kind")
        return self

    def size(self) -> int:
        """How many probabilities a record for this question carries."""
        if self.type == "noul":
            return 1
        return len(self.options or self.levels or [])


Label = StrictBool | Annotated[StrictStr, Field(min_length=1, max_length=256)] | StrictInt


def fits(question: QuestionIn, label: Any) -> bool:
    """Whether `label` is a possible answer to `question`: a boolean for noul, one of the
    options for choice (true and "true" are different options), a level index for score."""
    if question.type == "noul":
        return isinstance(label, bool)
    if question.type == "choice":
        return any(
            type(o.value) is type(label) and o.value == label for o in question.options or []
        )
    levels = len(question.levels or [])
    return isinstance(label, int) and not isinstance(label, bool) and 0 <= label < levels


class OutcomeIn(BaseModel):
    """outcome/1 (FR-10): names a decision by decision_id, or by external_ref and question."""

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    schema_: Literal["outcome/1"] = Field(alias="schema")
    outcome_id: Uuid | None = None
    decision_id: Uuid | None = None
    external_ref: Text | None = None
    question: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    label: Label
    source: Literal["human_review", "downstream", "rule"]
    observed_at: Timestamp

    @model_validator(mode="after")
    def _one_reference(self) -> "OutcomeIn":
        by_id = self.decision_id is not None
        by_ref = self.external_ref is not None and self.question is not None
        if by_id == by_ref or (by_id and (self.external_ref or self.question)):
            raise ValueError("name the decision by decision_id, or by external_ref and question")
        return self


class ActionIn(BaseModel):
    """An action report (FR-33): what the app did with a decision logged earlier."""

    model_config = ConfigDict(strict=True, extra="forbid")

    decision_id: Uuid
    action_taken: Annotated[StrictStr, Field(pattern=r"^(ACCEPT|BLOCK|ESCALATE|SELECT:.{1,256})$")]
    action_source: Literal["policy", "audit", "default"]
    reason: (
        Literal[
            "no_policy",
            "model_mismatch",
            "model_unknown",
            "question_changed",
            "invalid_probabilities",
            "fingerprint_mismatch",
            "stale_policy",
            "refusal",
        ]
        | None
    ) = None
    policy_version: Annotated[StrictInt, Field(ge=1)] | None = None
    policy_fingerprint: Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    audit: StrictBool = False
