\# Ticket Triage Agent — LangGraph + n8n



A small project combining LangGraph and n8n: an n8n webhook receives a

support ticket and forwards it to a Python service that runs a LangGraph

agent, which classifies the ticket and decides what to do with it.



\## How it works



```

n8n Webhook (POST) -> HTTP Request -> FastAPI -> LangGraph StateGraph -> JSON response -> If (escalated?) -> Slack-style notification

```



\- n8n receives a ticket as JSON through a webhook and forwards it to a local

&#x20; Python service via an HTTP Request node.

\- The Python service runs an input guard first (see "Guard Logic" below),

&#x20; then a LangGraph `StateGraph` with four nodes: `classify` -> `prioritize`

&#x20; -> a conditional edge -> `escalate` or `auto\_respond`.

\- The result (category, priority, action, response) is sent back as JSON,

&#x20; through n8n.

\- An `If` node checks whether the action was `escalated\_to\_human`. If so,

&#x20; a second HTTP Request node fires, sending a JSON payload in the same

&#x20; shape Slack's incoming webhooks expect (a `text` field with the ticket

&#x20; details filled in).



\## About the Slack step



I don't have a Slack workspace set up for this project, so the

notification step posts to a webhook.site test URL instead of a real

Slack channel — I used webhook.site to confirm the payload is correctly

formatted and the request actually fires when a ticket escalates. The

node is built exactly the way it would be for a real Slack incoming

webhook; pointing it at an actual Slack webhook URL instead of the test

one is the only change needed to make it live.



\## Why no real LLM call



I wanted to get the LangGraph and n8n wiring working correctly first,

before adding a model into the mix. The classify/prioritize nodes use

keyword-based rules instead of an LLM call, so the whole thing runs

offline with no API key. The graph structure — state, nodes, conditional

edges — is the same as it would be with an LLM node; swapping in a real

`llm.invoke(...)` call for one of the rule-based functions wouldn't change

the rest of the graph at all. That's the next thing I'd add if I kept

going with this.



\## Guard Logic



Before any ticket reaches the LangGraph classification/routing nodes, an

`input\_guard()` check runs against the raw incoming payload. It blocks the

request immediately if required fields (`ticket\_id`, `text`) are missing or

empty, returning a structured `blocked\_by\_guard` result with a specific

reason instead of letting malformed data flow into `classify\_node`.



This is deliberately separate from the existing routing logic (`route\_decision`,

which decides WHERE a valid ticket goes) and the retry logic on the n8n side

(which handles failures AFTER a call). The guard's only job is to stop bad

input from triggering any action — classification, escalation, or auto-response

— in the first place.



Verified two ways: a 6-test pytest suite (`test\_ticket\_agent.py`) covering

guard behavior and existing routing logic, and a live curl test against the

running FastAPI server confirming a malformed ticket (missing `text`) is

blocked while a valid ticket still escalates normally.



\## Running it



Install dependencies:

```

pip install langgraph fastapi uvicorn pytest

```



Start the agent service:

```

python ticket\_agent.py

```

Runs on `http://localhost:8001`.



Start n8n locally (no signup, no cloud):

```

npx n8n

```

Opens the editor at `http://localhost:5678`.



In the n8n editor, build the workflow:

\- Webhook node: method `POST`, path `new-ticket`

\- HTTP Request node: method `POST`, URL `http://127.0.0.1:8001/triage`,

&#x20; body set to JSON, body content = `{{ $json.body }}`

\- If node: condition `{{ $json.action }}` is equal to `escalated\_to\_human`

\- HTTP Request node (on the `true` branch): method `POST`, URL =

&#x20; your webhook.site test URL (or a real Slack incoming webhook URL),

&#x20; body set to JSON:

&#x20; ```

&#x20; {

&#x20;   "text": "Ticket {{ $('Webhook').item.json.body.ticket\_id }} escalated to human review — category: {{ $('If').item.json.category }}, priority: {{ $('If').item.json.priority }}"

&#x20; }

&#x20; ```



(Note: use `127.0.0.1` rather than `localhost` in the first HTTP Request

node's URL — n8n didn't resolve `localhost` correctly for me locally,

`127.0.0.1` fixed it.)



Test it:

```

curl -X POST http://localhost:5678/webhook-test/new-ticket -H "Content-Type: application/json" -d "{\\"ticket\_id\\": \\"T-101\\", \\"text\\": \\"My payment failed twice, please refund me\\"}"

```

Expected result: classified as `billing`, `high` priority, `escalated\_to\_human`.



Test the guard directly against the agent service:

```

curl -X POST http://localhost:8001/triage -H "Content-Type: application/json" -d "{\\"ticket\_id\\": \\"T-999\\"}"

```

Expected result: `blocked\_by\_guard`, since the `text` field is missing.



Run the test suite:

```

pytest -v

```

Expected result: 6 tests passed.



\## Files



\- `ticket\_agent.py` — LangGraph agent + FastAPI service, with guard logic

\- `test\_ticket\_agent.py` — pytest suite covering guard behavior and routing logic

\- `n8n\_workflow.json` — exported n8n workflow



## v2: async FastAPI + LiteLLM + OpenTelemetry/Langfuse

`async_service.py` is a second version of the service that adds the LLM call
I left out of v1, plus the tooling I'd want around it in production. The v1
service (`ticket_agent.py`) and the n8n workflow are unchanged, and v2 reuses
v1's guard, routing and response nodes as they are.

```
POST /triage or /triage/batch
  -> input_guard (v1)
  -> classify (async): LiteLLM model 1 -> model 2 -> ... -> v1 keyword rules
  -> route_decision (v1) -> escalate / auto_respond (v1)
every step is an OpenTelemetry span -> Langfuse
```

**Async.** The endpoints and graph nodes are `async def`, and the graph runs
with `agent.ainvoke`. `POST /triage/batch` triages a list of tickets
concurrently with `asyncio.gather`, with an `asyncio.Semaphore` capping how
many are in flight (`TRIAGE_MAX_CONCURRENCY`, default 5). Each model call has
a timeout through `asyncio.wait_for` (`TRIAGE_LLM_TIMEOUT`, default 10 s).

**LiteLLM.** The classify node calls `litellm.acompletion`, so switching
between OpenAI, Anthropic and Gemini is only a change to the model string.
`TRIAGE_MODELS` is a comma-separated list tried in order. If a model errors,
times out or returns JSON outside the allowed categories, the next one is
tried. If all of them fail, or none are set, the v1 keyword rules decide, so
a ticket is never left unclassified. The response's `classified_by` field
says which one was used (`llm:<model>` or `rules`).

**Tracing.** Each request is one trace: a `triage` span, a span per graph
node, and an `llm.classify` span per model attempt. The LLM span is marked as
a Langfuse generation and carries the model, prompt, completion and token
usage (`gen_ai.*` attributes). Spans go to Langfuse's OpenTelemetry endpoint
(`/api/public/otel`) through the standard OTLP exporter, so the code depends
on OpenTelemetry rather than on the Langfuse SDK, and failed attempts show up
as error spans with the reason (for example `TimeoutError`).

### Running v2

```
pip install -r requirements.txt
cp .env.example .env    # add one provider key, and Langfuse keys if you want traces
set -a; source .env; set +a
uvicorn async_service:app --port 8002
```

```
curl -X POST http://127.0.0.1:8002/triage \
  -H "Content-Type: application/json" \
  -d '{"ticket_id": "T-201", "text": "I was charged twice, please refund me asap"}'

curl -X POST http://127.0.0.1:8002/triage/batch \
  -H "Content-Type: application/json" \
  -d '{"tickets": [{"ticket_id": "T-202", "text": "app crashes on login"},
                   {"ticket_id": "T-203", "text": "what are your opening hours"}]}'
```

Without `TRIAGE_MODELS` the service still runs offline on the keyword rules,
like v1.

### How v2 is tested

`test_async_service.py` has 14 tests that need no API key. The LLM path runs
through the real `litellm.acompletion` using LiteLLM's `mock_response`
option, and an in-memory OpenTelemetry exporter checks the spans. The tests
cover:

- the LLM's answer overriding the keyword rules
- fallback to the second model when the first errors
- fallback to rules on invalid JSON and on timeout
- the guard over HTTP
- the generation attributes Langfuse reads
- all spans of one request sharing a trace
- the batch endpoint running concurrently (5 slow calls finish in under 0.9 s instead of 1.5 s)
- the batch endpoint never exceeding its concurrency limit

I also ran the service against a local stand-in for the Langfuse endpoint to
check that spans arrive on `/api/public/otel/v1/traces` with the right Basic
auth header.

```
pytest -q    # 20 passed (6 v1 + 14 v2)
```
