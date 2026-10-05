"""Budget valve: per-call usage ledger with USD and token limits.

Every LLM call in a run is recorded as a :class:`UsageRecord`. When a USD
budget is configured, every model participating in the run must carry cost
hints — otherwise the valve cannot work and the run refuses to start
(loud failure, never silent non-enforcement).

When a provider omits ``usage``, tokens are estimated (~4 chars/token) and
the record is flagged ``estimated=True`` so the report stays honest.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass

from orkestra.chat import estimate_tokens
from orkestra.errors import BudgetExceededError
from orkestra.schema import ModelConfig


@dataclass
class UsageRecord:
    """One LLM call's usage entry.

    Attributes:
        role: sef | hamal | kalfa | birlestirici.
        model: registry model name.
        model_id: remote id sent to the provider.
        piece_id: micro-task id for hamal/kalfa calls, else None.
        attempt: attempt number within the piece, else None.
        prompt_tokens / completion_tokens: reported or estimated counts.
        cost_usd: estimated USD cost, or None when the model has no hints.
        estimated: True when token counts are heuristic, not provider-reported.
    """

    role: str
    model: str
    model_id: str
    piece_id: str | None
    attempt: int | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    estimated: bool


def estimate_cost_usd(
    model: ModelConfig,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """USD estimate for one call, or None when the model has no cost hints."""
    cost = model.cost
    if cost is None or cost.input_per_1m is None or cost.output_per_1m is None:
        return None
    return (
        prompt_tokens * cost.input_per_1m + completion_tokens * cost.output_per_1m
    ) / 1_000_000.0


class UsageLedger:
    """Thread-safe record of all calls in a run, plus the budget valve.

    ``record()`` is called after every LLM call (workers run in parallel).
    ``ensure_within_budget()`` is called *before* every call, so an
    exhausted budget stops the next call instead of being discovered late.
    """

    def __init__(
        self,
        *,
        budget_usd: float | None = None,
        token_budget: int | None = None,
    ) -> None:
        self.budget_usd = budget_usd
        self.token_budget = token_budget
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(
        self,
        *,
        role: str,
        model: ModelConfig,
        model_id: str,
        piece_id: str | None,
        attempt: int | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        prompt_text: str,
        completion_text: str,
    ) -> UsageRecord:
        """Store one call's usage; estimate tokens when the provider omits them.

        Returns the stored :class:`UsageRecord`.
        """
        estimated = prompt_tokens is None or completion_tokens is None
        p_tok = prompt_tokens if prompt_tokens is not None else estimate_tokens(prompt_text)
        c_tok = (
            completion_tokens
            if completion_tokens is not None
            else estimate_tokens(completion_text)
        )
        entry = UsageRecord(
            role=role,
            model=model.name,
            model_id=model_id,
            piece_id=piece_id,
            attempt=attempt,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_usd=estimate_cost_usd(model, p_tok, c_tok),
            estimated=estimated,
        )
        with self._lock:
            self._records.append(entry)
        return entry

    def ensure_within_budget(self) -> None:
        """Raise :class:`BudgetExceededError` when a configured cap is crossed."""
        if self.budget_usd is not None and self.total_cost_usd >= self.budget_usd:
            raise BudgetExceededError(
                f"budget exhausted: ${self.total_cost_usd:.4f} >= ${self.budget_usd:.4f}"
            )
        if self.token_budget is not None and self.total_tokens >= self.token_budget:
            raise BudgetExceededError(
                f"token budget exhausted: {self.total_tokens} >= {self.token_budget}"
            )

    @property
    def total_prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self.records)

    @property
    def total_completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.records)

    @property
    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.records)

    @property
    def total_cost_usd(self) -> float:
        """Sum of known costs; calls without hints contribute 0."""
        return sum(r.cost_usd or 0.0 for r in self.records)

    @property
    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records)

    def as_report(self) -> dict[str, object]:
        """Usage section of the run report."""
        records = self.records
        return {
            "calls": [asdict(r) for r in records],
            "totals": {
                "prompt_tokens": self.total_prompt_tokens,
                "completion_tokens": self.total_completion_tokens,
                "cost_usd": round(self.total_cost_usd, 6),
                "estimated_calls": sum(1 for r in records if r.estimated),
            },
            "budget_usd": self.budget_usd,
            "token_budget": self.token_budget,
        }
