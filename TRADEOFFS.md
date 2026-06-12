# Tradeoffs & Scale Assumptions

## What I built (and deliberately didn't)

Built: an end-to-end campaign **agent** where the user just gives one prompt and the agent handles segmentation, campaign planning, explains its decisions, launches the campaign, monitors delivery in real time, adapts if needed, and generates the final report. The marketer only needs to approve the important steps.

Didn't build: authentication, multi-tenancy, campaign scheduling, customer management screens, or CSV import UI. Those are important CRM features, but they weren't the focus of this assignment. I wanted to spend my time on the AI workflow instead of standard CRUD features.

## Async send fan-out: FastAPI BackgroundTasks/asyncio vs a queue

Right now message sending uses FastAPI BackgroundTasks with asyncio. This works well for a few hundred messages per campaign and keeps the architecture simple.

If this needed to support much higher traffic, I'd switch to something like Celery or RQ with Redis. That would give proper retries, durable jobs, and prevent messages from getting lost if the server crashes.

## Live updates: SSE + in-memory pub/sub vs Redis/WebSockets

The live campaign tracker uses Server-Sent Events (SSE) with an in-memory subscriber registry. This assumes there's only one CRM server running.

If the application was deployed across multiple instances, I'd replace the in-memory registry with Redis pub/sub (or another message broker) so updates can reach clients connected to different servers. I'd also consider WebSockets if two-way communication becomes necessary.

## Segmentation: structured filters compiled to SQL vs raw LLM SQL

The LLM only decides **what filters** should be applied. My backend converts those structured filters into parameterized SQL queries.

I intentionally avoided letting the LLM generate raw SQL because it can produce invalid queries and introduces unnecessary security risks. This way the AI makes the decisions while the backend handles query generation safely. I also show the generated SQL for transparency.

## Channel choice: grounded in deterministic projections

Instead of letting the LLM randomly pick a communication channel, it calls a `compare_channels` tool which returns projected metrics based on stored historical data.

The model has to make its decision using those projections, so the recommendation always matches the actual numbers shown in the UI.

## Delivery loop: idempotency + ordering

Delivery callbacks can arrive multiple times or even out of order. For example, an "opened" event might arrive before "delivered".

To handle this, I made the callback handler idempotent using a `UNIQUE(message_id, event_type)` constraint so duplicate events are ignored. I also use a forward-only state machine so message status can only move forward and never go backwards because of delayed callbacks.

The `message_events` table stores every event as an append-only log, while `messages.status` always stores the latest derived status.

## LLM on a free tier: call-frugal design + graceful degradation

The project was built using Gemini's free tier, so API limits had to be considered from the beginning.

To reduce model usage, campaign planning happens in a single conversation, projections are fully deterministic, mid-campaign analysis only happens at specific milestones, and drill-down explanations are generated only when requested and then cached.

If the model hits a rate limit, the UI falls back to predefined template responses instead of breaking.

For production, I'd move to a paid API plan and use context caching to avoid repeatedly sending the same system prompt.

## Attribution

Campaign attribution is simulated by having the communication channel send a `converted` callback for a percentage of users who clicked the message. That callback creates an order linked to the corresponding `campaign_id`.

This allows the final report to show attributed revenue instead of only engagement metrics like opens and clicks.

## Other

* Drill-down explanations are currently cached in memory (I'd persist them in production).
* The backend is organized as two independently deployable services (CRM and stubbed communication service) connected through HTTP callbacks, since that separation is one of the main system design ideas.
* Current scope is designed for hundreds of messages per campaign. For larger workloads I'd introduce queues, Redis-based event distribution, and horizontal scaling.
