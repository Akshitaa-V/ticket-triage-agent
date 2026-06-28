# Ticket Triage Agent — LangGraph + n8n

A small project combining LangGraph and n8n: an n8n webhook receives a
support ticket and forwards it to a Python service that runs a LangGraph
agent, which classifies the ticket and decides what to do with it.

## How it works

```
n8n Webhook (POST) -> HTTP Request -> FastAPI -> LangGraph StateGraph -> JSON response -> If (escalated?) -> Slack-style notification
```

- n8n receives a ticket as JSON through a webhook and forwards it to a local
  Python service via an HTTP Request node.
- The Python service runs a LangGraph `StateGraph` with four nodes:
  `classify` -> `prioritize` -> a conditional edge -> `escalate` or
  `auto_respond`.
- The result (category, priority, action, response) is sent back as JSON,
  through n8n.
- An `If` node checks whether the action was `escalated_to_human`. If so,
  a second HTTP Request node fires, sending a JSON payload in the same
  shape Slack's incoming webhooks expect (a `text` field with the ticket
  details filled in).

## About the Slack step

I don't have a Slack workspace set up for this project, so the
notification step posts to a webhook.site test URL instead of a real
Slack channel — I used webhook.site to confirm the payload is correctly
formatted and the request actually fires when a ticket escalates. The
node is built exactly the way it would be for a real Slack incoming
webhook; pointing it at an actual Slack webhook URL instead of the test
one is the only change needed to make it live.

## Why no real LLM call

I wanted to get the LangGraph and n8n wiring working correctly first,
before adding a model into the mix. The classify/prioritize nodes use
keyword-based rules instead of an LLM call, so the whole thing runs
offline with no API key. The graph structure — state, nodes, conditional
edges — is the same as it would be with an LLM node; swapping in a real
`llm.invoke(...)` call for one of the rule-based functions wouldn't change
the rest of the graph at all. That's the next thing I'd add if I kept
going with this.

## Running it

1. Install dependencies:
   ```
   pip install langgraph fastapi uvicorn
   ```
2. Start the agent service:
   ```
   python ticket_agent.py
   ```
   Runs on `http://localhost:8001`.
3. Start n8n locally (no signup, no cloud):
   ```
   npx n8n
   ```
   Opens the editor at `http://localhost:5678`.
4. In the n8n editor, build the workflow:
   - **Webhook** node: method `POST`, path `new-ticket`
   - **HTTP Request** node: method `POST`, URL `http://127.0.0.1:8001/triage`,
     body set to JSON, body content = `{{ $json.body }}`
   - **If** node: condition `{{ $json.action }}` is equal to `escalated_to_human`
   - **HTTP Request** node (on the `true` branch): method `POST`, URL =
     your webhook.site test URL (or a real Slack incoming webhook URL),
     body set to JSON:
     ```
     {
       "text": "Ticket {{ $('Webhook').item.json.body.ticket_id }} escalated to human review — category: {{ $('If').item.json.category }}, priority: {{ $('If').item.json.priority }}"
     }
     ```

   (Note: use `127.0.0.1` rather than `localhost` in the first HTTP Request
   node's URL — n8n didn't resolve `localhost` correctly for me locally,
   `127.0.0.1` fixed it.)
5. Test it:
   ```
   curl -X POST http://localhost:5678/webhook-test/new-ticket -H "Content-Type: application/json" -d "{\"ticket_id\": \"T-101\", \"text\": \"My payment failed twice, please refund me\"}"
   ```
   Expected result: classified as `billing`, `high` priority, `escalated_to_human`.

## Files

- `ticket_agent.py` — LangGraph agent + FastAPI service
- `n8n_workflow.json` — exported n8n workflow
