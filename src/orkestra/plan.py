"""Runtime types for the orchestra engine.

These are *not* persisted config — they are the vocabulary the four roles
exchange during a run:

- sef produces a :class:`SefPlan` of :class:`MicroTask` pieces.
- hamal turns each piece into a JSON output.
- kalfa returns a :class:`KalfaVerdict` per attempt.
- the run aggregates :class:`PieceResult` objects into the report dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MicroTask(BaseModel):
    """One muhakemesiz micro-task handed to a hamal.

    Attributes:
        id: unique slug inside the plan (e.g. ``t-014``).
        instruction: the narrow instruction for the worker.
        input: structured input payload the instruction refers to.
        output_schema: JSON Schema the output must validate against. The
            ``x-from-input`` extension on a property marks it as a citation:
            its value must be a member of ``input[<name>]``.
        acceptance: natural-language criteria checked by the kalfa (stage-2
            arbitration on the strong model).
        budget_tokens: per-call ``max_tokens`` cap for this piece.
        max_retries: how many times a failed piece is retried on cheap
            models before escalating to the strong model.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any]
    acceptance: list[str] = Field(default_factory=list)
    budget_tokens: int | None = Field(default=None, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("output_schema")
    @classmethod
    def _schema_is_object_typed(cls, value: dict[str, Any]) -> dict[str, Any]:
        # The hamal transport always parses replies into a dict, so a schema
        # typed as anything else (e.g. "array") can never pass — reject it at
        # plan time, where the sef can still repair the plan.
        if value.get("type") != "object":
            raise ValueError(
                'output_schema must declare "type": "object" '
                "(worker replies are JSON objects)"
            )
        return value


class SefPlan(BaseModel):
    """The sef's decomposition: a non-empty list of unique-id pieces."""

    model_config = ConfigDict(extra="forbid")

    pieces: list[MicroTask] = Field(min_length=1)

    @field_validator("pieces")
    @classmethod
    def _unique_ids(cls, value: list[MicroTask]) -> list[MicroTask]:
        ids = [piece.id for piece in value]
        if len(ids) != len(set(ids)):
            raise ValueError("piece ids must be unique")
        return value


class KalfaVerdict(BaseModel):
    """The kalfa's decision on one worker output.

    Wire format uses ``pass`` (the design document's key); code uses
    ``passed`` via the alias.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    passed: bool = Field(alias="pass")
    reasons: list[str] = Field(default_factory=list)
    fix_hint: str | None = None


class PieceStatus(StrEnum):
    """Final state of a micro-task after the hamal/kalfa loop."""

    PASSED = "passed"
    ESCALATED_PASSED = "escalated"  # passed only after promotion to strong
    FAILED = "failed"
    CANCELLED = "cancelled"  # never ran: the run was aborted first


@dataclass
class Attempt:
    """One hamal (or escalated strong-model) execution + verdict."""

    number: int
    model: str  # registry name
    tier: str  # "cheap" | "strong"
    passed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class PieceResult:
    """Everything the run knows about one micro-task."""

    task: MicroTask
    status: PieceStatus
    output: dict[str, Any] | None
    attempts: list[Attempt] = field(default_factory=list)
    unchecked_acceptance: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the piece produced a validated output."""
        return self.status in (PieceStatus.PASSED, PieceStatus.ESCALATED_PASSED)
