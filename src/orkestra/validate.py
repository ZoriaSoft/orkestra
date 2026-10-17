"""Kalfa stage 1: deterministic output checks — no model involved.

Every worker output is checked before it can pass:

1. it must be a JSON object (dict);
2. it must validate against the piece's ``output_schema`` (jsonschema);
3. properties marked ``x-from-input: "<key>"`` must cite a member of
   ``task.input[<key>]`` — the deterministic citation check.

Anything the deterministic layer cannot judge (natural-language acceptance
criteria) is left for stage-2 arbitration on the strong model.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from orkestra.plan import MicroTask

MAX_VIOLATIONS = 8
"""Cap on reported violations so a garbage output doesn't flood the retry hint."""

MAX_SCHEMA_VIOLATIONS = 5
MAX_CITATION_VIOLATIONS = 3

_MISSING = object()
"""Sentinel: distinguishes "no bad element" from a bad element that is None."""


def _deny_remote_retrieve(uri: str) -> Resource[Any]:
    """Refuse every retrieval: only local ``#/...`` refs may resolve.

    jsonschema's default retrieve fetches http(s):// and file:// URIs via
    urllib (SSRF via model-authored schemas). Local fragment refs resolve
    against the root resource and never reach this callable; anything that
    does is external by definition and is rejected without any I/O.
    """
    raise Unresolvable(ref=uri)


_LOCAL_ONLY_REGISTRY: Registry[Any] = Registry(retrieve=_deny_remote_retrieve)
"""referencing Registry that never performs network/file retrieval."""


def deterministic_violations(task: MicroTask, output: Any) -> list[str]:
    """Return human-readable violations; empty list means stage-1 passed.

    Schema errors are capped at 5 and citation errors at 3: with a flat cap,
    a schema-heavy garbage output starved the citation checks out of the
    retry hint entirely, so the hamal never learned why it failed.
    """
    violations: list[str] = []
    if not isinstance(output, dict):
        return [f"output must be a JSON object, got {type(output).__name__}"]

    validator = jsonschema.Draft202012Validator(
        task.output_schema, registry=_LOCAL_ONLY_REGISTRY
    )
    try:
        schema_errors = [
            f"schema: {'.'.join(str(p) for p in err.absolute_path) or '(root)'}: {err.message}"
            for err in sorted(
                validator.iter_errors(output), key=lambda e: list(e.absolute_path)
            )
        ]
    except Unresolvable as exc:
        # External $ref slipped past plan checks: report it as a schema
        # violation (retryable) instead of fetching it or crashing the run.
        schema_errors = [f"schema: unresolvable $ref: {exc}"]
    violations.extend(schema_errors[:MAX_SCHEMA_VIOLATIONS])
    violations.extend(_citation_violations(task, output)[:MAX_CITATION_VIOLATIONS])
    return violations[:MAX_VIOLATIONS]


def _citation_violations(task: MicroTask, output: dict[str, Any]) -> list[str]:
    """Check ``x-from-input`` properties against the piece's input lists."""
    properties = task.output_schema.get("properties")
    if not isinstance(properties, dict):
        return []
    violations: list[str] = []
    for prop, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        source_key = prop_schema.get("x-from-input")
        if source_key is None or prop not in output:
            continue
        if not isinstance(source_key, str):
            # Plan validation rejects this, but a bypassed/hand-built task
            # must not crash here: input.get(<list>) raises TypeError.
            violations.append(
                f"citation: {prop}: x-from-input must be a string naming "
                f"an input field, got {type(source_key).__name__}"
            )
            continue
        source = task.input.get(source_key)
        if not isinstance(source, list):
            violations.append(
                f"citation: {prop}: x-from-input refers to missing/non-list "
                f"input field {source_key!r}"
            )
            continue
        value = output[prop]
        # A list value is a citation *set*: every element must come from the
        # input (an empty list carries no false claim, so it passes).
        candidates = value if isinstance(value, list) else [value]
        bad = next((v for v in candidates if v not in source), _MISSING)
        if bad is not _MISSING:
            violations.append(
                f"citation: {prop}: {bad!r} is not a member of input {source_key!r}"
            )
    return violations
