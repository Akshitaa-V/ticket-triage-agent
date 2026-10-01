"""
Tests for the async service. No API key and no network needed:

- The LLM path runs through the real `litellm.acompletion`, using LiteLLM's
  built-in `mock_response` argument, so the provider-routing code is real.
- Timeouts, provider errors and concurrency use small async fakes.
- Spans go to an in-memory OpenTelemetry exporter, so the tests can check
  exactly what Langfuse would receive.
"""

import asyncio
import json
import time

import litellm
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import llm_classifier
from async_service import app, triage_one
from llm_classifier import parse_classification
from tracing import configure_tracing

EXPORTER = InMemorySpanExporter()
configure_tracing(exporter=EXPORTER)

BILLING_HIGH = json.dumps(
    {"category": "billing", "priority": "high", "reason": "Payment failed twice."}
)
GENERAL_NORMAL = json.dumps(
    {"category": "general", "priority": "normal", "reason": "Simple question."}
)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    EXPORTER.clear()
    monkeypatch.setattr(llm_classifier, "_acompletion", litellm.acompletion)
    monkeypatch.setattr(llm_classifier, "EXTRA_KWARGS", {})
    monkeypatch.delenv("TRIAGE_MODELS", raising=False)
    monkeypatch.setenv("TRIAGE_LLM_TIMEOUT", "5")
    yield


def spans_named(name):
    return [s for s in EXPORTER.get_finished_spans() if s.name == name]


# ---------- parsing ----------

def test_parse_accepts_json_inside_extra_text():
    out = parse_classification('Sure! {"category": "Technical", "priority": "HIGH"}', "m")
    assert (out.category, out.priority) == ("technical", "high")


@pytest.mark.parametrize(
    "content",
    ["not json at all", '{"category": "sales", "priority": "high"}',
     '{"category": "billing", "priority": "urgent"}'],
)
def test_parse_rejects_off_schema_output(content):
    with pytest.raises(ValueError):
        parse_classification(content, "m")


# ---------- LLM path through real LiteLLM ----------

def test_llm_classification_drives_routing(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setattr(llm_classifier, "EXTRA_KWARGS", {"mock_response": GENERAL_NORMAL})
    # Text would be "billing" under the keyword rules; the LLM's answer must win.
    result = asyncio.run(triage_one({"ticket_id": "T-1", "text": "question about my payment page"}))
    assert result.classified_by == "llm:openai/gpt-4o-mini"
    assert result.category == "general"
    assert result.action == "auto_acknowledged"


def test_llm_span_has_langfuse_generation_attributes(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "anthropic/claude-sonnet-4-5")
    monkeypatch.setattr(llm_classifier, "EXTRA_KWARGS", {"mock_response": BILLING_HIGH})
    asyncio.run(triage_one({"ticket_id": "T-2", "text": "charged twice, refund asap"}))

    (llm_span,) = spans_named("llm.classify")
    attrs = llm_span.attributes
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["gen_ai.request.model"] == "anthropic/claude-sonnet-4-5"
    assert "refund" in attrs["langfuse.observation.input"]
    assert json.loads(attrs["langfuse.observation.output"])["category"] == "billing"
    assert attrs["gen_ai.usage.input_tokens"] >= 0
    assert attrs["gen_ai.usage.output_tokens"] >= 0


def test_spans_form_one_trace_request_node_llm(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setattr(llm_classifier, "EXTRA_KWARGS", {"mock_response": BILLING_HIGH})
    asyncio.run(triage_one({"ticket_id": "T-3", "text": "refund please"}))

    (root,) = spans_named("triage")
    (node,) = spans_named("node.classify")
    (llm,) = spans_named("llm.classify")
    (esc,) = spans_named("node.escalate")
    assert {s.context.trace_id for s in (root, node, llm, esc)} == {root.context.trace_id}
    assert llm.parent.span_id == node.context.span_id
    assert esc.attributes["triage.action"] == "escalated_to_human"


# ---------- fallbacks ----------

def test_falls_back_to_second_model_when_first_errors(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/broken-model,gemini/gemini-2.0-flash")

    async def flaky(model, **kwargs):
        if model.startswith("openai/"):
            raise RuntimeError("provider down")
        return await litellm.acompletion(model=model, mock_response=BILLING_HIGH, **kwargs)

    monkeypatch.setattr(llm_classifier, "_acompletion", flaky)
    result = asyncio.run(triage_one({"ticket_id": "T-4", "text": "refund"}))
    assert result.classified_by == "llm:gemini/gemini-2.0-flash"
    errored = [s for s in spans_named("llm.classify") if not s.status.is_ok]
    assert len(errored) == 1 and errored[0].attributes["gen_ai.request.model"] == "openai/broken-model"


def test_invalid_json_from_every_model_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setattr(llm_classifier, "EXTRA_KWARGS", {"mock_response": "I think it's billing"})
    result = asyncio.run(triage_one({"ticket_id": "T-5", "text": "refund my payment"}))
    assert result.classified_by == "rules"
    assert result.action == "escalated_to_human"  # same as v1 rules


def test_llm_timeout_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setenv("TRIAGE_LLM_TIMEOUT", "0.1")

    async def slow(**kwargs):
        await asyncio.sleep(2)

    monkeypatch.setattr(llm_classifier, "_acompletion", slow)
    start = time.perf_counter()
    result = asyncio.run(triage_one({"ticket_id": "T-6", "text": "what are your hours"}))
    assert time.perf_counter() - start < 1.0
    assert result.classified_by == "rules"
    assert result.action == "auto_acknowledged"
    (llm_span,) = spans_named("llm.classify")
    assert llm_span.status.description.startswith("TimeoutError")


def test_no_models_configured_matches_v1_rules():
    result = asyncio.run(triage_one({"ticket_id": "T-7", "text": "the app keeps crashing"}))
    assert (result.category, result.classified_by) == ("technical", "rules")
    assert spans_named("llm.classify") == []


# ---------- HTTP API ----------

def test_guard_blocks_malformed_ticket_over_http():
    with TestClient(app) as client:
        body = client.post("/triage", json={"ticket_id": "T-8"}).json()
    assert body["action"] == "blocked_by_guard"
    assert "text" in body["response"]


def test_batch_runs_concurrently(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setenv("TRIAGE_MAX_CONCURRENCY", "5")

    async def slow_ok(model, **kwargs):
        await asyncio.sleep(0.3)
        return await litellm.acompletion(model=model, mock_response=GENERAL_NORMAL, **kwargs)

    monkeypatch.setattr(llm_classifier, "_acompletion", slow_ok)
    tickets = [{"ticket_id": f"B-{i}", "text": "hello"} for i in range(5)]
    with TestClient(app) as client:
        start = time.perf_counter()
        body = client.post("/triage/batch", json={"tickets": tickets}).json()
        elapsed = time.perf_counter() - start
    assert [r["ticket_id"] for r in body] == [t["ticket_id"] for t in tickets]
    assert elapsed < 0.9  # 5 x 0.3 s would be 1.5 s if run one after another


def test_batch_respects_concurrency_limit(monkeypatch):
    monkeypatch.setenv("TRIAGE_MODELS", "openai/gpt-4o-mini")
    monkeypatch.setenv("TRIAGE_MAX_CONCURRENCY", "2")
    in_flight = peak = 0

    async def counting(model, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return await litellm.acompletion(model=model, mock_response=GENERAL_NORMAL, **kwargs)

    monkeypatch.setattr(llm_classifier, "_acompletion", counting)
    tickets = [{"ticket_id": f"C-{i}", "text": "hi"} for i in range(6)]
    with TestClient(app) as client:
        assert client.post("/triage/batch", json={"tickets": tickets}).status_code == 200
    assert peak == 2
