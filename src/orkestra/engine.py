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
import threading
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
            OrkestraError: setup failures surfaced from the registry.

        A failed sef or a broken arbiter no longer raises: the run returns a
        ``failed`` report with the pieces completed so far and the full
        usage ledger, so spent budget is never lost from the report.
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
        except EngineError as exc:
            # No usable plan: still report what the sef attempts spent.
            return self._report(task, run_id, "failed", None, [], ledger, [str(exc)])

        results, abort_error = self._execute_all(plan.pieces, ledger)
        if abort_error is not None:
            errors.append(str(abort_error))

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
        elif errors and (result is not None or any(r.ok for r in results)):
            # An abort mid-run can still leave a usable deliverable — say so.
            status = "partial"
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
                        f"plan has {len(plan.pieces)} pieces, max is {self._max_pieces}"
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
    ) -> tuple[list[PieceResult], EngineError | None]:
        """Run pieces in parallel over the cheap pool; order is preserved.

        An infrastructure failure (a broken arbiter, an unexpected exception)
        does not discard the run: ``abort_event`` tells queued pieces to report
        themselves ``cancelled`` instead of calling the model, completed
        results are kept and the error is returned so the caller can emit a
        partial report with the full usage ledger. In-flight HTTP calls are
        not interruptible, but no *new* model calls start after the abort.

        Cancellation is cooperative: ``cancel_futures=True`` inside the
        ``as_completed`` loop would deadlock the iteration (a drained pending
        future is never marked CANCELLED_AND_NOTIFIED, so the waiter's event
        is never set), so pieces check the event themselves.
        """
        results: list[PieceResult | None] = [None] * len(pieces)
        abort: EngineError | None = None
        abort_event = threading.Event()
        workers = min(self._max_parallel, len(pieces))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._execute_piece,
                    piece,
                    i,
                    ledger,
                    abort_event,
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
                    abort_event.set()
                except EngineError as exc:
                    if abort is None:
                        abort = exc
                    results[index] = PieceResult(
                        task=piece,
                        status=PieceStatus.FAILED,
                        output=None,
                        error=str(exc),
                    )
                    abort_event.set()
                except (KeyboardInterrupt, SystemExit):
                    # Stop dispatching new work and let the interrupt
                    # propagate — swallowing it would trap the user in a
                    # run that looks hung.
                    abort_event.set()
                    raise
                except Exception as exc:
                    # Unexpected infrastructure failure: keep it loud, but
                    # still return the partial results + ledger to the caller.
                    if abort is None:
                        abort = EngineError(f"run aborted: {exc}")
                    results[index] = PieceResult(
                        task=piece,
                        status=PieceStatus.FAILED,
                        output=None,
                        error=str(exc),
                    )
                    abort_event.set()
        return [r for r in results if r is not None], abort

    @staticmethod
    def _cancelled(piece: MicroTask, attempts: list[Attempt]) -> PieceResult:
        """The piece never started (or stopped) because the run was aborted."""
        return PieceResult(
            task=piece,
            status=PieceStatus.CANCELLED,
            output=None,
            attempts=attempts,
            error="cancelled: run aborted",
        )

    def _execute_piece(
        self,
        piece: MicroTask,
        pool_index: int,
        ledger: UsageLedger,
        abort_event: threading.Event,
    ) -> PieceResult:
        """hamal attempt(s) + kalfa verdict; escalate to strong on exhaustion.

        On a transport error the next cheap model in the pool takes over
        (round-robin from the piece's start slot), so one dead provider
        does not burn every retry of every piece it was assigned.

        ``abort_event`` is checked before every LLM call: once the run is
        aborted the piece reports itself cancelled instead of spending
        budget on a doomed attempt.
        """
        attempts: list[Attempt] = []
        fix_hint: str | None = None
        reasons: list[str] = []
        slot = pool_index

        try:
            return self._piece_loop(
                piece, slot, ledger, abort_event, attempts, fix_hint, reasons
            )
        except EngineError:
            # Fail fast for siblings too: the worker knows the run is dead
            # before the collector thread consumes its future.
            abort_event.set()
            raise

    def _piece_loop(
        self,
        piece: MicroTask,
        slot: int,
        ledger: UsageLedger,
        abort_event: threading.Event,
        attempts: list[Attempt],
        fix_hint: str | None,
        reasons: list[str],
    ) -> PieceResult:
        unchecked: list[str] = []
        for n in range(1, piece.max_retries + 2):
            if abort_event.is_set():
                return self._cancelled(piece, attempts)
            cheap = self._cheap[slot % len(self._cheap)]
            try:
                response = self._call_llm(
                    "hamal",
                    cheap,
                    hamal_messages(piece, fix_hint=fix_hint, previous_reasons=reasons),
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
                # Transport failure looks like provider trouble, not the
                # model's output quality: rotate to the next cheap model.
                slot += 1
                continue
            try:
                output = parse_json_object(response.content, who=f"hamal[{piece.id}]")
            except ValueError as exc:
                reasons = [str(exc)]
                fix_hint = "reply with a single JSON object matching output_schema"
                attempts.append(
                    Attempt(n, cheap.model.name, "cheap", False, list(reasons))
                )
                continue

            if abort_event.is_set():
                return self._cancelled(piece, attempts)
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

        return self._escalate(piece, ledger, attempts, reasons, fix_hint, abort_event)

    def _escalate(
        self,
        piece: MicroTask,
        ledger: UsageLedger,
        attempts: list[Attempt],
        reasons: list[str],
        fix_hint: str | None,
        abort_event: threading.Event,
    ) -> PieceResult:
        """Last chance: run the piece once on the strong model."""
        if abort_event.is_set():
            return self._cancelled(piece, attempts)
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
            output = parse_json_object(
                response.content, who=f"hamal[{piece.id}]+strong"
            )
        except (ChatError, ValueError) as exc:
            attempts.append(
                Attempt(n, self._strong.model.name, "strong", False, [str(exc)])
            )
            return PieceResult(
                task=piece,
                status=PieceStatus.FAILED,
                output=None,
                attempts=attempts,
                error=f"escalation failed: {exc}",
            )

        if abort_event.is_set():
            return self._cancelled(piece, attempts)
        verdict, unchecked = self._validate(piece, output, ledger, n)
        attempts.append(
            Attempt(
                n, self._strong.model.name, "strong", verdict.passed, verdict.reasons
            )
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
                KalfaVerdict(passed=False, reasons=violations, fix_hint=violations[0]),
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
        raise EngineError(f"kalfa arbiter failed for piece {piece.id!r}: {last_error}")

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
