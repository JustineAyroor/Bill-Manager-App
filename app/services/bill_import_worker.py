"""
In-process background worker for the Bill Import v2 (RAG, opt-in) pipeline.

A lightweight SQLite-backed job table (BillImportJob) + a single daemon
thread (no Redis/Celery) - processes one job at a time, which is a
deliberate feature given the target e2-micro VM's resources. Applies only
to the new opt-in v2 path; the legacy synchronous pipeline
(app/ui/bill_import.py's existing flow) is completely unaffected.

Flow per job: extract+clean text (done synchronously in enqueue_job, since
it's free/instant) -> content-hash cache check -> rate limit check -> chunk
+ select relevant chunks -> retrieve precedent + known roster -> carrier
prompt -> single LLM call (retried on transient failure) -> parse -> detect
NORMAL vs EVALUATE_ONLY mode (existing Invoice for plan/year/month?) -> if
EVALUATE_ONLY, diff proposal vs actual and store the comparison, never
touching the ledger -> DONE/FAILED.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import func, select

from app.core.config import BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN, OPENROUTER_MODEL, VECTOR_RETRIEVAL_LOOKBACK_MONTHS
from app.db.database import SessionLocal
from app.db.models import Allocation, BillImportJob, Invoice
from app.services import bill_preprocess, member_identifiers, plans as plans_service, vectorstore
from app.services.allocation_view import build_member_allocation_view, mean_abs_diff_active
from app.services.llm_invoice_extract_v2 import extract_bill_proposal_v2
from app.services.pdf_extract import extract_pdf_text

_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "history.jsonl")

DEFAULT_SELECTION_QUERIES = [
    "total amount due and per-line bill summary",
    "per-member or per-line charges, phone numbers, emails, or names and their amounts",
]

MAX_LLM_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0

_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def enqueue_job(plan_id: int, member_id: Optional[int], pdf_path: str) -> dict[str, Any]:
    """
    Extract + clean text immediately (free/instant), then either short-
    circuit to a cached/in-flight job or insert a new PENDING row.
    Returns {"status": "cached"|"queued"|"rate_limited"|"error", "job_id", "message"}.
    """
    try:
        raw_text = extract_pdf_text(pdf_path)
    except Exception as e:
        return {"status": "error", "job_id": None, "message": f"Failed to extract PDF text: {e}"}

    cleaned = bill_preprocess.clean_text(raw_text)
    if not cleaned or len(cleaned) < 50:
        return {"status": "error", "job_id": None, "message": "Extracted text too short; bill might be scanned/unreadable."}

    chash = bill_preprocess.content_hash(cleaned)

    with SessionLocal() as db:
        existing = db.execute(
            select(BillImportJob).where(BillImportJob.plan_id == plan_id, BillImportJob.content_hash == chash)
        ).scalars().first()

        if existing:
            if existing.status == "DONE":
                existing.cache_hit_count = (existing.cache_hit_count or 0) + 1
                db.commit()
                return {
                    "status": "cached",
                    "job_id": existing.id,
                    "message": "Identical bill already processed for this plan - returning the cached result, zero new LLM/embedding calls.",
                }
            return {
                "status": "queued",
                "job_id": existing.id,
                "message": f"An identical upload already exists (status={existing.status}) - not creating a duplicate job.",
            }

        if not _under_rate_limit(db, plan_id):
            return {
                "status": "rate_limited",
                "job_id": None,
                "message": (
                    f"Rate limit reached ({BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN} new jobs/hour for this plan). "
                    "Try again later, or raise BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN for a bulk backfill session."
                ),
            }

        job = BillImportJob(
            plan_id=plan_id,
            uploaded_by_member_id=member_id,
            content_hash=chash,
            cleaned_text=cleaned,
            status="PENDING",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    return {"status": "queued", "job_id": job_id, "message": "Job queued for AI extraction."}


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with SessionLocal() as db:
        job = db.get(BillImportJob, int(job_id))
        if not job:
            return None
        return _job_to_dict(job)


def list_recent_jobs(plan_id: int, limit: int = 10) -> list[dict[str, Any]]:
    if not plan_id:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            select(BillImportJob)
            .where(BillImportJob.plan_id == int(plan_id))
            .order_by(BillImportJob.created_at.desc())
            .limit(limit)
        ).scalars().all()
        return [_job_to_dict(r) for r in rows]


def start_worker_thread() -> None:
    """Idempotent - safe to call more than once (e.g. if build_app() runs twice)."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="bill-import-worker", daemon=True)
    _worker_thread.start()


# -------------------------------------------------------------------------
# Internals
# -------------------------------------------------------------------------

def _job_to_dict(job: BillImportJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "plan_id": job.plan_id,
        "uploaded_by_member_id": job.uploaded_by_member_id,
        "status": job.status,
        "mode": job.mode,
        "error": job.error,
        "invoice_id": job.invoice_id,
        "content_hash": job.content_hash,
        "cleaned_text": job.cleaned_text,
        "selected_chunks": json.loads(job.selected_chunks_json) if job.selected_chunks_json else [],
        "precedent_used": json.loads(job.precedent_used_json) if job.precedent_used_json else [],
        "known_roster": json.loads(job.known_roster_json) if job.known_roster_json else [],
        "system_prompt": job.system_prompt,
        "llm_raw_response": job.llm_raw_response,
        "token_usage": json.loads(job.token_usage_json) if job.token_usage_json else {},
        "cache_hit_count": job.cache_hit_count or 0,
        "proposal": json.loads(job.proposal_json) if job.proposal_json else None,
        "diff": json.loads(job.diff_json) if job.diff_json else None,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


def _under_rate_limit(db, plan_id: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = db.execute(
        select(func.count(BillImportJob.id)).where(
            BillImportJob.plan_id == plan_id,
            BillImportJob.created_at >= cutoff,
        )
    ).scalar() or 0
    return count < BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN


def _reset_stale_processing_jobs() -> None:
    """On worker startup, a job stuck in PROCESSING (e.g. a VM restart mid-job) is reset to PENDING."""
    with SessionLocal() as db:
        stale = db.execute(select(BillImportJob).where(BillImportJob.status == "PROCESSING")).scalars().all()
        for job in stale:
            job.status = "PENDING"
        if stale:
            db.commit()


def _worker_loop(poll_interval: float = 3.0) -> None:
    _reset_stale_processing_jobs()
    while not _worker_stop.is_set():
        job_id = None
        with SessionLocal() as db:
            job = db.execute(
                select(BillImportJob).where(BillImportJob.status == "PENDING").order_by(BillImportJob.created_at.asc())
            ).scalars().first()
            if job:
                job_id = job.id

        if job_id:
            _process_job(job_id)
        else:
            _worker_stop.wait(poll_interval)


def _process_job(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(BillImportJob, int(job_id))
        if not job or job.status != "PENDING":
            return
        job.status = "PROCESSING"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

    try:
        _run_job(job_id)
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(BillImportJob, int(job_id))
            if job:
                job.status = "FAILED"
                job.error = str(e)[:2000]
                job.completed_at = datetime.now(timezone.utc)
                db.commit()


def _extract_with_retry(selected_chunks, precedent_facts, known_roster, carrier_type):
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_ATTEMPTS):
        try:
            return extract_bill_proposal_v2(selected_chunks, precedent_facts, known_roster, carrier_type)
        except Exception as e:
            last_exc = e
            if attempt < MAX_LLM_ATTEMPTS - 1:
                time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise last_exc


def _diff_against_invoice(
    db, plan_id: int, roster: list[dict[str, Any]], invoice: Invoice, proposal, bill_period_key: int | None
) -> dict[str, Any]:
    """
    Evaluate-only mode: diff the proposal against an already-approved
    Invoice/Allocation - never writes anything, purely a scoring/comparison
    view. Uses the same build_member_allocation_view() the NORMAL-mode
    review UI and the eval CLI use, so this comparison reflects what would
    actually land in the ledger if this were approved as proposed (shared/
    unmatched money is split equally across whoever matched a line on this
    bill, owner absorbs the rest, same as _v2_approve). See round 4/5 of
    the architecture doc.
    """
    actual_by_member: dict[int, float] = {a.member_id: a.amount_due for a in invoice.allocations}

    view = build_member_allocation_view(
        db, plan_id, roster, proposal.lines, bill_period_key=bill_period_key, lookback_months=VECTOR_RETRIEVAL_LOOKBACK_MONTHS
    )

    per_member = []
    for m in view.members:
        actual = actual_by_member.get(m.member_id, 0.0)
        per_member.append(
            {
                "member_id": m.member_id,
                "actual_amount": round(actual, 2),
                "proposed_amount": m.amount,
                "diff": round(m.amount - actual, 2),
                "basis": m.basis,
                "detail": m.detail,
            }
        )

    return {
        "actual_total": invoice.total_amount,
        "proposed_total": proposal.total_amount,
        "total_diff": round(proposal.total_amount - invoice.total_amount, 2),
        "per_member": per_member,
        "unmatched_lines": view.unmatched_lines,
        "unmatched_total": view.unmatched_total,
        "none_total": view.none_total,
        "owner_member_id": view.owner_member_id,
        "unattributed_total": view.unattributed_total,
    }


def get_live_diff_for_approved_job(db, job_id: int) -> dict[str, Any] | None:
    """
    Once a job has an invoice attached (job.invoice_id set) - whether it
    started life as EVALUATE_ONLY, or was NORMAL and got approved since -
    its review screen must never stay editable/re-approvable: any further
    correction has to go through Payments -> Invoices, same rule for both.
    This recomputes the same "predicted (frozen at extraction time) vs.
    actual" comparison _diff_against_invoice() gives evaluate-only jobs,
    but against the invoice's *current* allocations - so if you edit the
    invoice later from the ledger, re-opening this job's read-only view
    reflects that edit instead of a stale approval-time snapshot. Returns
    None if the job has no invoice yet (still genuinely editable) or is
    missing the frozen proposal needed to recompute this.
    """
    job = db.get(BillImportJob, int(job_id))
    if not job or not job.invoice_id or not job.proposal_json:
        return None
    invoice = db.get(Invoice, int(job.invoice_id))
    if not invoice:
        return None
    try:
        proposal_dict = json.loads(job.proposal_json)
    except Exception:
        return None
    roster = json.loads(job.known_roster_json) if job.known_roster_json else member_identifiers.build_known_roster(db, job.plan_id)
    try:
        bill_period_key = vectorstore.period_key(int(proposal_dict.get("year")), str(proposal_dict.get("month")))
    except Exception:
        bill_period_key = None
    proposal_ns = SimpleNamespace(lines=proposal_dict.get("lines") or [], total_amount=float(proposal_dict.get("total_amount") or 0.0))
    return _diff_against_invoice(db, job.plan_id, roster, invoice, proposal_ns, bill_period_key)


def record_approval_accuracy(db, job_id: int, plan_id: int, approved_by_member: dict[int, float]) -> None:
    """
    Permanently records "what the model predicted vs. what actually got
    approved" the moment a NORMAL-mode job is approved - independent of
    whether the amounts were accepted as-is or hand-corrected first, and
    never invalidated by later edits made elsewhere (e.g. the Invoices
    ledger UI).

    Why this exists (not just "call eval/run_eval.py later"): that CLI tool
    re-runs the LLM fresh against the job's cached inputs to compare
    *different* models against each other - a deliberate, separate feature.
    Re-running the same model later isn't guaranteed to reproduce the exact
    proposal a human actually saw and (maybe) corrected at approval time,
    and if nothing was corrected, comparing a fresh re-run to the untouched
    invoice is tautological (naturally ~$0 error, telling you nothing about
    real-world accuracy). This function instead diffs the job's own
    *original, frozen* `proposal_json` against the amounts the approver just
    chose to write - the one true "was the model right, or did a human have
    to fix it" signal, captured exactly once, exactly when it happened.

    Appends one row to eval/history.jsonl (reusing the same schema
    eval/run_eval.py and the Admin Eval Dashboard already read), tagged with
    a "(approved)" model suffix so it's never mistaken for - or skipped as
    already-covering - a CLI re-scoring run of the same real model slug.
    """
    job = db.get(BillImportJob, int(job_id))
    if not job or not job.proposal_json:
        return

    try:
        proposal = json.loads(job.proposal_json)
    except Exception:
        return

    roster = json.loads(job.known_roster_json) if job.known_roster_json else member_identifiers.build_known_roster(db, plan_id)
    lines = list(proposal.get("lines") or [])

    try:
        bill_period_key = vectorstore.period_key(int(proposal.get("year")), str(proposal.get("month")))
    except Exception:
        bill_period_key = None

    predicted_view = build_member_allocation_view(
        db, plan_id, roster, lines, bill_period_key=bill_period_key, lookback_months=VECTOR_RETRIEVAL_LOOKBACK_MONTHS
    )
    predicted_by_member = {m.member_id: m.amount for m in predicted_view.members}
    basis_by_member = {m.member_id: m.basis for m in predicted_view.members}

    all_member_ids = set(predicted_by_member) | set(approved_by_member)
    per_member = []
    for mid in all_member_ids:
        predicted = round(predicted_by_member.get(mid, 0.0), 2)
        approved = round(approved_by_member.get(mid, 0.0), 2)
        per_member.append(
            {
                "member_id": mid,
                "actual": approved,
                "proposed": predicted,
                "diff": round(predicted - approved, 2),
                "basis": basis_by_member.get(mid, "none"),
            }
        )

    approved_total = round(sum(approved_by_member.values()), 2)
    predicted_total = round(sum(predicted_by_member.values()), 2)
    try:
        usage = json.loads(job.token_usage_json) if job.token_usage_json else {}
    except Exception:
        usage = {}
    model = str(usage.get("model") or OPENROUTER_MODEL)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job.id,
        "plan_id": plan_id,
        "model": f"{model} (approved)",
        "invoice_id": job.invoice_id,
        "json_parse_success": True,
        "error": None,
        "month_year_match": True,
        "actual_total": approved_total,
        "proposed_total": predicted_total,
        "total_amount_diff": round(predicted_total - approved_total, 2),
        "total_amount_pct_diff": round(abs(predicted_total - approved_total) / approved_total * 100, 2) if approved_total else None,
        "per_member": per_member,
        "mean_abs_per_member_diff": round(sum(abs(p["diff"]) for p in per_member) / len(per_member), 2) if per_member else 0.0,
        "mean_abs_diff_active_members": mean_abs_diff_active(per_member),
        "unmatched_lines": predicted_view.unmatched_lines,
        "unmatched_total": predicted_view.unmatched_total,
        "none_total": predicted_view.none_total,
        "unattributed_total": predicted_view.unattributed_total,
    }

    try:
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        with open(_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never block a successful ledger write on a logging hiccup


def _run_job(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(BillImportJob, int(job_id))
        plan = plans_service.get_plan(db, job.plan_id)
        carrier_type = plan.carrier_type if plan else None
        cleaned_text = job.cleaned_text
        plan_id = job.plan_id
        roster = member_identifiers.build_known_roster(db, plan_id)

    # Shared across chunk-selection, precedent-retrieval, and the LLM call so
    # the job's persisted token_usage_json reflects the *total* embedding +
    # chat cost incurred to produce this result (see _usage_to_dict in
    # llm_invoice_extract_v2.py for the chat side).
    usage_sink: dict[str, Any] = {}

    chunks = bill_preprocess.chunk_text(cleaned_text)
    selected_chunks = vectorstore.select_relevant_chunks(chunks, DEFAULT_SELECTION_QUERIES, top_n=6, usage_sink=usage_sink)
    if not selected_chunks:
        selected_chunks = [cleaned_text[:12000]]

    # Guess this bill's own period (regex, no LLM call) so precedent never
    # leaks facts from AFTER the bill being processed - important when
    # backfilling/evaluating an old bill long after the fact.
    guessed_period_key = bill_preprocess.guess_bill_period_key(cleaned_text)
    precedent_facts = vectorstore.retrieve_precedent(
        plan_id,
        member_ids=[r["member_id"] for r in roster],
        periods_per_member=2,
        lookback_months=VECTOR_RETRIEVAL_LOOKBACK_MONTHS,
        before_period_key=guessed_period_key,
    )

    proposal = _extract_with_retry(selected_chunks, precedent_facts, roster, carrier_type)
    usage_sink.update(proposal.token_usage or {})

    try:
        bill_period_key = vectorstore.period_key(proposal.year, proposal.month)
    except Exception:
        bill_period_key = None

    with SessionLocal() as db:
        # Deterministic match always wins over the LLM's suggestion; the LLM's
        # guess is kept only as a suggestion for lines the deterministic
        # lookup couldn't resolve (surfaced/pre-filled in the review UI).
        # Also clears any LLM suggestion that collides with another line's
        # match (see member_identifiers.dedupe_llm_suggestions) - persisting
        # the deduped state here means the Inspector and every future poll
        # see the same corrected matches, not just the first render.
        member_identifiers.apply_deterministic_matches(db, plan_id, proposal.lines)
        member_identifiers.dedupe_llm_suggestions(proposal.lines)

        proposal_dict = {
            "year": proposal.year,
            "month": proposal.month,
            "total_amount": proposal.total_amount,
            "confidence": proposal.confidence,
            "evidence_total": proposal.evidence_total,
            "evidence_period": proposal.evidence_period,
            "lines": proposal.lines,
            "allocation_by_line": proposal.allocation_by_line,
            "notes": proposal.notes,
        }

        existing_invoice = db.execute(
            select(Invoice).where(Invoice.plan_id == plan_id, Invoice.year == proposal.year, Invoice.month == proposal.month)
        ).scalars().first()

        job = db.get(BillImportJob, int(job_id))
        job.selected_chunks_json = json.dumps(selected_chunks)
        job.precedent_used_json = json.dumps(precedent_facts)
        job.llm_raw_response = (proposal.raw_response or "")[:20000]
        job.proposal_json = json.dumps(proposal_dict)
        job.system_prompt = proposal.system_prompt or None
        job.known_roster_json = json.dumps(roster)
        job.token_usage_json = json.dumps(usage_sink)

        if existing_invoice:
            job.mode = "EVALUATE_ONLY"
            job.invoice_id = existing_invoice.id
            job.diff_json = json.dumps(
                _diff_against_invoice(db, plan_id, roster, existing_invoice, proposal, bill_period_key)
            )

            # The existing invoice is already-approved ground truth - seed the
            # vector store from it immediately, no UI click needed. This is
            # what makes "upload your archive of old bills" the actual
            # backfill mechanism (decision 6): without this, evaluate-only
            # uploads would diff correctly but never contribute precedent.
            try:
                facts = vectorstore.build_outcome_facts_for_invoice(db, existing_invoice.id)
                vectorstore.upsert_outcome_facts(
                    plan_id=plan_id,
                    invoice_ref=f"invoice-{existing_invoice.id}",
                    year=existing_invoice.year,
                    month=existing_invoice.month,
                    facts=facts,
                )
            except Exception:
                pass  # never fail a job because of a vector-store hiccup
        else:
            job.mode = "NORMAL"

        job.status = "DONE"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
