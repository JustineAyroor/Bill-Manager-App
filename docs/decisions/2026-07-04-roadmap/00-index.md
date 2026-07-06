# Roadmap Discussion - July 4, 2026

This folder captures a planning discussion about the next phase of the Bill Manager App: deployment reliability, LLM bill-import accuracy, notification provider flexibility, and multi-plan schema support.

Each topic has its own file so it can later be lifted out and turned into a standalone write-up (blog post, internal doc, etc.) without dragging in the others.

## Topics

1. [Deployment & GCP Architecture](01-deployment-and-gcp-architecture.md) - moving off manual `tmux`, streamlining redeploys, and picking a GCP shape suited to a low-load personal project.
2. [Notifications Strategy](02-notifications-strategy.md) - what to do now that Twilio access was rejected, and how to keep the reminder system provider-agnostic.
3. [LLM Bill Import Accuracy](03-llm-bill-import-accuracy.md) - why allocations aren't always correct today, and how to build an evaluation loop to systematically improve accuracy.
4. [Multi-Plan Schema Scaling](04-multi-plan-schema.md) - how to support more than one bill/plan (with different members) without disrupting current production data.
5. [Deferred: Microservice / Next.js Migration](05-deferred-microservice-migration.md) - the longer-term architecture idea that is intentionally *not* being worked on yet, and why.
6. [Multi-Plan Authorization & Global Plan Selector](06-multi-plan-authorization-and-global-selector.md) - who can create/manage which plans, and the global "Active plan" dropdown that scopes the whole app to one plan at a time.
7. [Member Management Authorization](07-member-management-authorization.md) - only the app owner can remove a member from a plan; a plan-owning member can only edit members they personally added.
8. [LLM Bill Import v2: RAG Architecture](08-llm-bill-import-rag-architecture.md) - the opt-in, carrier-agnostic, cost-conscious retrieval-augmented pipeline built alongside (not replacing) the legacy synchronous import flow, including generalized member identifiers, an evaluation harness, per-job observability (system prompt, tokens, real $ cost, cache hits), an owner-only "Inspect a job" view, an owner-only cross-plan "AI Eval (admin)" dashboard, and three rounds of real-world-testing-driven fixes: a residual-line safety net, transparent unmatched/shared-charge diff accounting, deterministic per-member precedent retrieval with no future-leakage, a phone-digit extraction cross-check, a Gradio polling race fix (diff/review view flickering), current OpenRouter model-slug guidance, a precedent-based fallback suggestion for members with no identifier match on the current bill, and tightened prompt rules against matching a member purely because their historical amount "looks about right."
9. [Bill Import v2 Manual Test Plan](09-bill-import-v2-manual-test-plan.md) - a fillable checklist (steps + expected results + Pass/Fail/Notes fields to record directly in the doc) to re-validate the pipeline from a freshly-wiped job/eval-history state, covering the NORMAL and EVALUATE_ONLY flows, caching, rate limiting, identifier matching, precedent correctness, access control, and the eval harness/admin dashboard.

## Execution order agreed for this round

Order of work, as prioritized during this discussion (see individual todos in the active plan):

1. Discussion write-up (this folder) - **done**
2. Notifications: modularize the provider layer, default to Email, keep Twilio intact but isolated - **done, tested and confirmed by owner**
3. Multi-plan schema migration (incl. updating seed scripts) - **done, tested and verified against the dev database**
3b. Multi-plan authorization + global "Active plan" selector (incl. `Payment.plan_id`) - **done, tested and verified against the dev database**
3c. Member management authorization (delete restricted to OWNER, edit restricted to the member who created them) - **done, tested and verified against the dev database**
4. LLM bill-import accuracy: opt-in RAG pipeline v2, generalized member identifiers, evaluation harness, plus per-job observability + owner-only Inspector and Admin Eval Dashboard - **done, implemented and smoke-tested end-to-end against the dev database with real OpenRouter calls; see [08-llm-bill-import-rag-architecture.md](08-llm-bill-import-rag-architecture.md)**
5. Deployment hardening (systemd, Tailscale, CI/CD) - up next

Note this is a deliberate reprioritization of the original recommendation (which suggested deployment first as a safety foundation). The owner chose to tackle functional/product work first and defer infra hardening to last since the app already runs today, just requires manual restarts.
