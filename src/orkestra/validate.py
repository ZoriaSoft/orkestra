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

from orkestra.plan import MicroTask

MAX_VIOLATIONS = 8
"""Cap on reported violations so a garbage output doesn't flood the retry hint."""


def deterministic_violations(task: MicroTask, output: Any) -> list[str]:
    """Return human-readable violations; empty list means stage-1 passed."""
    violations: list[str] = []
    if not isinstance(output, dict):
        return [f"output must be a JSON object, got {type(output).__name__}"]

    validator = jsonschema.Draft202012Validator(task.output_schema)
    for err in sorted(validator.iter_errors(output), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "(root)"
        violations.append(f"schema: {where}: {err.message}")

    violations.extend(_citation_violations(task, output))
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
        source = task.input.get(source_key)
        if not isinstance(source, list):
            violations.append(
                f"citation: {prop}: x-from-input refers to missing/non-list "
                f"input field {source_key!r}"
            )
            continue
        value = output[prop]
        if value not in source:
            violations.append(
                f"citation: {prop}: {value!r} is not a member of input {source_key!r}"
            )
    return violations
