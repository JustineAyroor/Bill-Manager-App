"""
Regenerate the entire Bill Import v2 vector store (data/vectorstore/) from
already-approved Invoice/Allocation rows.

Because outcome-fact embeddings are a deterministic function of already-
approved data (the real source of truth, backed up as part of tmobile.db),
this script exists so data/vectorstore/ never has to be treated as
precious - it can be wiped and rebuilt at any time (VM move, disk wipe,
switching OPENROUTER_EMBEDDING_MODEL, etc) at embedding-only cost, with zero
chat-completion calls.

Usage:
  uv run python scripts/rebuild_vectorstore.py [--plan-id ID]
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running as `uv run python scripts/rebuild_vectorstore.py` from the
# project root without also having to set PYTHONPATH=.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Invoice
from app.services import vectorstore as vs


def rebuild(plan_id: int | None = None) -> int:
    count = 0
    with SessionLocal() as db:
        stmt = select(Invoice)
        if plan_id:
            stmt = stmt.where(Invoice.plan_id == int(plan_id))
        invoices = db.execute(stmt).scalars().all()

        for invoice in invoices:
            facts = vs.build_outcome_facts_for_invoice(db, invoice.id)
            if not facts:
                continue
            invoice_ref = f"invoice-{invoice.id}"
            vs.upsert_outcome_facts(
                plan_id=invoice.plan_id,
                invoice_ref=invoice_ref,
                year=invoice.year,
                month=invoice.month,
                facts=facts,
            )
            count += 1

    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", type=int, default=None, help="Only rebuild for this plan (default: all plans)")
    args = parser.parse_args()

    n = rebuild(plan_id=args.plan_id)
    print(f"Rebuilt outcome facts for {n} invoice(s).", file=sys.stderr)
