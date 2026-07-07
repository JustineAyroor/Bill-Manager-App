"""
Evaluation harness for the Bill Import v2 (RAG, opt-in) pipeline.

Iterates BillImportJob rows with status == DONE - both NORMAL-approved jobs
(linked back to the Invoice they produced when approved, see
app/ui/bill_import.py's _v2_approve) and EVALUATE_ONLY jobs (linked to the
pre-existing Invoice they were compared against) qualify, since both have a
comparable ground truth (an approved Invoice/Allocation).

Cost stays flat as history grows: preprocessing (extract/clean/chunk/embed)
is never repeated here - each job's already-selected top-N chunks
(selected_chunks_json) and already-retrieved precedent facts
(precedent_used_json) are reused as-is. Only the LLM chat-completion call
itself is re-run, once per (job, model) pair not already scored (skipped
unless --force), and results are appended to eval/history.jsonl.

Usage:
  uv run python eval/run_eval.py --models openai/gpt-4o-mini,anthropic/claude-sonnet-4.6 --limit 20
  uv run python eval/run_eval.py --models openai/gpt-4o-mini --force

Model slugs must be valid, currently-routable OpenRouter model ids - these
get retired/renamed over time (e.g. "anthropic/claude-3.5-sonnet" 404s as of
mid-2026), so if a model errors with "No endpoints found", check the live
catalog first:
  curl -s https://openrouter.ai/api/v1/models | python -c \\
    "import json,sys; print('\\n'.join(m['id'] for m in json.load(sys.stdin)['data'] if 'claude' in m['id']))"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Allow running as `uv run python eval/run_eval.py` from the project root
# without also having to set PYTHONPATH=. - eval/ isn't a package, so `app`
# wouldn't otherwise be importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.core.config import OPENROUTER_MODEL, VECTOR_RETRIEVAL_LOOKBACK_MONTHS
from app.db.database import SessionLocal
from app.db.models import BillImportJob, Invoice
from app.services import member_identifiers, plans as plans_service, vectorstore
from app.services.allocation_view import build_member_allocation_view, mean_abs_diff_active
from app.services.llm_invoice_extract_v2 import extract_bill_proposal_v2

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "history.jsonl")


def _load_scored_pairs(history_path: str) -> set[tuple[int, str]]:
    scored: set[tuple[int, str]] = set()
    if not os.path.exists(history_path):
        return scored
    with open(history_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                scored.add((int(rec["job_id"]), str(rec["model"])))
            except Exception:
                continue
    return scored


def _score_job(job: BillImportJob, model: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job.id,
        "plan_id": job.plan_id,
        "model": model,
        "invoice_id": job.invoice_id,
    }

    if not job.invoice_id:
        record.update({"json_parse_success": None, "error": "No linked invoice - no ground truth to score against."})
        return record

    with SessionLocal() as db:
        invoice = db.get(Invoice, job.invoice_id)
        if not invoice:
            record.update({"json_parse_success": None, "error": f"Invoice {job.invoice_id} not found."})
            return record

        plan = plans_service.get_plan(db, job.plan_id)
        carrier_type = plan.carrier_type if plan else None
        roster = member_identifiers.build_known_roster(db, job.plan_id)

        selected_chunks = json.loads(job.selected_chunks_json) if job.selected_chunks_json else [job.cleaned_text[:12000]]
        precedent_facts = json.loads(job.precedent_used_json) if job.precedent_used_json else []

        actual_by_member = {a.member_id: a.amount_due for a in invoice.allocations}
        actual_total = invoice.total_amount
        actual_year, actual_month = invoice.year, invoice.month

        try:
            proposal = extract_bill_proposal_v2(selected_chunks, precedent_facts, roster, carrier_type, model=model)
        except Exception as e:
            record.update({"json_parse_success": False, "error": str(e)[:500]})
            return record

        # Score against the SAME allocation model the app would actually use
        # if this were approved as proposed - build_member_allocation_view()
        # applies deterministic matching, dedupes colliding LLM guesses, and
        # splits shared/unmatched money equally across whichever members
        # actually matched a line on THIS bill (owner absorbs the rest) -
        # see round 4/5 of the architecture doc for why this replaced a
        # separate ad hoc scoring formula here.
        try:
            bill_period_key = vectorstore.period_key(proposal.year, proposal.month)
        except Exception:
            bill_period_key = None

        view = build_member_allocation_view(
            db, job.plan_id, roster, proposal.lines,
            bill_period_key=bill_period_key, lookback_months=VECTOR_RETRIEVAL_LOOKBACK_MONTHS,
        )

        per_member = []
        for m in view.members:
            actual = actual_by_member.get(m.member_id, 0.0)
            per_member.append(
                {"member_id": m.member_id, "actual": round(actual, 2), "proposed": m.amount, "diff": round(m.amount - actual, 2), "basis": m.basis}
            )

        total_diff = round(proposal.total_amount - actual_total, 2)
        # Kept for backward compatibility with older history.jsonl rows/
        # dashboards, but diluted by roster members with no stake in this
        # bill (correctly $0/$0) - mean_abs_diff_active_members below is the
        # more meaningful number; see its docstring.
        mean_abs_per_member_diff = round(sum(abs(p["diff"]) for p in per_member) / len(per_member), 2) if per_member else 0.0

        record.update(
            {
                "json_parse_success": True,
                "error": None,
                "month_year_match": (proposal.year == actual_year and proposal.month == actual_month),
                "actual_total": actual_total,
                "proposed_total": proposal.total_amount,
                "total_amount_diff": total_diff,
                "total_amount_pct_diff": round(abs(total_diff) / actual_total * 100, 2) if actual_total else None,
                "per_member": per_member,
                "mean_abs_per_member_diff": mean_abs_per_member_diff,
                "mean_abs_diff_active_members": mean_abs_diff_active(per_member),
                "unmatched_lines": view.unmatched_lines,
                "unmatched_total": view.unmatched_total,
                "none_total": view.none_total,
                "unattributed_total": view.unattributed_total,
            }
        )
        return record


def run(models: list[str], limit: int | None, force: bool, history_path: str = HISTORY_PATH) -> int:
    scored_pairs = set() if force else _load_scored_pairs(history_path)

    with SessionLocal() as db:
        stmt = select(BillImportJob).where(BillImportJob.status == "DONE").order_by(BillImportJob.created_at.asc())
        if limit:
            stmt = stmt.limit(int(limit))
        jobs = db.execute(stmt).scalars().all()
        job_ids = [j.id for j in jobs]

    n_written = 0
    with open(history_path, "a") as f:
        for job_id in job_ids:
            with SessionLocal() as db:
                job = db.get(BillImportJob, int(job_id))
                if not job:
                    continue
                for model in models:
                    if (job.id, model) in scored_pairs:
                        continue
                    record = _score_job(job, model)
                    f.write(json.dumps(record) + "\n")
                    f.flush()
                    n_written += 1
                    print(f"scored job={job.id} model={model} -> json_parse_success={record.get('json_parse_success')}", file=sys.stderr)

    return n_written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", type=str, default=OPENROUTER_MODEL, help="Comma-separated OpenRouter model ids to compare")
    parser.add_argument("--limit", type=int, default=None, help="Only evaluate the oldest N DONE jobs")
    parser.add_argument("--force", action="store_true", help="Re-score (job, model) pairs even if already in history.jsonl")
    args = parser.parse_args()

    models_list = [m.strip() for m in args.models.split(",") if m.strip()]
    n = run(models_list, args.limit, args.force)
    print(f"Wrote {n} new score(s) to {HISTORY_PATH}", file=sys.stderr)
