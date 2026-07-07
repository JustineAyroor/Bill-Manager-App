"""
Read-only data access for the admin-only "AI Eval (admin)" dashboard.

Two independent data sources, deliberately not joined at the DB layer:
  1. list_all_jobs() - cross-plan BillImportJob rows (every v2 job ever run
     in production, whatever model was configured at the time).
  2. load_eval_history()/aggregate_by_model() - eval/history.jsonl, written
     by eval/run_eval.py (a CLI-only tool in this version - no in-UI
     trigger) when explicitly comparing models against replayed jobs.

Nothing here triggers an LLM/embedding call - pure aggregation of
already-persisted data.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import BillImportJob, Plan

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "history.jsonl")


def list_all_jobs(limit: int = 200) -> list[dict[str, Any]]:
    """Cross-plan BillImportJob rows, most recent first, joined with plan name."""
    with SessionLocal() as db:
        rows = db.execute(
            select(BillImportJob, Plan.name)
            .join(Plan, Plan.id == BillImportJob.plan_id)
            .order_by(BillImportJob.created_at.desc())
            .limit(limit)
        ).all()

        out: list[dict[str, Any]] = []
        for job, plan_name in rows:
            usage = json.loads(job.token_usage_json) if job.token_usage_json else {}
            chat_cost = usage.get("chat_cost_usd")
            emb_cost = usage.get("embedding_cost_usd")
            total_cost = None
            if chat_cost is not None or emb_cost is not None:
                total_cost = (chat_cost or 0.0) + (emb_cost or 0.0)
            out.append(
                {
                    "id": job.id,
                    "plan_id": job.plan_id,
                    "plan_name": plan_name,
                    "status": job.status,
                    "mode": job.mode or "",
                    "invoice_id": job.invoice_id,
                    "cache_hit_count": job.cache_hit_count or 0,
                    "chat_prompt_tokens": usage.get("chat_prompt_tokens"),
                    "chat_completion_tokens": usage.get("chat_completion_tokens"),
                    "embedding_tokens": usage.get("embedding_tokens"),
                    "total_cost_usd": total_cost,
                    "error": (job.error or "")[:200],
                    "created_at": job.created_at,
                    "completed_at": job.completed_at,
                }
            )
        return out


def load_eval_history(history_path: str = HISTORY_PATH) -> list[dict[str, Any]]:
    """Raw eval/history.jsonl records, one per (job, model) scoring run."""
    records: list[dict[str, Any]] = []
    if not os.path.exists(history_path):
        return records
    with open(history_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def aggregate_by_model(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One row per model: mean per-member $ error (both the plain and
    "active-members-only" variants - see mean_abs_diff_active()'s docstring
    for why the plain one alone is misleading), JSON-parse success rate,
    and month/year match rate, across every scored job for that model.
    """
    by_model: dict[str, list[dict[str, Any]]] = {}
    for rec in history:
        by_model.setdefault(str(rec.get("model") or "unknown"), []).append(rec)

    out: list[dict[str, Any]] = []
    for model, recs in sorted(by_model.items()):
        n = len(recs)
        parsed = [r for r in recs if r.get("json_parse_success")]
        n_parsed = len(parsed)
        mean_abs_diffs = [r["mean_abs_per_member_diff"] for r in parsed if r.get("mean_abs_per_member_diff") is not None]
        # Older rows (written before this field existed) fall back to the
        # plain mean rather than being dropped from the average entirely.
        mean_abs_diffs_active = [
            r["mean_abs_diff_active_members"] if r.get("mean_abs_diff_active_members") is not None else r.get("mean_abs_per_member_diff")
            for r in parsed
            if r.get("mean_abs_diff_active_members") is not None or r.get("mean_abs_per_member_diff") is not None
        ]
        month_matches = [r for r in parsed if r.get("month_year_match")]
        total_pct_diffs = [r["total_amount_pct_diff"] for r in parsed if r.get("total_amount_pct_diff") is not None]

        out.append(
            {
                "model": model,
                "n_scored": n,
                "json_parse_success_rate": round(n_parsed / n * 100, 1) if n else 0.0,
                "month_year_match_rate": round(len(month_matches) / n_parsed * 100, 1) if n_parsed else None,
                "mean_abs_per_member_diff_usd": round(sum(mean_abs_diffs) / len(mean_abs_diffs), 2) if mean_abs_diffs else None,
                "mean_abs_diff_active_members_usd": round(sum(mean_abs_diffs_active) / len(mean_abs_diffs_active), 2)
                if mean_abs_diffs_active
                else None,
                "mean_total_pct_diff": round(sum(total_pct_diffs) / len(total_pct_diffs), 2) if total_pct_diffs else None,
            }
        )
    return out
