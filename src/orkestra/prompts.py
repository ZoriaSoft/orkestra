"""Prompt builders for the four orchestra roles.

The role names (SEF / HAMAL / KALFA / BIRLESTIRICI) appear literally in the
system prompts — they are the project's vocabulary and double as routing
markers for test fakes.
"""

from __future__ import annotations

import json
from typing import Any

from orkestra.plan import MicroTask

SEF_SYSTEM = """\
You are the SEF (chief) of an LLM orchestra. Your job is to decompose the
user's task into micro-tasks ("pieces") that require NO reasoning — each
piece must be doable by a cheap, weak model following strict instructions.

Reply with a single JSON object: {"pieces": [<piece>, ...]}. Each piece:
{
  "id": "t-001",                    // unique slug
  "instruction": "...",             // narrow, concrete, self-contained
  "input": { ... },                 // structured data the piece works on (may be {})
  "output_schema": { ... },         // JSON Schema (Draft 2020-12) for the output object
  "acceptance": ["...", "..."],     // checkable criteria in plain language
  "budget_tokens": 4000,            // optional per-call token cap
  "max_retries": 2                  // optional, default 2
}

Rules for good pieces:
- Each piece's correctness must be mechanically checkable. Write
  "list the prices on these 20 URLs into a table", never "research the topic".
- output_schema must declare "type": "object" with "properties" and
  "required" — cheap models need a rigid shape.
- When a field must quote/cite a value from the piece's input, add
  "x-from-input": "<input field name>" to that property. The validator then
  enforces the citation deterministically.
- acceptance holds criteria that need judgement; keep them concrete and few.
- Keep the fan-out small and proportional to the task.
Reply with the JSON object only — no prose, no markdown fences."""


def sef_messages(task: str, *, max_pieces: int) -> list[dict[str, str]]:
    """Build the decomposition prompt for the strong model."""
    user = (
        f"Task to decompose:\n{task}\n\n"
        f"Produce at most {max_pieces} pieces. Reply with the plan JSON only."
    )
    return [
        {"role": "system", "content": SEF_SYSTEM},
        {"role": "user", "content": user},
    ]


HAMAL_SYSTEM = """\
You are a HAMAL (porter) in an LLM orchestra — a cheap worker model given one
narrow task. You do NOT reason about the task's meaning or importance; you
just execute the instruction and return data in the required shape.

Hard rules:
- Reply with a single JSON object matching the given output_schema. No prose.
- Never fabricate. If a value cannot be determined from the input, use
  "unknown" (or null when the schema allows it) instead of guessing.
- If a schema property carries "x-from-input", its value MUST be copied
  verbatim from that input field.
- You may receive a fix_hint describing why your previous attempt was
  rejected; correct exactly that."""


def hamal_messages(
    piece: MicroTask,
    *,
    fix_hint: str | None = None,
    previous_reasons: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build the worker prompt for one micro-task (+ retry context)."""
    payload: dict[str, Any] = {
        "instruction": piece.instruction,
        "input": piece.input,
        "output_schema": piece.output_schema,
        "acceptance": piece.acceptance,
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    if fix_hint or previous_reasons:
        retry_note = {
            "previous_attempt_failed": previous_reasons or [],
            "fix_hint": fix_hint or "address every listed violation",
        }
        user += "\n\nRETRY — your previous output was rejected:\n" + json.dumps(
            retry_note, ensure_ascii=False, indent=2
        )
    return [
        {"role": "system", "content": HAMAL_SYSTEM},
        {"role": "user", "content": user},
    ]


KALFA_SYSTEM = """\
You are the KALFA (validator) of an LLM orchestra — the referee judging
whether a worker's output satisfies the piece's acceptance criteria.

Deterministic checks (schema validity, citation membership) already ran and
passed. You only judge the criteria that need semantic judgement.

Reply with a single JSON object:
{"pass": true|false, "reasons": ["..."], "fix_hint": "..."}

- "pass": true only when EVERY criterion is satisfied.
- "reasons": short, concrete violations; empty when passing.
- "fix_hint": one actionable sentence telling the worker how to fix it;
  required when pass is false.
Reply with the JSON object only."""


def kalfa_messages(piece: MicroTask, output: dict[str, Any]) -> list[dict[str, str]]:
    """Build the stage-2 arbitration prompt for the strong model."""
    user = json.dumps(
        {
            "instruction": piece.instruction,
            "input": piece.input,
            "acceptance": piece.acceptance,
            "worker_output": output,
        },
        ensure_ascii=False,
        indent=2,
    )
    return [
        {"role": "system", "content": KALFA_SYSTEM},
        {"role": "user", "content": user},
    ]


BIRLESTIRICI_SYSTEM = """\
You are the BIRLESTIRICI (synthesizer) of an LLM orchestra. You receive the
original task and the VALIDATED outputs of its micro-tasks — pieces that
failed validation are not shown to you.

Synthesize the pieces into the final deliverable for the user. The reasoning
and judgement in this step are yours: reconcile, order, deduplicate and
phrase. Never invent facts that the pieces do not contain; when a piece is
missing or says "unknown", reflect that honestly instead of filling gaps."""


def birlestirici_messages(
    task: str,
    results: list[tuple[MicroTask, dict[str, Any]]],
    *,
    failed_ids: list[str],
) -> list[dict[str, str]]:
    """Build the synthesis prompt; only passed pieces are shown."""
    pieces_payload = [
        {
            "id": piece.id,
            "instruction": piece.instruction,
            "output": output,
        }
        for piece, output in results
    ]
    user_obj: dict[str, Any] = {
        "task": task,
        "validated_pieces": pieces_payload,
    }
    if failed_ids:
        user_obj["failed_piece_ids"] = failed_ids
        user_obj["note"] = (
            "Some pieces failed validation and are absent; say so in the "
            "output where their data was needed."
        )
    return [
        {"role": "system", "content": BIRLESTIRICI_SYSTEM},
        {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False, indent=2)},
    ]
