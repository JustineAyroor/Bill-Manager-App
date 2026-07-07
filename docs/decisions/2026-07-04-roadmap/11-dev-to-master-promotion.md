# Promoting `dev` to `master`: Multi-Plan + RAG v2 Go Live

## Context

Since [Deployment Implementation Notes](10-deployment-implementation.md), production (`master`) had deliberately stayed on the pre-multi-plan-schema codebase while all the multi-plan support, RAG v2 bill import pipeline, member identifiers, and notification-provider work landed on `dev`. This document covers the one-time, deliberate promotion of `dev` to `master`/production, executed 2026-07-07.

`master` was a strict git ancestor of `dev` (`git merge-base --is-ancestor master dev`) - no divergence, so this was always going to be a clean merge. The real risk wasn't git conflicts; it was (a) running 6 new, backfilling Alembic migrations against real production data for the first time, and (b) syncing a much heavier dependency set (`chromadb`, `langchain-*`, `onnxruntime`, `grpcio`, etc.) onto an `e2-micro` VM that was already tight on both disk and memory.

## What changed

- 6 new Alembic migrations beyond `master`'s `n4`: `n5` (plans/plan_members + `invoices.plan_id`), `n6` (`payments.plan_id`), `n7` (`members.created_by_member_id`), `n8` (`bill_import_jobs`), `n9` (`member_identifiers`), `n10` (observability columns on `bill_import_jobs`). All are additive/idempotent (`if <column> in existing_columns: return`) and were individually designed with a documented backfill strategy - see [04-multi-plan-schema.md](04-multi-plan-schema.md) and [08-llm-bill-import-rag-architecture.md](08-llm-bill-import-rag-architecture.md).
- 5 new dependencies for the RAG v2 pipeline: `langchain-core`, `langchain-text-splitters`, `langchain-chroma`, `langchain-openai`, `chromadb`. `chromadb` pulls in a heavy transitive tree (`onnxruntime`, `grpcio`, `kubernetes`, `opentelemetry-*`, `tokenizers`, `uvicorn[standard]`) even though the app never uses Chroma's own bundled embedder (see the comment at the top of [`app/services/vectorstore.py`](../../../app/services/vectorstore.py) - embeddings always go through OpenRouter instead, specifically to avoid pulling a local `onnxruntime`-based model onto this memory-constrained VM).
- No new required environment variables - `OPENROUTER_API_KEY` was already configured on the VM (used by the pre-existing single-shot import). New optional config (`OPENROUTER_EMBEDDING_MODEL`, `VECTOR_RETRIEVAL_LOOKBACK_MONTHS`, `BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN`, `BILL_TEXT_RETENTION_DAYS`) all ship with safe defaults.
- The deployment tooling from the previous round (`deploy/`, `.github/workflows/deploy.yml`, `DEPLOYMENT.md`) had been sitting **uncommitted** on `dev`'s working tree since it was built directly on top of that branch - it needed to be committed before it could flow into `master` at all.

## Pre-flight: VM disk and memory headroom

Before touching any code, `df -h /` on the VM showed only **1.7G available out of 8.6G (81% used)**. Breakdown: `.venv` 430M, `~/.cache/uv` 382M, `/var/cache/apt` 137M, plus 3 unused old kernel packages (`6.17.0-1012/1013/1020-gcp`; only `1018` was the running kernel). Given `onnxruntime` alone is typically 100-200MB, this needed headroom freed first:

- `sudo apt-get purge` the 2 kernel versions that were neither running nor the metapackage's "latest" target (`1012`, `1013`), followed by `sudo apt-get autoremove` and `sudo update-grub`. Freed a modest ~100MB - deliberately conservative, kept the running kernel (`1018`) and the metapackage's latest (`1020`) untouched to avoid any boot risk.
- Force-cleared `~/.cache/uv` (`uv cache clean --force`, since the always-running systemd service holds a lock on the ordinary cache-clean path) - freed ~345MB. This is pure cache, safe to fully clear since it regenerates on the next `uv sync`.
- Net result: ~1.8G available before the sync. The actual `uv sync --frozen` for the new dependency set only added a **net ~300MB** to disk (it downloaded and installed 50 packages in about 32 seconds - the VM's 2 vCPUs and network were never the bottleneck here), landing at **1.5G available (83% used)** post-deploy. Tight, but not critical - worth watching as `data/vectorstore/` grows and revisiting a live disk resize (`gcloud compute disks resize` + `resize2fs`, no reboot required) if it gets tighter.
- RAM: idle available dropped from ~277Mi to ~217Mi after the deploy, and the app's own steady-state memory climbed from ~270-346M to ~335M (the new `langchain`/`chromadb` import graph). Not currently a problem, but the ceiling is closer than before.

## Validating the migration against real production data

Rather than trust the migrations blind (even though they were already tested once during original `dev` development against an older snapshot), the actual **live** `tmobile.db` was copied off the VM and the full migration was dry-run locally before touching production:

1. `gcloud compute scp` the live `tmobile.db` to a local scratch path.
2. Swapped it in place of the local dev database temporarily, ran `uv run alembic upgrade head` against it, and restored the original local dev database immediately after (the local dev DB was never at risk).
3. Verified byte-for-byte: row counts for `members` (8), `invoices` (29), `allocations` (166), `payments` (74), `reminder_logs` (3), `users` (4), `payment_applications` (136) were **identical** before and after; a single "Default Plan" was created with `owner_member_id` correctly resolved to "Justine"; all 8 members linked via `plan_members`; every invoice and payment given a non-null `plan_id`; and the financial sums (`SUM(allocations.amount_due)` = $8,816.96, `SUM(payments.amount)` = $14,519.62, `SUM(invoices.total_amount)` = $8,679.62) matched exactly before and after.

This dry run gave high confidence before running the same migration for real.

## Execution

1. Committed the pending deploy-tooling changes to `dev` (commit `7ac00a2`).
2. Pushed `dev` to `origin`.
3. Tagged the pre-promotion `master` tip as `pre-dev-promotion` - a one-command rollback anchor (`git checkout pre-dev-promotion`).
4. Merged `dev` into `master` (`--no-ff`, commit `77d7e28`) and pushed. Note: pushing changes to `.github/workflows/*.yml` requires the `workflow` OAuth scope, which the automated tooling's token didn't have - this specific push had to be done by the owner directly from their own terminal.
5. Pushing to `master` auto-triggers the GitHub Actions deploy workflow. For this *first* heavy promotion specifically, an extra manual GCE boot-disk snapshot was taken immediately beforehand (on top of the automatic pre-migration SQLite backup `deploy/backup_db.sh` always takes), and `deploy/deploy.sh` was run **manually over SSH** while watching output live, rather than trusting the unattended CI run blind for this one.
6. Bootstrapping note: since `deploy/deploy.sh` itself only became tracked in git as part of this very merge, the VM had a stale untracked copy of `deploy/deploy.sh`/`deploy/backup_db.sh` left over from when they were first hand-installed (content was verified byte-identical to what git was about to bring in, then removed to avoid a fast-forward conflict) - the very first pull had to be done manually (`git fetch && git checkout master && git merge --ff-only origin/master`) before `deploy.sh` existed to run itself. Every subsequent deploy won't hit this bootstrap step.
7. `deploy.sh` ran cleanly: pre-deploy backup -> fast-forward pull (no-op, already pulled in step 6) -> `uv sync --frozen` (50 packages, ~32s) -> all 6 migrations (`n4` -> `n10`) -> `systemctl restart` -> health check. The health check needed **22 retries (~2 minutes)** before the app answered `HTTP 200` - longer than the pre-RAG-v2 baseline (which sometimes cleared in under a minute), consistent with the heavier `langchain`/`chromadb` import graph on this CPU-throttled VM. Still comfortably inside the existing 5-minute budget (`HEALTH_RETRIES=60`, 5s apart).

## Post-deploy verification

- `systemctl status`/`journalctl` clean, no tracebacks.
- Public URL (`http://34.42.180.111:7860`) serving the new UI: Plans tab, Bill Import v2 flow visible by default (legacy flow collapsed), Admin Eval Dashboard markdown all present in `/config`.
- Queried the live production `tmobile.db` directly on the VM post-deploy: `alembic_version = n10`, all row counts and financial sums **identical** to the pre-migration dry run above - zero data loss or corruption.
- Ran `uv run python scripts/rebuild_vectorstore.py` once (owner's choice) to backfill historical precedent embeddings from all 29 already-approved invoices, so RAG v2 has context from day one instead of starting cold. Took about 2 minutes (mostly the same cold `chromadb`/`langchain` import cost as the app itself), produced a 2.3MB `data/vectorstore/` - negligible disk impact. This is purely an embedding-cost operation (no chat-completion calls) and is safe to re-run any time, since it's a deterministic function of already-approved data (see the script's own docstring).

## Rollback plan (documented, not needed)

- App-only issue: `git checkout pre-dev-promotion`, `uv sync --frozen`, `sudo systemctl restart tmobile-bill-manager`.
- Data issue: stop the service, restore `tmobile.db` from the pre-migration `deploy/backup_db.sh` backup or the extra manual GCE snapshot taken immediately before this deploy, then roll back the app as above.

## Status

**Done - executed and verified against the real production VM, 2026-07-07.** Production is now running the same codebase as `dev` (multi-plan support, RAG v2 bill import, member identifiers, notification-provider abstraction). The `dev`/`master` split continues going forward for future rounds of work, promoted the same deliberate way.
