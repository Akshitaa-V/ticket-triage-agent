"""
LLM-backed ticket classification through LiteLLM.

LiteLLM gives one async call, `litellm.acompletion`, for OpenAI, Anthropic,
Gemini and many other providers. The provider is chosen by the model string
alone, e.g.

    openai/gpt-4o-mini
    anthropic/claude-sonnet-4-5
    gemini/gemini-2.0-flash

TRIAGE_MODELS is a comma-separated list tried in order: if the first model
times out, errors or returns something that isn't valid JSON, the next one
is tried. If every model fails (or TRIAGE_MODELS is empty), the caller falls
back to the original keyword rules, so a ticket is never left unclassified.

Every model call is wrapped in an OpenTelemetry span marked as a Langfuse
"generation", with the model name, prompt, completion and token usage.
"""

import asyncio
import json
import os
from dataclasses import dataclass

import litellm
from opentelemetry.trace import Status, StatusCode

from tracing import get_tracer

CATEGORIES = {"billing", "technical", "account", "general"}
PRIORITIES = {"high", "normal"}

SYSTEM_PROMPT = (
    "You triage customer support tickets. Reply with JSON only, no prose:\n"
    '{"category": "billing|technical|account|general", '
    '"priority": "high|normal", "reason": "<one short sentence>"}\n'
    "Use priority high for urgent language, repeated failures or money at risk."
)

# Indirection so tests can swap in a slow or failing fake without a network call.
_acompletion = litellm.acompletion
# Extra kwargs for every call; tests set {"mock_response": "..."} here so the
# real LiteLLM code path runs without an API key.
EXTRA_KWARGS: dict = {}


@dataclass
class LLMClassification:
    category: str
    priority: str
    reason: str
    model: str


def configured_models() -> list[str]:
    raw = os.getenv("TRIAGE_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def llm_timeout_seconds() -> float:
    return float(os.getenv("TRIAGE_LLM_TIMEOUT", "10"))


def parse_classification(content: str, model: str) -> LLMClassification:
    """Parse and validate the model's JSON. Raises ValueError on anything off-schema."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    data = json.loads(content[start : end + 1])
    category = str(data.get("category", "")).lower().strip()
    priority = str(data.get("priority", "")).lower().strip()
    if category not in CATEGORIES:
        raise ValueError(f"invalid category: {category!r}")
    if priority not in PRIORITIES:
        raise ValueError(f"invalid priority: {priority!r}")
    return LLMClassification(category, priority, str(data.get("reason", ""))[:300], model)


async def _classify_with_model(model: str, text: str) -> LLMClassification:
    tracer = get_tracer()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    with tracer.start_as_current_span("llm.classify") as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("langfuse.observation.input", json.dumps(messages))
        try:
            response = await asyncio.wait_for(
                _acompletion(model=model, messages=messages, temperature=0, **EXTRA_KWARGS),
                timeout=llm_timeout_seconds(),
            )
            content = response.choices[0].message.content or ""
            span.set_attribute("langfuse.observation.output", content)
            usage = getattr(response, "usage", None)
            if usage is not None:
                span.set_attribute("gen_ai.usage.input_tokens", int(usage.prompt_tokens or 0))
                span.set_attribute("gen_ai.usage.output_tokens", int(usage.completion_tokens or 0))
            return parse_classification(content, model)
        except Exception as exc:  # timeout, provider error or bad JSON
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


async def classify_with_llm(text: str) -> LLMClassification | None:
    """Try each configured model in order. Returns None if all of them fail."""
    for model in configured_models():
        try:
            return await _classify_with_model(model, text)
        except Exception:
            continue  # already recorded on the span; try the next model
    return None
