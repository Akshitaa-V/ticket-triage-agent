"""
Ticket Triage Agent v2 -- async FastAPI + LiteLLM + OpenTelemetry/Langfuse

Same graph shape as ticket_agent.py (guard -> classify -> route -> escalate
or auto_respond), with three changes:

1. Async end to end. FastAPI endpoints and LangGraph nodes are `async def`,
   the LLM call is awaited, and POST /triage/batch triages many tickets
   concurrently with asyncio.gather, capped by an asyncio.Semaphore.
2. The classify node calls an LLM through LiteLLM (provider set by
   TRIAGE_MODELS), with fallback to the next model and finally to the
   original keyword rules. The response says which one was used.
3. Every request, graph node and LLM call is an OpenTelemetry span,
   exported to Langfuse when LANGFUSE_* keys are set.

The v1 service (ticket_agent.py) and its n8n workflow are unchanged.

Run:
    uvicorn async_service:app --port 8002
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal, TypedDict

from fastapi import FastAPI
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from llm_classifier import classify_with_llm
from ticket_agent import (
    auto_respond_node,
    classify_node as rule_classify_node,
    escalate_node,
    input_guard,
    prioritize_node as rule_prioritize_node,
    route_decision,
)
from tracing import configure_tracing, get_tracer, shutdown_tracing

def max_concurrency() -> int:
    return int(os.getenv("TRIAGE_MAX_CONCURRENCY", "5"))


class TicketState(TypedDict):
    ticket_id: str
    text: str
    category: str
    priority: str
    action: str
    response: str
    classified_by: str  # "llm:<model>" or "rules"
    reason: str


# ---------- Graph nodes ----------

async def classify_node(state: TicketState) -> TicketState:
    """LLM first; if every configured model fails, use the v1 keyword rules."""
    span_tracer = get_tracer()
    with span_tracer.start_as_current_span("node.classify") as span:
        result = await classify_with_llm(state["text"])
        if result is not None:
            new_state = {
                **state,
                "category": result.category,
                "priority": result.priority,
                "classified_by": f"llm:{result.model}",
                "reason": result.reason,
            }
        else:
            new_state = rule_prioritize_node(rule_classify_node(state))
            new_state = {**new_state, "classified_by": "rules", "reason": "keyword rules"}
        span.set_attribute("triage.category", new_state["category"])
        span.set_attribute("triage.priority", new_state["priority"])
        span.set_attribute("triage.classified_by", new_state["classified_by"])
        return new_state


def _traced(name: str, fn):
    """Wrap a synchronous v1 node in a span without changing its logic."""

    async def node(state: TicketState) -> TicketState:
        with get_tracer().start_as_current_span(f"node.{name}") as span:
            new_state = fn(state)
            span.set_attribute("triage.action", new_state.get("action", ""))
            return new_state

    return node


def route(state: TicketState) -> Literal["escalate", "auto_respond"]:
    return route_decision(state)


def build_graph():
    graph = StateGraph(TicketState)
    graph.add_node("classify", classify_node)
    graph.add_node("escalate", _traced("escalate", escalate_node))
    graph.add_node("auto_respond", _traced("auto_respond", auto_respond_node))
    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify", route, {"escalate": "escalate", "auto_respond": "auto_respond"}
    )
    graph.add_edge("escalate", END)
    graph.add_edge("auto_respond", END)
    return graph.compile()


agent = build_graph()


# ---------- Triage one ticket (shared by both endpoints) ----------

class TriageResult(BaseModel):
    ticket_id: str
    category: str
    priority: str
    action: str
    response: str
    classified_by: str
    reason: str


async def triage_one(payload: dict) -> TriageResult:
    with get_tracer().start_as_current_span("triage") as span:
        ticket_id = str(payload.get("ticket_id", "UNKNOWN"))
        span.set_attribute("triage.ticket_id", ticket_id)

        guard = input_guard(payload)
        if not guard.allowed:
            span.set_attribute("triage.action", "blocked_by_guard")
            return TriageResult(
                ticket_id=ticket_id, category="", priority="", action="blocked_by_guard",
                response=guard.reason, classified_by="", reason="",
            )

        state: TicketState = {
            "ticket_id": ticket_id, "text": str(payload["text"]), "category": "",
            "priority": "", "action": "", "response": "", "classified_by": "", "reason": "",
        }
        result = await agent.ainvoke(state)
        span.set_attribute("triage.action", result["action"])
        return TriageResult(**{k: result[k] for k in TriageResult.model_fields})


# ---------- HTTP API ----------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_tracing()
    yield
    shutdown_tracing()  # flush spans to Langfuse before exit


app = FastAPI(title="Ticket Triage Agent v2 (async)", lifespan=lifespan)


@app.post("/triage", response_model=TriageResult)
async def triage(payload: dict) -> TriageResult:
    return await triage_one(payload)


class BatchRequest(BaseModel):
    tickets: list[dict]


@app.post("/triage/batch", response_model=list[TriageResult])
async def triage_batch(body: BatchRequest) -> list[TriageResult]:
    """Triage tickets concurrently; at most TRIAGE_MAX_CONCURRENCY in flight."""
    semaphore = asyncio.Semaphore(max_concurrency())

    async def limited(payload: dict) -> TriageResult:
        async with semaphore:
            return await triage_one(payload)

    return list(await asyncio.gather(*(limited(t) for t in body.tickets)))


@app.get("/health")
async def health():
    return {"status": "ok"}
