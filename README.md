# Ticket Triage Agent — LangGraph + n8n

A minimal, fully local demonstration of an n8n workflow triggering a real
LangGraph-based agent over HTTP, end to end.

## Architecture

```
[ n8n: Webhook node ] --POST JSON--> [ FastAPI service ] --invoke--> [ LangGraph StateGraph ] --JSON--> back through n8n
```

- **n8n** receives an incoming "support ticket" via a Webhook node and forwards it
  via an HTTP Request node to a local Python service.
- **LangGraph** (`ticket_agent.py`) runs a real `StateGraph` with multiple nodes
  (`classify` → `prioritize` → conditional routing → `escalate` / `auto_respond`)
  and returns a structured JSON result.
- The "reasoning" inside each node is rule-based rather than an LLM call, so the
  whole thing runs offline with no API key. The graph topology — state,
  nodes, conditional edges — is identical to what you'd use with an LLM-backed
  node; only the function body inside each node would change.

## Why rule-based nodes instead of an LLM call

This project is meant to demonstrate the LangGraph **pattern** (stateful,
multi-step, tool-and-decision-oriented agent graphs) without requiring a paid
API key or external signup. Swapping any node's rule-based function for
`llm.invoke(prompt)` would make it LLM-backed without changing the graph
structure, edges, or state shape at all.

## Run it yourself

**1. Start the LangGraph service:**
```bash
pip install langgraph fastapi uvicorn
python ticket_agent.py
```
This serves `POST /triage` on `http://localhost:8001`.

**2. Start n8n locally (no signup, no cloud):**
```bash
npx n8n
```
Opens the editor at `http://localhost:5678`.

**3. Build the n8n workflow:**
- **Webhook node**: POST trigger, e.g. path `/new-ticket`
- **HTTP Request node**: POST to `http://localhost:8001/triage`, body = incoming webhook JSON
- (Optional) **Set node**: pass through / log the structured response

**4. Test:**
```bash
curl -X POST http://localhost:5678/webhook-test/new-ticket \
  -H "Content-Type: application/json" \
  -d '{"ticket_id": "T-101", "text": "My payment failed twice, please refund me"}'
```

Expected response (from LangGraph, relayed through n8n):
```json
{
  "ticket_id": "T-101",
  "category": "billing",
  "priority": "high",
  "action": "escalated_to_human",
  "response": "Ticket T-101 flagged for human review (category=billing, priority=high)."
}
```

## Files

- `ticket_agent.py` — LangGraph agent + FastAPI wrapper
- `n8n_workflow.json` — exported n8n workflow (Webhook → HTTP Request)
