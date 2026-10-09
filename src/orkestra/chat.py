"""OpenAI-compatible chat-completions transport.

:func:`chat_completions_url` mirrors :func:`orkestra.client.models_url`.
:class:`HttpChatClient` issues ``POST {base}/v1/chat/completions`` and
returns a :class:`ChatResponse`; every failure mode raises
:class:`orkestra.errors.ChatError` — nothing fails silently.

The engine depends only on the :class:`ChatClient` protocol, so tests inject
an in-memory fake and never touch the network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from orkestra.errors import ChatError
from orkestra.registry import ResolvedModel


def chat_completions_url(base_url: str) -> str:
    """Return the ``/chat/completions`` endpoint for a base URL.

    ``https://host/v1``  -> ``https://host/v1/chat/completions``;
    ``https://host``     -> ``https://host/v1/chat/completions``;
    ``https://host/api`` -> ``https://host/api/v1/chat/completions``.
    """
    base = base_url.rstrip("/")
    suffix = "chat/completions"
    return f"{base}/{suffix}" if base.endswith("/v1") else f"{base}/v1/{suffix}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) used when a provider omits usage."""
    return max(1, len(text) // 4)


@dataclass
class ChatResponse:
    """Result of one chat-completions call.

    Attributes:
        content: assistant message text.
        model: model id echoed by the provider (falls back to requested id).
        prompt_tokens: reported input tokens, or ``None`` when absent.
        completion_tokens: reported output tokens, or ``None`` when absent.
        latency_ms: wall-clock time of the HTTP request.
        raw: full provider payload for debugging.
    """

    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float
    raw: dict[str, Any] = field(default_factory=dict)


class ChatClient(Protocol):
    """Anything that can complete a chat call for a resolved model.

    ``json_mode`` asks the provider for a JSON object response
    (``response_format: {"type": "json_object"}``); implementations that
    cannot honour it must still return parseable text — the engine validates
    output itself.
    """

    def complete(
        self,
        resolved: ResolvedModel,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResponse: ...


class HttpChatClient:
    """ChatClient over httpx against an OpenAI-compatible endpoint."""

    def complete(
        self,
        resolved: ResolvedModel,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        """POST a chat completion and unwrap the first choice.

        Raises:
            ChatError: network failure, HTTP error, non-JSON body, or a
                payload missing ``choices[0].message.content``.
        """
        provider = resolved.provider
        payload: dict[str, Any] = {
            "model": resolved.model_id,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            # Reasoning-family models (o1/o3/gpt-5 …) reject ``max_tokens``
            # with HTTP 400 and require ``max_completion_tokens``. Try the
            # legacy field first; on an explicit "unsupported parameter"
            # 400, retry once with the modern field.
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        headers = dict(provider.headers)
        if resolved.api_key:
            headers["Authorization"] = f"Bearer {resolved.api_key}"

        url = chat_completions_url(provider.base_url)
        start = time.monotonic()
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=provider.timeout_seconds,
            )
            if (
                response.status_code == 400
                and "max_tokens" in payload
                and "max_completion_tokens" not in payload
                and "unsupported parameter" in response.text.lower()
            ):
                # Reasoning-family models (o1/o3/gpt-5 …) reject max_tokens;
                # swap to the modern field and retry once.
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                response = httpx.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=provider.timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise ChatError(
                f"{provider.name}: chat completion failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = (time.monotonic() - start) * 1000.0

        if response.status_code >= 400:
            snippet = response.text[:200].strip()
            detail = f": {snippet}" if snippet else ""
            raise ChatError(
                f"{provider.name}: HTTP {response.status_code}{detail}"
            )

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ChatError(f"{provider.name}: response is not valid JSON") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError(
                f"{provider.name}: response missing choices[0].message.content"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ChatError(f"{provider.name}: empty assistant content")

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        echoed = body.get("model")
        return ChatResponse(
            content=content,
            model=echoed if isinstance(echoed, str) else resolved.model_id,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            latency_ms=latency_ms,
            raw=body,
        )


def _int_or_none(value: Any) -> int | None:
    """Coerce a usage counter to int, tolerating floats/strings."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_json_object(text: str, *, who: str) -> dict[str, Any]:
    """Parse an assistant reply into a JSON object.

    Tolerates ```json fences and leading/trailing prose around the first
    ``{...}`` block, which cheap models add despite ``json_mode``.

    Raises:
        ValueError: no JSON object can be recovered; ``who`` (the role that
            produced the reply) is included in the message.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # strip ```json ... ``` fences
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except ValueError:
        start = cleaned.find("{")
        if start == -1:
            raise ValueError(f"{who}: reply contains no JSON object") from None
        try:
            # raw_decode stops at the end of the first object, so trailing
            # prose — even prose containing braces — cannot poison the parse.
            parsed, _ = json.JSONDecoder().raw_decode(cleaned, start)
        except ValueError:
            # Fallback: the widest {...} slice (the object's own braces may
            # have been preceded by junk that raw_decode tripped on).
            end = cleaned.rfind("}")
            if end <= start:
                raise ValueError(f"{who}: reply contains no JSON object") from None
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except ValueError as exc:
                raise ValueError(f"{who}: reply is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{who}: reply must be a JSON object")
    return parsed
