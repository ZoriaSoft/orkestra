"""Engine tests: Orchestra driven by an in-memory FakeChatClient.

No real API calls. The fake routes by the role name embedded in each role's
system prompt (SEF / HAMAL / KALFA / BIRLESTIRICI), so a test can script
every stage of a run deterministically.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from orkestra.chat import ChatResponse
from orkestra.engine import Orchestra
from orkestra.errors import ChatError, EngineError
from orkestra.registry import Registry
from orkestra.schema import (
    CostHint,
    ModelConfig,
    OrkestraConfig,
    ProviderConfig,
    Purpose,
    Tier,
)

Call = dict[str, Any]
Handler = Callable[[Any, list[dict[str, str]]], Any]


class FakeChatClient:
    """ChatClient that delegates to a scripted handler; records every call."""

    def __init__(self, handler: Handler, *, usage: tuple[int, int] = (10, 10)) -> None:
        self.handler = handler
        self.usage = usage
        self.calls: list[Call] = []
        self._lock = threading.Lock()

    def complete(
        self, resolved, messages, *, json_mode=False, max_tokens=None, temperature=None
    ):
        with self._lock:
            self.calls.append(
                {
                    "model": resolved.model.name,
                    "role": role_of(messages),
                    "messages": [dict(m) for m in messages],
                    "json_mode": json_mode,
                    "max_tokens": max_tokens,
                }
            )
        reply = self.handler(resolved, messages)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            reply = {"content": reply}
        prompt_tokens, completion_tokens = self.usage
        return ChatResponse(
            content=reply["content"],
            model=resolved.model_id,
            prompt_tokens=reply.get("prompt_tokens", prompt_tokens),
            completion_tokens=reply.get("completion_tokens", completion_tokens),
            latency_ms=1.0,
        )


def role_of(messages: list[dict[str, str]]) -> str:
    """Extract the orchestra role from a system prompt."""
    system = messages[0]["content"]
    for role in ("SEF", "HAMAL", "KALFA", "BIRLESTIRICI"):
        if role in system:
            return role.lower()
    raise AssertionError(f"no role marker in system prompt: {system[:80]}")


def calls_for(client: FakeChatClient, role: str) -> list[Call]:
    return [c for c in client.calls if c["role"] == role]


NUM_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "number"}},
    "required": ["value"],
}

PLAN_TWO = {
    "pieces": [
        {
            "id": "t-1",
            "instruction": "double the number",
            "input": {"n": 21},
            "output_schema": NUM_SCHEMA,
            "acceptance": [],
        },
        {
            "id": "t-2",
            "instruction": "square the number",
            "input": {"n": 5},
            "output_schema": NUM_SCHEMA,
            "acceptance": [],
        },
    ]
}


def plan_json(pieces: list[dict[str, Any]] | None = None) -> str:
    return json.dumps({"pieces": pieces if pieces is not None else PLAN_TWO["pieces"]})


@pytest.fixture
def registry(api_key_env: str) -> Registry:
    """Registry: one strong + two cheap models, all with cost hints."""
    provider = ProviderConfig(
        name="p", base_url="http://127.0.0.1:1", api_key_env=api_key_env
    )
    cost = CostHint(input_per_1m=1.0, output_per_1m=4.0)
    config = OrkestraConfig(
        providers={"p": provider},
        models={
            "brain": ModelConfig(
                name="brain", provider="p", tier=Tier.STRONG, cost=cost
            ),
            "mule-a": ModelConfig(
                name="mule-a",
                provider="p",
                tier=Tier.CHEAP,
                purposes=[Purpose.MICRO_TASK],
                cost=cost,
            ),
            "mule-b": ModelConfig(
                name="mule-b",
                provider="p",
                tier=Tier.CHEAP,
                purposes=[Purpose.MICRO_TASK],
                cost=cost,
            ),
        },
    )
    return Registry(config)


def make_orchestra(
    registry: Registry, client: FakeChatClient, **kwargs: Any
) -> Orchestra:
    kwargs.setdefault("max_parallel", 2)
    return Orchestra(registry, client, **kwargs)


class TestHappyPath:
    def test_full_run_ok(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                return '{"value": 42}'
            if role == "birlestirici":
                return {"content": "final synthesis"}
            raise AssertionError(f"unexpected role {role}")

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("double 21 and square 5")

        assert report["status"] == "ok"
        assert report["result"] == "final synthesis"
        assert [p["status"] for p in report["pieces"]] == ["passed", "passed"]
        # 1 sef + 2 hamal + 1 birlestirici; no kalfa (empty acceptance)
        assert len(report["usage"]["calls"]) == 4
        assert [c["role"] for c in report["usage"]["calls"]] == [
            "sef",
            "hamal",
            "hamal",
            "birlestirici",
        ]

    def test_pieces_spread_over_cheap_pool(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                return '{"value": 1}'
            return "synthesis"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        hamal_models = {
            c["model"] for c in report["usage"]["calls"] if c["role"] == "hamal"
        }
        assert hamal_models == {"mule-a", "mule-b"}

    def test_hamal_json_mode_and_budget_tokens(self, registry: Registry) -> None:
        pieces = [
            {
                "id": "t-1",
                "instruction": "i",
                "input": {},
                "output_schema": NUM_SCHEMA,
                "acceptance": [],
                "budget_tokens": 256,
            }
        ]

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return '{"value": 1}'
            return "done"

        client = FakeChatClient(handler)
        make_orchestra(registry, client).run("task")

        hamal_call = calls_for(client, "hamal")[0]
        assert hamal_call["json_mode"] is True
        assert hamal_call["max_tokens"] == 256


class TestKalfaRetry:
    def test_schema_violation_retries_with_fix_hint(self, registry: Registry) -> None:
        pieces = [dict(PLAN_TWO["pieces"][0])]
        hamal_replies = iter(['{"wrong": 1}', '{"value": 42}'])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return next(hamal_replies)
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        piece = report["pieces"][0]
        assert piece["status"] == "passed"
        assert len(piece["attempts"]) == 2
        assert piece["attempts"][0]["passed"] is False
        assert "required" in piece["attempts"][0]["reasons"][0]

        # retry carried the verdict back to the worker
        second = calls_for(client, "hamal")[1]
        retry_block = second["messages"][1]["content"]
        assert "fix_hint" in retry_block
        assert "required" in retry_block

    def test_escalation_to_strong_after_max_retries(self, registry: Registry) -> None:
        pieces = [dict(PLAN_TWO["pieces"][0])]

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                # cheap models keep failing; strong (escalation) succeeds
                if resolved.model.name == "brain":
                    return '{"value": 42}'
                return '{"wrong": true}'
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        piece = report["pieces"][0]
        assert piece["status"] == "escalated"
        # 1 initial + 2 retries on cheap, then 1 escalated attempt
        assert [a["tier"] for a in piece["attempts"]] == [
            "cheap",
            "cheap",
            "cheap",
            "strong",
        ]
        assert piece["attempts"][-1]["model"] == "brain"
        assert report["status"] == "ok"

    def test_piece_fails_when_escalation_rejected(self, registry: Registry) -> None:
        pieces = [dict(PLAN_TWO["pieces"][0])]

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return "not json at all"
            raise AssertionError("birlestirici must not run when all pieces fail")

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        piece = report["pieces"][0]
        assert piece["status"] == "failed"
        assert len(piece["attempts"]) == 4  # 3 cheap + 1 strong
        assert report["status"] == "failed"
        assert report["result"] is None

    def test_unparseable_output_counts_as_failed_attempt(
        self, registry: Registry
    ) -> None:
        pieces = [dict(PLAN_TWO["pieces"][0])]
        hamal_replies = iter(["garbage", '{"value": 7}'])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return next(hamal_replies)
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        piece = report["pieces"][0]
        assert piece["status"] == "passed"
        assert len(piece["attempts"]) == 2
        assert (
            "not valid JSON" in piece["attempts"][0]["reasons"][0]
            or "no JSON object" in piece["attempts"][0]["reasons"][0]
        )

    def test_transport_error_is_a_failed_attempt(self, registry: Registry) -> None:
        from orkestra.errors import ChatError

        pieces = [dict(PLAN_TWO["pieces"][0])]
        hamal_replies = iter([ChatError("conn refused"), '{"value": 3}'])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return next(hamal_replies)
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        piece = report["pieces"][0]
        assert piece["status"] == "passed"
        assert "transport error" in piece["attempts"][0]["reasons"][0]


class TestArbitration:
    PIECES_WITH_ACCEPTANCE = [
        {
            "id": "t-1",
            "instruction": "summarize",
            "input": {"urls": ["https://a", "https://b"]},
            "output_schema": {
                "type": "object",
                "properties": {
                    "kaynak": {
                        "type": "string",
                        "x-from-input": "urls",
                    },
                    "ozet": {"type": "string"},
                },
                "required": ["kaynak", "ozet"],
            },
            "acceptance": ["ozet must mention a price"],
        }
    ]

    def test_citation_check_is_deterministic(self, registry: Registry) -> None:
        """x-from-input violations fail without any kalfa model call."""

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES_WITH_ACCEPTANCE)
            if role == "hamal":
                return json.dumps({"kaynak": "https://not-in-input", "ozet": "x"})
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert calls_for(client, "kalfa") == []  # never reached the arbiter
        piece = report["pieces"][0]
        assert piece["status"] == "failed"
        assert "citation" in piece["attempts"][0]["reasons"][0]

    def test_arbiter_fail_then_pass(self, registry: Registry) -> None:
        hamal_replies = iter(
            [
                json.dumps({"kaynak": "https://a", "ozet": "no price"}),
                json.dumps({"kaynak": "https://a", "ozet": "price 10 TL"}),
            ]
        )
        kalfa_verdicts = iter(
            [
                json.dumps(
                    {
                        "pass": False,
                        "reasons": ["ozet mentions no price"],
                        "fix_hint": "include the price",
                    }
                ),
                json.dumps({"pass": True, "reasons": []}),
            ]
        )

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES_WITH_ACCEPTANCE)
            if role == "hamal":
                return next(hamal_replies)
            if role == "kalfa":
                return next(kalfa_verdicts)
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        assert len(calls_for(client, "kalfa")) == 2
        piece = report["pieces"][0]
        assert len(piece["attempts"]) == 2
        # the fix_hint travelled back into the hamal retry
        assert (
            "include the price"
            in calls_for(client, "hamal")[1]["messages"][1]["content"]
        )

    def test_no_arbitrate_marks_unchecked(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES_WITH_ACCEPTANCE)
            if role == "hamal":
                return json.dumps({"kaynak": "https://a", "ozet": "x"})
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client, arbitrate=False).run("task")

        assert calls_for(client, "kalfa") == []
        piece = report["pieces"][0]
        assert piece["status"] == "passed"
        assert piece["unchecked_acceptance"] == ["ozet must mention a price"]

    def test_broken_arbiter_reports_failed_with_usage(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES_WITH_ACCEPTANCE)
            if role == "hamal":
                return json.dumps({"kaynak": "https://a", "ozet": "x"})
            if role == "kalfa":
                return "i am not a verdict"
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        # The run fails loudly, but the report (and the spent budget) survives.
        assert report["status"] == "failed"
        assert any("kalfa arbiter" in e for e in report["errors"])
        assert report["usage"]["calls"]  # sef+hamal+kalfa calls recorded


class TestSef:
    def test_invalid_plan_then_repaired(self, registry: Registry) -> None:
        sef_replies = iter(["not a plan", plan_json()])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return next(sef_replies)
            if role == "hamal":
                return '{"value": 1}'
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        assert len(calls_for(client, "sef")) == 2
        # the retry told the sef what was wrong
        assert (
            "not a valid plan" in calls_for(client, "sef")[1]["messages"][-1]["content"]
        )

    def test_persistent_bad_plan_reports_failed(self, registry: Registry) -> None:
        client = FakeChatClient(lambda resolved, messages: "still garbage")
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "failed"
        assert any("no valid plan" in e for e in report["errors"])
        # the two sef attempts are still in the usage report
        assert len(calls_for(client, "sef")) == 2
        assert report["usage"]["calls"]

    def test_invalid_output_schema_reports_failed(self, registry: Registry) -> None:
        bad = plan_json(
            [
                {
                    "id": "t-1",
                    "instruction": "i",
                    "input": {},
                    "output_schema": {"type": "not-a-type"},
                    "acceptance": [],
                }
            ]
        )
        client = FakeChatClient(
            lambda resolved, messages: bad if role_of(messages) == "sef" else "x"
        )
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "failed"
        assert any("output_schema" in e for e in report["errors"])

    def test_too_many_pieces_reports_failed(self, registry: Registry) -> None:
        many = [
            {
                "id": f"t-{i}",
                "instruction": "i",
                "input": {},
                "output_schema": NUM_SCHEMA,
                "acceptance": [],
            }
            for i in range(10)
        ]

        def handler(resolved, messages):
            return plan_json(many) if role_of(messages) == "sef" else "x"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client, max_pieces=3).run("task")

        assert report["status"] == "failed"
        assert any("pieces" in e for e in report["errors"])


class TestBudget:
    def test_zero_usd_budget_stops_before_first_call(self, registry: Registry) -> None:
        client = FakeChatClient(lambda resolved, messages: "x")
        report = make_orchestra(registry, client, budget_usd=0.0).run("task")

        assert report["status"] == "budget_exceeded"
        assert report["pieces"] == []
        assert client.calls == []

    def test_token_budget_closes_mid_run(self, registry: Registry) -> None:
        # each fake call reports 10+10 tokens; cap at 25 -> sef (20) ok,
        # first hamal ok (40 total), second hamal refused by the valve
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                return '{"value": 1}'
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client, token_budget=25).run("task")

        assert report["status"] == "budget_exceeded"
        assert report["result"] is None
        assert calls_for(client, "birlestirici") == []

    def test_usd_budget_requires_cost_hints(self, api_key_env: str) -> None:
        provider = ProviderConfig(
            name="p", base_url="http://127.0.0.1:1", api_key_env=api_key_env
        )
        config = OrkestraConfig(
            providers={"p": provider},
            models={
                "brain": ModelConfig(name="brain", provider="p", tier=Tier.STRONG),
                "mule": ModelConfig(name="mule", provider="p", tier=Tier.CHEAP),
            },
        )
        registry = Registry(config)
        client = FakeChatClient(lambda resolved, messages: "x")
        with pytest.raises(EngineError, match="cost hints"):
            make_orchestra(registry, client, budget_usd=1.0)


class TestModelSelection:
    def test_no_strong_model(self, api_key_env: str) -> None:
        provider = ProviderConfig(
            name="p", base_url="http://127.0.0.1:1", api_key_env=api_key_env
        )
        config = OrkestraConfig(
            providers={"p": provider},
            models={"mule": ModelConfig(name="mule", provider="p", tier=Tier.CHEAP)},
        )
        with pytest.raises(EngineError, match="strong"):
            Orchestra(Registry(config), FakeChatClient(lambda r, m: "x"))

    def test_no_cheap_model(self, api_key_env: str) -> None:
        provider = ProviderConfig(
            name="p", base_url="http://127.0.0.1:1", api_key_env=api_key_env
        )
        config = OrkestraConfig(
            providers={"p": provider},
            models={"brain": ModelConfig(name="brain", provider="p", tier=Tier.STRONG)},
        )
        with pytest.raises(EngineError, match="cheap"):
            Orchestra(Registry(config), FakeChatClient(lambda r, m: "x"))

    def test_strong_must_be_strong(self, registry: Registry) -> None:
        with pytest.raises(EngineError, match="tier=cheap"):
            Orchestra(
                registry,
                FakeChatClient(lambda r, m: "x"),
                strong_model="mule-a",
            )


class TestSynthesisSeesOnlyPassed:
    def test_failed_pieces_hidden_from_birlestirici(self, registry: Registry) -> None:
        # t-1 passes; t-2 fails everything incl. escalation
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                if (
                    '"t-2"' in messages[1]["content"]
                    or "square" in messages[1]["content"]
                ):
                    return "broken"
                return '{"value": 1}'
            return "synthesis"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "partial"
        piece_status = {p["id"]: p["status"] for p in report["pieces"]}
        assert piece_status == {"t-1": "passed", "t-2": "failed"}

        synth_user = calls_for(client, "birlestirici")[0]["messages"][1]["content"]
        assert '"t-1"' in synth_user
        assert "failed_piece_ids" in synth_user
        # t-2's output is never shown — only its id inside failed_piece_ids
        assert '"worker_output"' not in synth_user

    def test_result_json_is_parsed(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                return '{"value": 1}'
            return {"content": '{"table": [[1, 2]]}'}

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")
        assert report["result"] == {"table": [[1, 2]]}


class TestUsageLogging:
    def test_every_call_logged_with_cost(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json()
            if role == "hamal":
                return '{"value": 1}'
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        calls = report["usage"]["calls"]
        assert len(calls) == 4
        for call in calls:
            assert call["prompt_tokens"] == 10
            assert call["cost_usd"] is not None
            assert call["estimated"] is False
            assert call["model_id"]
        # cost: 10*1 + 10*4 per 1M per call = 0.00005 x4
        assert report["usage"]["totals"]["cost_usd"] == pytest.approx(0.0002)

    def test_missing_usage_is_estimated(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return {
                    "content": plan_json(),
                    "prompt_tokens": None,
                    "completion_tokens": None,
                }
            if role == "hamal":
                return {
                    "content": '{"value": 1}',
                    "prompt_tokens": None,
                    "completion_tokens": None,
                }
            return {"content": "done", "prompt_tokens": None, "completion_tokens": None}

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        assert report["usage"]["totals"]["estimated_calls"] == 4
        assert report["usage"]["totals"]["prompt_tokens"] > 0


class TestRegressionFixes:
    def test_transport_error_rotates_to_next_cheap_model(
        self, registry: Registry
    ) -> None:
        """A dead provider must not burn every retry of its pieces."""
        from orkestra.errors import ChatError

        pieces = [dict(PLAN_TWO["pieces"][0])]
        replies = iter([ChatError("conn refused"), '{"value": 3}'])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return next(replies)
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        hamal_models = [c["model"] for c in calls_for(client, "hamal")]
        assert len(set(hamal_models)) == 2  # attempt 2 ran on the other mule

    def test_over_budget_usd_reported_when_valve_overshot(
        self, registry: Registry
    ) -> None:
        """A single call past the cap shows the overrun amount in the report."""

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json([PLAN_TWO["pieces"][0]])
            if role == "hamal":
                # Oversized usage: sef 20 + hamal 4000 tokens > any cheap cap.
                return {
                    "content": '{"value": 1}',
                    "prompt_tokens": 2000,
                    "completion_tokens": 2000,
                }
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client, budget_usd=0.0001).run("task")

        assert report["status"] == "budget_exceeded"
        assert report["usage"]["over_budget_usd"] > 0

    def test_array_typed_schema_is_rejected_then_repaired(
        self, registry: Registry
    ) -> None:
        """sef's array-typed schema is a plan bug — repair prompt, then pass."""
        bad = json.dumps(
            {
                "pieces": [
                    {
                        "id": "t-1",
                        "instruction": "i",
                        "input": {},
                        "output_schema": {"type": "array"},
                        "acceptance": [],
                    }
                ]
            }
        )
        good = plan_json([PLAN_TWO["pieces"][0]])
        sef_replies = iter([bad, good])

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return next(sef_replies)
            if role == "hamal":
                return '{"value": 1}'
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert report["status"] == "ok"
        assert '"object"' in calls_for(client, "sef")[1]["messages"][-1]["content"]

    def test_citation_violations_survive_schema_noise(self, registry: Registry) -> None:
        """8+ schema errors must not push citation reasons out of the hint."""
        schema = {
            "type": "object",
            "properties": {
                **{f"f{i}": {"type": "integer"} for i in range(10)},
                "kaynak": {"type": "string", "x-from-input": "urls"},
            },
            "required": [f"f{i}" for i in range(10)] + ["kaynak"],
        }
        pieces = [
            {
                "id": "t-1",
                "instruction": "summarize",
                "input": {"urls": ["https://a"]},
                "output_schema": schema,
                "acceptance": [],
            }
        ]

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(pieces)
            if role == "hamal":
                return json.dumps({"kaynak": "https://not-in-input"})
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        piece = report["pieces"][0]
        assert piece["status"] == "failed"
        # citation feedback reached the hamal despite the schema flood
        retry_prompt = calls_for(client, "hamal")[1]["messages"][1]["content"]
        assert "citation" in retry_prompt


class TestRunAbort:
    """An EngineError inside one piece aborts the run cooperatively.

    Regression: the old code called ``pool.shutdown(cancel_futures=True)``
    inside the ``as_completed`` loop, which deadlocked the iteration —
    pending futures cancelled by the executor never notify the as_completed
    waiter, so ``orkestra run`` hung forever on a broken arbiter.
    """

    PIECES = [
        {
            "id": f"t-{i}",
            "instruction": "work",
            "input": {},
            "output_schema": NUM_SCHEMA,
            "acceptance": [] if i == 1 else ["looks fine"],
        }
        for i in range(1, 5)
    ]

    def _run_in_thread(self, orchestra: Orchestra, timeout: float = 10.0):
        """Run the orchestra off-thread so a regression hang fails, not stalls."""
        box: dict[str, Any] = {}

        def go() -> None:
            try:
                box["report"] = orchestra.run("task")
            except BaseException as exc:  # noqa: BLE001 — capture for assertion
                box["exc"] = exc

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        thread.join(timeout)
        assert not thread.is_alive(), "run() did not return within the timeout"
        if "exc" in box:
            raise box["exc"]
        return box["report"]

    def test_abort_returns_and_marks_queued_pieces_cancelled(
        self, registry: Registry
    ) -> None:
        """Dead arbiter + more pieces than workers: queued pieces report
        'cancelled', the run returns a partial report, no hang."""

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES)
            if role == "hamal":
                return '{"value": 1}'
            if role == "kalfa":
                return ChatError("arbiter endpoint dead")
            if role == "birlestirici":
                return "synthesis"
            raise AssertionError(role)

        client = FakeChatClient(handler)
        report = self._run_in_thread(make_orchestra(registry, client, max_parallel=1))

        piece_status = {p["id"]: p["status"] for p in report["pieces"]}
        # every piece appears in the report
        assert set(piece_status) == {"t-1", "t-2", "t-3", "t-4"}
        assert piece_status["t-1"] == "passed"
        assert piece_status["t-2"] == "failed"
        assert piece_status["t-3"] == "cancelled"
        assert piece_status["t-4"] == "cancelled"
        # abort + a usable synthesis -> partial, not a blanket "failed"
        assert report["status"] == "partial"
        assert report["result"] == "synthesis"
        assert report["errors"]

    def test_keyboardinterrupt_in_piece_propagates(self, registry: Registry) -> None:
        """Ctrl-C semantics must survive the worker pool: it is re-raised,
        not swallowed into a 'failed' report."""

        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self.PIECES)
            if role == "hamal":
                raise KeyboardInterrupt
            return "done"

        client = FakeChatClient(handler)
        with pytest.raises(KeyboardInterrupt):
            make_orchestra(registry, client).run("task")


class TestCitationList:
    """x-from-input on an array property: every element must be in the input."""

    LIST_SCHEMA = {
        "type": "object",
        "properties": {
            "kaynaklar": {
                "type": "array",
                "items": {"type": "string"},
                "x-from-input": "urls",
            },
            "ozet": {"type": "string"},
        },
        "required": ["kaynaklar", "ozet"],
    }

    def _piece(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "t-1",
                "instruction": "summarize",
                "input": {"urls": ["https://a", "https://b"]},
                "output_schema": self.LIST_SCHEMA,
                "acceptance": [],
            }
        ]

    def test_citation_list_all_members_pass(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self._piece())
            if role == "hamal":
                return json.dumps(
                    {"kaynaklar": ["https://a", "https://b"], "ozet": "x"}
                )
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")
        assert report["pieces"][0]["status"] == "passed"

    def test_citation_list_with_stranger_fails(self, registry: Registry) -> None:
        def handler(resolved, messages):
            role = role_of(messages)
            if role == "sef":
                return plan_json(self._piece())
            if role == "hamal":
                return json.dumps(
                    {"kaynaklar": ["https://a", "https://invented"], "ozet": "x"}
                )
            return "done"

        client = FakeChatClient(handler)
        report = make_orchestra(registry, client).run("task")

        assert calls_for(client, "kalfa") == []
        piece = report["pieces"][0]
        assert piece["status"] == "failed"
        assert "https://invented" in piece["attempts"][0]["reasons"][0]
