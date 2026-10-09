# Changelog

## 0.2.1 — 2026-10-09

Fixes found in the pre-release review:

- **Run abort no longer deadlocks.** `_execute_all` used to call
  `pool.shutdown(cancel_futures=True)` inside the `as_completed` loop; pending
  futures cancelled that way never notify the `as_completed` waiter, so a
  broken kalfa arbiter hung `orkestra run` forever. Cancellation is now
  cooperative via a per-run `threading.Event`: pieces check it before every
  LLM call and report themselves `cancelled` (new `PieceStatus.CANCELLED`),
  so every piece appears in the report.
- **`KeyboardInterrupt`/`SystemExit` propagate.** The blanket
  `except BaseException` no longer swallows Ctrl-C; it sets the abort flag
  and re-raises.
- **Aborted runs can still be `partial`.** A run that produced a synthesis or
  at least one passed piece before the abort reports `partial` instead of
  `failed` (errors stay in the report).
- **`parse_json_object` tolerates trailing prose with braces.** The first
  JSON object is decoded with `json.JSONDecoder().raw_decode`, so a second
  object or a stray `}` after the payload can no longer poison the parse.
- **`x-from-input` works on array properties.** A list value passes the
  citation check only when every element is a member of the input list;
  scalars are unchanged.
- **CLI validates limits.** `--budget`, `--token-budget` and
  `--max-parallel` must be positive; non-positive values are a usage error
  before any registry load or network call.
- Lint: `ruff>=0.6` is a dev extra and `ruff check .` is clean
  (`select = E4,E7,E9,F,I,B,UP`, `B008` ignored — Typer idiom; enums are
  `StrEnum`).

## 0.2.0

Phase 2: the orchestra engine. `orkestra run` decomposes a task with the
sef (strong model), executes pieces on the hamal pool (cheap models,
parallel), validates every output with the kalfa (schema + citation checks,
then strong-model arbitration), and synthesizes with the birlestirici.
Per-call usage ledger with USD and token budget valves; bounded retries with
strong-model escalation.

## 0.1.0

Phase 1: provider/model registry (`orkestra providers|models`), atomic
0600 config writes, `providers test` probe.
