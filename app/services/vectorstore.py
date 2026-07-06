"""
Vector store for the Bill Import v2 (RAG, opt-in) pipeline.

Does two jobs, neither of which is a chat-completion call:
  1. select_relevant_chunks() - transient, in-memory ranking of the CURRENT
     bill's own chunks against a few fixed queries (replaces the legacy
     regex-anchor filter in bill_text_filter.py). Nothing here is persisted.
  2. upsert_outcome_facts()/retrieve_precedent() - a small, persistent Chroma
     collection of compact "outcome facts" (plain strings built from
     already-computed/approved data, never a fresh LLM summarization call),
     scoped per plan and used as historical precedent for future imports.

Embeddings always go through app/services/embeddings_client.py (OpenRouter),
never Chroma's own bundled default embedder - this avoids pulling in a local
onnxruntime-based model on the memory-constrained e2-micro VM.

data/vectorstore/ is the one new persistent directory in this design. It is
gitignored and fully regenerable from already-approved Invoice/Allocation
rows via scripts/rebuild_vectorstore.py - the DB, not the vector store, is
the source of truth.
"""

from __future__ import annotations

import math
import os
from typing import Any

from langchain_chroma import Chroma

from app.services.embeddings_client import LangchainOpenRouterEmbeddings, embed_texts

VECTORSTORE_DIR = os.path.join("data", "vectorstore")
OUTCOME_FACTS_COLLECTION = "outcome_facts"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]

_store: Chroma | None = None


def _get_store() -> Chroma:
    global _store
    if _store is None:
        os.makedirs(VECTORSTORE_DIR, exist_ok=True)
        _store = Chroma(
            collection_name=OUTCOME_FACTS_COLLECTION,
            embedding_function=LangchainOpenRouterEmbeddings(),
            persist_directory=VECTORSTORE_DIR,
        )
    return _store


def period_key(year: int, month: str) -> int:
    """Sortable/filterable integer key, e.g. 2026-Oct -> 202610, for lookback-window filtering."""
    try:
        month_num = _MONTHS.index(month) + 1
    except ValueError:
        month_num = 1
    return int(year) * 100 + month_num


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def select_relevant_chunks(
    chunks: list[Any], queries: list[str], top_n: int = 6, usage_sink: dict | None = None
) -> list[str]:
    """
    Transient (never persisted): embed the current bill's own chunks once,
    rank them against a few fixed queries, return the top-N chunk texts.
    Replaces the legacy regex-anchor search - generalizes across carriers
    and bounds the tokens sent to the LLM on long bills.

    `usage_sink`, if provided, accumulates embedding token/cost usage - see
    app/services/embeddings_client.py's _accumulate_usage().
    """
    texts = [c.page_content if hasattr(c, "page_content") else str(c) for c in chunks]
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return []
    if len(texts) <= top_n:
        return texts

    chunk_vecs = embed_texts(texts, usage_sink=usage_sink)
    query_vecs = embed_texts(queries or ["total amount due and per-line bill summary"], usage_sink=usage_sink)

    scored = []
    for text, vec in zip(texts, chunk_vecs):
        score = max((_cosine(vec, qv) for qv in query_vecs), default=0.0)
        scored.append((score, text))

    scored.sort(key=lambda t: t[0], reverse=True)
    # Preserve original order among the selected top-N so the LLM sees the
    # bill in its natural reading order, not similarity-ranked order.
    top_texts = {text for _, text in scored[:top_n]}
    return [t for t in texts if t in top_texts]


def build_outcome_facts_for_invoice(db: Any, invoice_id: int) -> list[dict[str, Any]]:
    """
    Build compact, carrier-agnostic outcome facts from an already approved
    Invoice's Allocation rows - no LLM call, just plain string formatting
    from already-computed data. Each fact carries its member_id AND raw
    amount so precedent retrieval can look facts up deterministically per
    member (see retrieve_precedent()/get_latest_amount_per_member() below)
    instead of via semantic search or regex-parsing the formatted string.
    Shared by the post-approve hook (app/ui/bill_import.py) and
    scripts/rebuild_vectorstore.py.
    """
    from app.db.models import Allocation, Invoice, Member

    invoice = db.get(Invoice, int(invoice_id))
    if not invoice:
        return []

    facts = []
    for alloc in invoice.allocations:
        member = db.get(Member, alloc.member_id) if alloc.member_id else None
        member_name = member.name if member else "Unassigned"
        facts.append(
            {
                "member_id": int(alloc.member_id) if alloc.member_id else None,
                "amount": float(alloc.amount_due),
                "fact": f"{member_name}: ${alloc.amount_due:.2f} in {invoice.month} {invoice.year}",
            }
        )
    return facts


def upsert_outcome_facts(plan_id: int, invoice_ref: str, year: int, month: str, facts: list[dict[str, Any]]) -> None:
    """
    Embed and store compact per-member outcome facts built from already-
    computed data (approved allocation, or an extraction result) - no LLM
    call here. Deterministic ids (plan_id:invoice_ref:index) make this an
    upsert: re-approving the same invoice replaces its old facts rather than
    duplicating them. `member_id`/`amount` are stored in metadata (Chroma
    requires scalars, so "no member"/account-level facts use member_id=-1)
    so retrieve_precedent()/get_latest_amount_per_member() can look facts up
    per member deterministically rather than by semantic similarity or by
    regex-parsing the formatted fact string - see retrieve_precedent()'s
    docstring for why semantic search was dropped for this data.
    """
    facts = [f for f in (facts or []) if f and str(f.get("fact") or "").strip()]
    if not facts:
        return

    store = _get_store()
    ids = [f"{plan_id}:{invoice_ref}:{i}" for i in range(len(facts))]
    texts = [str(f["fact"]) for f in facts]
    pkey = period_key(year, month)
    metadatas = [
        {
            "plan_id": int(plan_id),
            "period_key": pkey,
            "invoice_ref": str(invoice_ref),
            "member_id": int(f["member_id"]) if f.get("member_id") is not None else -1,
            "amount": float(f.get("amount") or 0.0),
        }
        for f in facts
    ]
    store.add_texts(texts=texts, metadatas=metadatas, ids=ids)


def _period_key_label(pkey: int) -> str:
    year, month_num = divmod(int(pkey), 100)
    month_name = _MONTHS[month_num - 1] if 1 <= month_num <= 12 else str(month_num)
    return f"{month_name} {year}"


def _fetch_outcome_facts(
    plan_id: int, member_ids: list[int], before_period_key: int | None, lookback_months: int | None
) -> dict[int, list[tuple[int, float, str]]]:
    """Shared metadata-filtered fetch behind retrieve_precedent() and
    get_latest_amount_per_member() - returns {member_id: [(period_key, amount, fact_text), ...]}."""
    store = _get_store()
    member_ids = [int(m) for m in member_ids]
    conditions: list[dict[str, Any]] = [{"plan_id": int(plan_id)}, {"member_id": {"$in": member_ids}}]

    if before_period_key:
        conditions.append({"period_key": {"$lt": int(before_period_key)}})

    if lookback_months:
        if before_period_key:
            cutoff_key = _shift_period_key(before_period_key, lookback_months)
        else:
            import datetime

            today = datetime.date.today()
            cutoff_key = _shift_period_key(today.year * 100 + today.month, lookback_months)
        conditions.append({"period_key": {"$gte": cutoff_key}})

    where: dict[str, Any] = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    try:
        raw = store.get(where=where, include=["documents", "metadatas"])
    except Exception:
        return {}

    by_member: dict[int, list[tuple[int, float, str]]] = {}
    for doc, meta in zip(raw.get("documents") or [], raw.get("metadatas") or []):
        mid = meta.get("member_id")
        if mid is None or int(mid) < 0:
            continue
        amount = float(meta.get("amount") or 0.0)
        by_member.setdefault(int(mid), []).append((int(meta.get("period_key") or 0), amount, doc))
    return by_member


def get_latest_amount_per_member(
    plan_id: int,
    member_ids: list[int],
    before_period_key: int | None = None,
    lookback_months: int | None = None,
) -> dict[int, dict[str, Any]]:
    """
    Structured counterpart to retrieve_precedent(): for each member, their
    single most recent outcome fact's dollar amount (not just formatted
    text). Powers the "no identifier match on this bill yet, but we know
    their usual amount" fallback suggestion in the NORMAL-mode review UI
    (app/ui/bill_import.py) - never auto-approved, always a clearly-labeled,
    editable, low-confidence suggestion the owner confirms before it affects
    the ledger. Same before_period_key/lookback_months semantics as
    retrieve_precedent() (never leaks facts from after the bill in hand).
    """
    if not plan_id or not member_ids:
        return {}

    by_member = _fetch_outcome_facts(plan_id, member_ids, before_period_key, lookback_months)
    result: dict[int, dict[str, Any]] = {}
    for mid, records in by_member.items():
        if not records:
            continue
        pkey, amount, _ = max(records, key=lambda r: r[0])
        result[mid] = {"amount": amount, "period_key": pkey, "label": _period_key_label(pkey)}
    return result


def _shift_period_key(period_key: int, months: int) -> int:
    year, month = divmod(int(period_key), 100)
    month_index = month - 1 - int(months)
    new_year = year + (month_index // 12)
    new_month = (month_index % 12) + 1
    return new_year * 100 + new_month


def retrieve_precedent(
    plan_id: int,
    member_ids: list[int],
    periods_per_member: int = 2,
    lookback_months: int | None = None,
    before_period_key: int | None = None,
) -> list[str]:
    """
    Deterministic, NOT semantic: for each of this plan's known members,
    return their `periods_per_member` most recent outcome facts (by
    period_key) STRICTLY BEFORE `before_period_key` - a metadata-filtered
    lookup, no embedding call needed.

    This replaces an earlier semantic-search version. In production testing,
    embedding similarity search over these facts (short, near-identical
    "Name: $amount in Month Year" strings) did NOT correlate with genuine
    relevance: for the exact same invoice period, one member's fact would
    consistently score as "more similar" to a generic query than every other
    member's fact for that same invoice - an embedding-model artifact, not a
    real signal - so a fixed top-K semantic search could return 8 facts for
    one member and zero for anyone else. Since every outcome fact is built
    from the plan's own Invoice/Allocation rows (never bill text), the
    membership of "which members exist" is always known upfront, so a
    deterministic per-member lookup is strictly better here: it guarantees
    coverage across the whole roster, costs zero embedding calls, and can't
    be biased by embedding-space quirks.

    `before_period_key` should be the bill's OWN period (see
    bill_preprocess.guess_bill_period_key) - never "today". Precedent must
    never leak facts from AFTER the bill being processed, which matters a
    lot when backfilling/evaluating an old bill long after the fact (e.g.
    importing a 2024 bill in 2026 must not see 2025/2026 outcome facts - that
    both leaks "future" information into the prompt and is nonsensical as
    precedent for a bill written years earlier). If the bill's own period
    can't be guessed, `lookback_months` (if given) falls back to being
    relative to today, same as before this parameter existed.
    """
    if not plan_id or not member_ids:
        return []

    member_ids = [int(m) for m in member_ids]
    by_member = _fetch_outcome_facts(plan_id, member_ids, before_period_key, lookback_months)

    facts: list[str] = []
    for mid in member_ids:
        recs = sorted(by_member.get(mid, []), key=lambda t: t[0], reverse=True)
        facts.extend(doc for _, _, doc in recs[:periods_per_member])
    return facts
