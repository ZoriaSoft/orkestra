"""The orchestra engine: sef -> hamal pool -> kalfa -> birlestirici.

``Orchestra.run(task)`` executes one task end to end and returns a report
dict. Roles and their models:

- **sef** (strong tier): decomposes the task into :class:`MicroTask` pieces.
- **hamal** (cheap tier pool): executes pieces in parallel under a strict
  JSON schema; reasoning is forbidden, "unknown" beats fabrication.
- **kalfa** (code + strong tier): deterministic checks first (schema,
  citation membership); acceptance criteria needing judgement go to the
  strong model. A failed piece retries at most ``max_retries`` times, then
  escalates to the strong model once.
- **birlestirici** (strong tier): synthesizes *passed* pieces into the final
  deliverable.

Every LLM call is logged in a :class:`UsageLedger` (model, tokens, est.
cost). ``budget_usd`` / ``token_budget`` close the valve mid-run — a partial
report with status ``budget_exceeded`` is returned, never a silent stop.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import jsonschema
from pydantic import ValidationError as PydanticValidationError

from orkestra.budget import UsageLedger
from orkestra.chat import ChatClient, parse_json_object
from orkestra.errors import BudgetExceededError, ChatError, EngineError
from orkestra.plan import (
    Attempt,
    KalfaVerdict,
    MicroTask,
    PieceResult,
    PieceStatus,
    SefPlan,
)
from orkestra.prompts import (
    birlestirici_messages,
    hamal_messages,
    kalfa_messages,
    sef_messages,
)
from orkestra.registry import Registry, ResolvedModel
from orkestra.schema import Tier
from orkestra.validate import deterministic_violations

SEF_MAX_ATTEMPTS = 2
"""The sef gets one self-repair retry on an unparseable plan."""

STRONG_CALL_ATTEMPTS = 2
"""Strong-tier calls (arbiter reply, birlestirici) retry once on transport errors."""


def _first_invalid_schema(plan: SefPlan) -> str | None:
    """Return a description of the first piece whose output_schema is invalid.

    A schema that itself fails ``check_schema`` would explode inside the
    kalfa's validator later — the sef's mistake must be caught here, while
    the plan can still be repaired.
    """
    for piece in plan.pieces:
        try:
            jsonschema.Draft202012Validator.check_schema(piece.output_schema)
        except jsonschema.SchemaError as exc:
            return f"piece {piece.id!r} has an invalid output_schema: {exc.message}"
    return None


class Orchestra:
    """Conducts one sef -> hamal -> kalfa -> birlestirici run.

    Args:
        registry: model/provider registry (Phase 1) — tiers drive routing.
        client: a :class:`orkestra.chat.ChatClient`; production code uses
            :class:`orkestra.chat.HttpChatClient`, tests inject a fake.
        strong_model: registry name of the strong model; defaults to the
            first ``tier: strong`` model (sorted by name).
        max_parallel: hamal thread-pool width.
        max_pieces: refuse plans larger than this (cost guardrail).
        arbitrate: when False, acceptance criteria that need judgement are
            recorded as ``unchecked_acceptance`` instead of being refereed
            by the strong model.
        budget_usd: USD valve; requires cost hints on every engaged model.
        token_budget: token valve across the whole run.
    """

    def __init__(
        self,
        registry: Registry,
        client: ChatClient,
        *,
        strong_model: str | None = None,
        max_parallel: int = 4,
        max_pieces: int = 32,
        arbitrate: bool = True,
        budget_usd: float | None = None,
        token_budget: int | None = None,
    ) -> None:
        self._registry = registry
        self._client = client
        self._max_parallel = max(1, max_parallel)
        self._max_pieces = max_pieces
        self._arbitrate = arbitrate
        self._budget_usd = budget_usd
        self._token_budget = token_budget

        self._strong = self._select_strong(registry, strong_model)
        self._cheap = self._select_cheap(registry)
        if budget_usd is not None:
            self._require_cost_hints()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run(self, task: str) -> dict[str, Any]:
        """Execute ``task`` through the full pipeline; return a report dict.

        Raises:
            EngineError: the run cannot start (no plan after retry) or a
                strong-tier call dies both attempts.
            OrkestraError: setup failures surfaced from the registry.
        """
        ledger = UsageLedger(
            budget_usd=self._budget_usd, token_budget=self._token_budget
        )
        run_id = uuid.uuid4().hex[:8]
        errors: list[str] = []
        result: Any = None

        try:
            plan = self._decompose(task, ledger)
        except BudgetExceededError as exc:
            return self._report(
                task, run_id, "budget_exceeded", None, [], ledger, [str(exc)]
            )

        results = self._execute_all(plan.pieces, ledger)

        budget_hit = self._budget_hit(ledger)
        if not budget_hit and any(r.ok for r in results):
            try:
                result = self._synthesize(task, results, ledger)
            except BudgetExceededError:
                budget_hit = True
            except EngineError as exc:
                errors.append(str(exc))

        if budget_hit:
            status = "budget_exceeded"
        elif errors:
            status = "failed"
        elif all(r.ok for r in results):
            status = "ok"
        elif any(r.ok for r in results):
            status = "partial"
        else:
            status = "failed"

        return self._report(task, run_id, status, result, results, ledger, errors)

    # ------------------------------------------------------------------
    # model selection
    # ------------------------------------------------------------------

    @staticmethod
    def _select_strong(registry: Registry, name: str | None) -> ResolvedModel:
        strongs = registry.list_models(tier=Tier.STRONG)
        if name is not None:
            model = registry.get_model(name)
            if model.tier is not Tier.STRONG:
                raise EngineError(
                    f"model {name!r} is tier={model.tier.value}; the sef/kalfa/"
                    f"birlestirici role needs a tier=strong model"
                )
            return registry.resolve(name)
        if not strongs:
            raise EngineError(
                "no tier=strong model registered; the sef, kalfa arbiter and "
                "birlestirici all need one — add it with `orkestra models add`"
            )
        return registry.resolve(strongs[0].name)

    @staticmethod
    def _select_cheap(registry: Registry) -> list[ResolvedModel]:
        cheaps = registry.list_models(tier=Tier.CHEAP)
        if not cheaps:
            raise EngineError(
                "no tier=cheap model registered; the hamal pool is empty — "
                "add one with `orkestra models add ... --tier cheap`"
            )
        return [registry.resolve(m.name) for m in cheaps]

    def _require_cost_hints(self) -> None:
        missing = sorted(
            resolved.model.name
            for resolved in [self._strong, *self._cheap]
            if resolved.model.cost is None
            or resolved.model.cost.input_per_1m is None
            or resolved.model.cost.output_per_1m is None
        )
        if missing:
            raise EngineError(
                "a USD budget was set but these models lack cost hints, so "
                f"the valve cannot meter them: {', '.join(missing)} — set "
                "--cost-in/--cost-out on them or use --token-budget instead"
            )

    # ------------------------------------------------------------------
    # llm call + logging
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        role: str,
        resolved: ResolvedModel,
        messages: list[dict[str, str]],
        ledger: UsageLedger,
        *,
        piece_id: str | None = None,
        attempt: int | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ):
        """One metered chat call: budget check -> call -> ledger record."""
        ledger.ensure_within_budget()
        prompt_text = "\n".join(m["content"] for m in messages)
        response = self._client.complete(
            resolved,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        ledger.record(
            role=role,
            model=resolved.model,
            model_id=resolved.model_id,
            piece_id=piece_id,
            attempt=attempt,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            prompt_text=prompt_text,
            completion_text=response.content,
        )
        return response

    # ------------------------------------------------------------------
    # sef
    # ------------------------------------------------------------------

    def _decompose(self, task: str, ledger: UsageLedger) -> SefPlan:
        """Ask the strong model for a plan; one self-repair retry allowed."""
        messages = sef_messages(task, max_pieces=self._max_pieces)
        last_error: str | None = None
        for _ in range(SEF_MAX_ATTEMPTS):
            try:
                response = self._call_llm(
                    "sef", self._strong, messages, ledger, json_mode=True
                )
            except ChatError as exc:
                last_error = f"sef call failed: {exc}"
                continue
            try:
                raw = parse_json_object(response.content, who="sef")
                plan = SefPlan.model_validate(raw)
            except (ValueError, PydanticValidationError) as exc:
                last_error = str(exc)
            else:
                if len(plan.pieces) > self._max_pieces:
                    last_error = (
                        f"plan has {len(plan.pieces)} pieces, "
                        f"max is {self._max_pieces}"
                    )
                elif (bad := _first_invalid_schema(plan)) is not None:
                    last_error = bad
                else:
                    return plan
            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        "That reply was not a valid plan: "
                        f"{last_error}. Reply with the corrected JSON only."
                    ),
                },
            ]
        raise EngineError(f"sef produced no valid plan: {last_error}")

    # ------------------------------------------------------------------
    # hamal pool + kalfa loop
    # ------------------------------------------------------------------

    def _execute_all(
        self, pieces: list[MicroTask], ledger: UsageLedger
    ) -> list[PieceResult]:
        """Run pieces in parallel over the cheap pool; order is preserved."""
        results: list[PieceResult | None] = [None] * len(pieces)
        workers = min(self._max_parallel, len(pieces))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._execute_piece,
                    piece,
                    self._cheap[i % len(self._cheap)],
                    ledger,
                ): i
                for i, piece in enumerate(pieces)
            }
            for future in as_completed(futures):
                index = futures[future]
                piece = pieces[index]
                try:
                    results[index] = future.result()
                except BudgetExceededError as exc:
                    results[index] = PieceResult(
                        task=piece,
                        status=PieceStatus.FAILED,
                        output=None,
                        error=str(exc),
                    )
                except BaseException:
                    # EngineError (broken arbiter) and unexpected exceptions
                    # propagate: an infrastructure failure must not masquerade
                    # as a piece failure. Pieces not yet started are cancelled
                    # so the doomed run stops spending budget.
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
        return [r for r in results if r is not None]

    def _execute_piece(
        self,
        piece: MicroTask,
        cheap: ResolvedModel,
        ledger: UsageLedger,
    ) -> PieceResult:
        """hamal attempt(s) + kalfa verdict; escalate to strong on exhaustion."""
        attempts: list[Attempt] = []
        fix_hint: str | None = None
        reasons: list[str] = []
        unchecked: list[str] = []

        for n in range(1, piece.max_retries + 2):
            try:
                response = self._call_llm(
                    "hamal",
                    cheap,
                    hamal_messages(
                        piece, fix_hint=fix_hint, previous_reasons=reasons
                    ),
                    ledger,
                    piece_id=piece.id,
                    attempt=n,
                    json_mode=True,
                    max_tokens=piece.budget_tokens,
                )
            except ChatError as exc:
                reasons = [f"transport error: {exc}"]
                attempts.append(
                    Attempt(n, cheap.model.name, "cheap", False, list(reasons))
                )
                continue
            try:
                output = parse_json_object(
                    response.content, who=f"hamal[{piece.id}]"
                )
            except ValueError as exc:
                reasons = [str(exc)]
                fix_hint = "reply with a single JSON object matching output_schema"
                attempts.append(
                    Attempt(n, cheap.model.name, "cheap", False, list(reasons))
                )
                continue

            verdict, unchecked = self._validate(piece, output, ledger, n)
            attempts.append(
                Attempt(n, cheap.model.name, "cheap", verdict.passed, verdict.reasons)
            )
            if verdict.passed:
                return PieceResult(
                    task=piece,
                    status=PieceStatus.PASSED,
                    output=output,
                    attempts=attempts,
                    unchecked_acceptance=unchecked,
                )
            reasons = verdict.reasons
            fix_hint = verdict.fix_hint

        return self._escalate(piece, ledger, attempts, reasons, fix_hint)

    def _escalate(
        self,
        piece: MicroTask,
        ledger: UsageLedger,
        attempts: list[Attempt],
        reasons: list[str],
        fix_hint: str | None,
    ) -> PieceResult:
        """Last chance: run the piece once on the strong model."""
        n = len(attempts) + 1
        try:
            response = self._call_llm(
                "hamal",
                self._strong,
                hamal_messages(piece, fix_hint=fix_hint, previous_reasons=reasons),
                ledger,
                piece_id=piece.id,
                attempt=n,
                json_mode=True,
                max_tokens=piece.budget_tokens,
            )
            output = parse_json_object(response.content, who=f"hamal[{piece.id}]+strong")
        except (ChatError, ValueError) as exc:
            attempts.append(Attempt(n, self._strong.model.name, "strong", False, [str(exc)]))
            return PieceResult(
                task=piece,
                status=PieceStatus.FAILED,
                output=None,
                attempts=attempts,
                error=f"escalation failed: {exc}",
            )

        verdict, unchecked = self._validate(piece, output, ledger, n)
        attempts.append(
            Attempt(n, self._strong.model.name, "strong", verdict.passed, verdict.reasons)
        )
        if verdict.passed:
            return PieceResult(
                task=piece,
                status=PieceStatus.ESCALATED_PASSED,
                output=output,
                attempts=attempts,
                unchecked_acceptance=unchecked,
            )
        return PieceResult(
            task=piece,
            status=PieceStatus.FAILED,
            output=None,
            attempts=attempts,
            error="; ".join(verdict.reasons) or "escalated output rejected",
        )

    # ------------------------------------------------------------------
    # kalfa
    # ------------------------------------------------------------------

    def _validate(
        self,
        piece: MicroTask,
        output: dict[str, Any],
        ledger: UsageLedger,
        attempt: int,
    ) -> tuple[KalfaVerdict, list[str]]:
        """Deterministic checks, then optional strong-model arbitration.

        Returns the verdict plus any acceptance criteria that were *not*
        checked (arbitration disabled) — recorded on the result so nothing
        passes silently.
        """
        violations = deterministic_violations(piece, output)
        if violations:
            return (
                KalfaVerdict(
                    passed=False, reasons=violations, fix_hint=violations[0]
                ),
                [],
            )
        if not piece.acceptance:
            return KalfaVerdict(passed=True), []
        if not self._arbitrate:
            return KalfaVerdict(passed=True), list(piece.acceptance)
        return self._run_arbitration(piece, output, ledger, attempt), []

    def _run_arbitration(
        self,
        piece: MicroTask,
        output: dict[str, Any],
        ledger: UsageLedger,
        attempt: int,
    ) -> KalfaVerdict:
        """Stage 2: the strong model referees the acceptance criteria."""
        messages = kalfa_messages(piece, output)
        last_error: str | None = None
        for _ in range(STRONG_CALL_ATTEMPTS):
            try:
                response = self._call_llm(
                    "kalfa",
                    self._strong,
                    messages,
                    ledger,
                    piece_id=piece.id,
                    attempt=attempt,
                    json_mode=True,
                )
            except ChatError as exc:
                last_error = str(exc)
                continue
            try:
                raw = parse_json_object(response.content, who="kalfa")
                return KalfaVerdict.model_validate(raw)
            except (ValueError, PydanticValidationError) as exc:
                last_error = str(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not a valid verdict: "
                            f"{last_error}. Reply with the verdict JSON only."
                        ),
                    },
                ]
        raise EngineError(
            f"kalfa arbiter failed for piece {piece.id!r}: {last_error}"
        )

    # ------------------------------------------------------------------
    # birlestirici
    # ------------------------------------------------------------------

    def _synthesize(
        self,
        task: str,
        results: list[PieceResult],
        ledger: UsageLedger,
    ) -> Any:
        """Fuse validated piece outputs into the final deliverable."""
        pairs = [(r.task, r.output) for r in results if r.ok and r.output is not None]
        failed_ids = [r.task.id for r in results if not r.ok]
        messages = birlestirici_messages(task, pairs, failed_ids=failed_ids)
        last_error: str | None = None
        for _ in range(STRONG_CALL_ATTEMPTS):
            try:
                response = self._call_llm(
                    "birlestirici", self._strong, messages, ledger
                )
            except ChatError as exc:
                last_error = str(exc)
                continue
            content = response.content.strip()
            try:
                return json.loads(content)
            except ValueError:
                return content
        raise EngineError(f"birlestirici call failed: {last_error}")

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _budget_hit(ledger: UsageLedger) -> bool:
        try:
            ledger.ensure_within_budget()
        except BudgetExceededError:
            return True
        return False

    @staticmethod
    def _report(
        task: str,
        run_id: str,
        status: str,
        result: Any,
        results: list[PieceResult],
        ledger: UsageLedger,
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "task": task,
            "status": status,
            "result": result,
            "pieces": [
                {
                    "id": r.task.id,
                    "instruction": r.task.instruction,
                    "status": r.status.value,
                    "output": r.output,
                    "attempts": [
                        {
                            "n": a.number,
                            "model": a.model,
                            "tier": a.tier,
                            "passed": a.passed,
                            "reasons": a.reasons,
                        }
                        for a in r.attempts
                    ],
                    "unchecked_acceptance": r.unchecked_acceptance,
                    "error": r.error,
                }
                for r in results
            ],
            "usage": ledger.as_report(),
            "errors": errors,
        }
