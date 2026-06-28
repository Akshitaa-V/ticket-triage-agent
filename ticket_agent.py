"""
Ticket Triage Agent — LangGraph + FastAPI

A small stateful agent built with LangGraph that classifies an incoming
support ticket, decides a routing action, and drafts a structured response.

No paid API / no LLM key required: the "reasoning" steps are rule-based
functions standing in for an LLM call, which keeps the graph runnable
fully offline. The graph structure (state, nodes, conditional edges) is
the same pattern used with a real LLM-backed node — swapping a rule-based
function for `llm.invoke(...)` inside a node would make this LLM-backed
without changing the graph topology at all.

Run:
    python ticket_agent.py
Then POST to http://localhost:8001/triage with JSON like:
    {"ticket_id": "T-101", "text": "My payment failed twice, please refund me"}
"""

from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END
from fastapi import FastAPI
import uvicorn


# ---------- 1. Define the agent state ----------

class TicketState(TypedDict):
    ticket_id: str
    text: str
    category: str
    priority: str
    action: str
    response: str


# ---------- 2. Define nodes (each is one step of multi-step reasoning) ----------

def classify_node(state: TicketState) -> TicketState:
    """Classify the ticket into a category based on keyword evidence."""
    text = state["text"].lower()
    if any(w in text for w in ["refund", "payment", "charge", "billing"]):
        category = "billing"
    elif any(w in text for w in ["bug", "error", "crash", "broken"]):
        category = "technical"
    elif any(w in text for w in ["cancel", "unsubscribe", "delete account"]):
        category = "account"
    else:
        category = "general"
    return {**state, "category": category}


def prioritize_node(state: TicketState) -> TicketState:
    """Assign priority using simple constraint checks (urgency signals)."""
    text = state["text"].lower()
    urgent_signals = ["urgent", "asap", "immediately", "twice", "again", "still not"]
    priority = "high" if any(s in text for s in urgent_signals) else "normal"
    return {**state, "priority": priority}


def route_decision(state: TicketState) -> Literal["escalate", "auto_respond"]:
    """Conditional edge: decide the next node based on accumulated state."""
    if state["priority"] == "high" or state["category"] == "billing":
        return "escalate"
    return "auto_respond"


def escalate_node(state: TicketState) -> TicketState:
    action = "escalated_to_human"
    response = (
        f"Ticket {state['ticket_id']} flagged for human review "
        f"(category={state['category']}, priority={state['priority']})."
    )
    return {**state, "action": action, "response": response}


def auto_respond_node(state: TicketState) -> TicketState:
    action = "auto_acknowledged"
    response = (
        f"Ticket {state['ticket_id']} auto-acknowledged "
        f"(category={state['category']}, priority={state['priority']})."
    )
    return {**state, "action": action, "response": response}


# ---------- 3. Build the graph ----------

def build_graph():
    graph = StateGraph(TicketState)

    graph.add_node("classify", classify_node)
    graph.add_node("prioritize", prioritize_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("auto_respond", auto_respond_node)

    graph.set_entry_point("classify")
    graph.add_edge("classify", "prioritize")
    graph.add_conditional_edges(
        "prioritize",
        route_decision,
        {"escalate": "escalate", "auto_respond": "auto_respond"},
    )
    graph.add_edge("escalate", END)
    graph.add_edge("auto_respond", END)

    return graph.compile()


agent = build_graph()

# ---------- 4. Expose it over HTTP so n8n (or anything else) can call it ----------

app = FastAPI(title="Ticket Triage Agent")


@app.post("/triage")
def triage(payload: dict):
    initial_state: TicketState = {
        "ticket_id": payload.get("ticket_id", "UNKNOWN"),
        "text": payload.get("text", ""),
        "category": "",
        "priority": "",
        "action": "",
        "response": "",
    }
    result = agent.invoke(initial_state)
    # Structured output — exactly what an n8n HTTP node expects back
    return {
        "ticket_id": result["ticket_id"],
        "category": result["category"],
        "priority": result["priority"],
        "action": result["action"],
        "response": result["response"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
