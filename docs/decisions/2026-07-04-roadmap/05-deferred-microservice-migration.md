# Deferred: Microservice / Next.js Migration

## Context

The current app is a Python monolith: Gradio UI, service-layer business logic, SQLAlchemy models, all in one process. The owner finds Gradio limiting for UI control and personalization, and the long-term direction under consideration is:

- **Fastify** APIs as the backend, providing clear seams for rate limiting/throttling on LLM-calling endpoints and tracking total cost/token usage
- A **Next.js** frontend that talks to those APIs and handles routing/UI

## Decision: explicitly deferred

This is **not** being worked on in this round. The owner's stated priority is to get the core functionality - especially bill-import accuracy - "spot on" before investing in a UI framework migration. Rebuilding the UI layer before the underlying extraction/allocation logic is trustworthy would mean re-doing UI work later anyway once the business logic changes shape (e.g. once multi-plan scoping lands).

## Why it's still worth writing down now

Two of the changes already planned in this round make a future migration meaningfully easier if/when it happens:

- **Notification provider abstraction** ([02-notifications-strategy.md](02-notifications-strategy.md)) creates a clean service-layer seam that a Fastify API could wrap directly, instead of being tangled into Gradio callback functions.
- **Multi-plan schema** ([04-multi-plan-schema.md](04-multi-plan-schema.md)) defines the domain model (`Plan`, `Member`, `Invoice`, `Allocation`) that a future API layer would expose - getting this right now avoids re-designing the API contract later.

The LLM bill-import pipeline is also a natural candidate for rate limiting, cost tracking, and token accounting once it sits behind a real API layer - this is explicitly called out as a driver for the eventual Fastify move, and the [evaluation harness work](03-llm-bill-import-accuracy.md) will produce accuracy/cost data that's useful input to that later design regardless of which UI framework sits in front of it.

## Status

Tracked as a future direction only. Revisit once:

1. Bill-import accuracy is validated via the evaluation harness, and
2. The multi-plan schema and notification abstraction have been in production use for a while.

No code changes are planned for this item in the current round.
