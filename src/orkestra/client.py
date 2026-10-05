"""Minimal OpenAI-compatible HTTP client.

Used by ``orkestra providers test`` (and later by the engine) to probe an
endpoint: ``GET {base_url}/v1/models`` measures latency and returns the list
of reachable model ids. Network failures never raise — they are reported
inside :class:`ProbeResult` so the CLI can render them uniformly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from orkestra.schema import ProviderConfig


@dataclass
class ProbeResult:
    """Outcome of a single provider probe.

    Attributes:
        ok: whether the endpoint answered 2xx with a well-formed model list.
        latency_ms: wall-clock time of the HTTP request.
        status_code: HTTP status when a response was received.
        model_ids: sorted ids from the ``data`` array on success.
        error: human-readable failure reason when ``ok`` is False.
    """

    ok: bool
    latency_ms: float
    status_code: int | None = None
    model_ids: list[str] = field(default_factory=list)
    error: str | None = None


def models_url(base_url: str) -> str:
    """Return the ``/models`` endpoint for a base URL.

    ``https://host/v1`` -> ``https://host/v1/models``;
    ``https://host``   -> ``https://host/v1/models``;
    ``https://host/api`` -> ``https://host/api/v1/models``.
    """
    base = base_url.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


def probe_provider(
    provider: ProviderConfig,
    api_key: str | None,
    timeout: float | None = None,
) -> ProbeResult:
    """Probe one provider's ``/v1/models`` endpoint.

    Args:
        provider: the provider to contact.
        api_key: resolved secret for the ``Authorization: Bearer`` header, or
            ``None`` for unauthenticated endpoints. Never logged.
        timeout: per-request timeout in seconds; defaults to
            ``provider.timeout_seconds``.

    Returns:
        A :class:`ProbeResult`; never raises for network or HTTP failures.
    """
    headers = dict(provider.headers)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    start = time.monotonic()
    try:
        response = httpx.get(
            models_url(provider.base_url),
            headers=headers,
            timeout=timeout if timeout is not None else provider.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            ok=False,
            latency_ms=_elapsed_ms(start),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency = _elapsed_ms(start)

    if response.status_code >= 400:
        snippet = response.text[:200].strip()
        detail = f": {snippet}" if snippet else ""
        return ProbeResult(
            ok=False,
            latency_ms=latency,
            status_code=response.status_code,
            error=f"HTTP {response.status_code}{detail}",
        )

    try:
        payload = response.json()
    except ValueError:
        return ProbeResult(
            ok=False,
            latency_ms=latency,
            status_code=response.status_code,
            error="response is not valid JSON",
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ProbeResult(
            ok=False,
            latency_ms=latency,
            status_code=response.status_code,
            error="unexpected JSON shape: missing 'data' list",
        )

    model_ids = sorted(
        item["id"]
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    return ProbeResult(
        ok=True,
        latency_ms=latency,
        status_code=response.status_code,
        model_ids=model_ids,
    )


def _elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since ``start`` (monotonic clock)."""
    return (time.monotonic() - start) * 1000.0
