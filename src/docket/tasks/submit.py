"""Task result models and submission checks, M1 form (TD §9.4).

Order: schema (tolerant coercions declared on the model), identity partition
(integrity, refuses), vocabulary (shape, tolerated with a safe reading).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from docket.cli.errors import InputError, IntegrityError
from docket.shapes.unexpected import unexpected

VERDICTS = ("ask", "drop", "merge", "needs_verification")


class AssessRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(description="The pack item id, copied exactly.")
    keep: bool = Field(description="true to keep the item for a decision.")
    score: int = Field(ge=0, le=100, description="0..100; higher means more worth the operator's attention.")


class AssessResult(BaseModel):
    """Result of an ``assess`` task: one row per pack item."""
    model_config = ConfigDict(extra="ignore")
    items: list[AssessRow]


class ReviewRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(description="The pack item id, copied exactly.")
    verdict: Literal["ask", "drop", "merge", "needs_verification"] = Field(
        description="ask: put it to the operator; drop: not worth asking; merge: duplicate of another item; "
                    "needs_verification: unsure.")
    reason: str | None = None

    @field_validator("verdict", mode="before")
    @classmethod
    def _tolerant_verdict(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in VERDICTS:
            return v.strip().lower()
        unexpected("task.vocabulary", "review.verdict unknown value", sample=str(v)[:80],
                   reading="needs_verification", extend="docket/tasks/submit.py: VERDICTS")
        return "needs_verification"


class ReviewResult(BaseModel):
    """Result of a ``review`` task: one row per pack item."""
    model_config = ConfigDict(extra="ignore")
    items: list[ReviewRow]


RESULT_MODELS: dict[str, type[BaseModel]] = {"assess": AssessResult, "review": ReviewResult}
RESULT_SCHEMAS: dict[str, str] = {"assess": "assess-result", "review": "review-result"}


def validate_result(kind: str, payload: Any, pack_ids: list[str]) -> BaseModel:
    model = RESULT_MODELS[kind]
    try:
        result = model.model_validate(payload)
    except ValidationError as e:
        rows = [{"path": ".".join(str(x) for x in err["loc"]), "received": err.get("input"), "rule": err["msg"]}
                for err in e.errors()]
        raise InputError(
            "task.invalid_result", f"The {kind} result does not match its schema ({len(rows)} error(s)).",
            hint=f"docket schema {RESULT_SCHEMAS[kind]}", details={"errors": rows[:50]},
        ) from None
    ids = [row.id for row in result.items]  # type: ignore[attr-defined]
    counts = Counter(ids)
    dup = sorted(i for i, c in counts.items() if c > 1)
    missing = sorted(set(pack_ids) - set(ids))
    extra = sorted(set(ids) - set(pack_ids))
    if dup or missing or extra:
        raise IntegrityError(
            "integrity.partition",
            f"The result must cover exactly the pack's {len(pack_ids)} item ids "
            f"(missing {len(missing)}, extra {len(extra)}, duplicated {len(dup)}).",
            hint="Return one row per pack item id, copied exactly from pack.json",
            details={"missing": missing, "extra": extra, "duplicates": dup},
        )
    return result
