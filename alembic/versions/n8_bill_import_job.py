"""Add bill_import_jobs table for the Bill Import v2 (RAG, opt-in) pipeline.

Purely additive - no existing table is touched. This is the audit
trail/golden dataset for the new pipeline; the legacy synchronous import
flow (app/services/llm_invoice_extract.py, app/ui/bill_import.py) never
writes here and is unaffected.

See docs/decisions/2026-07-04-roadmap/08-llm-bill-import-rag-architecture.md
for background.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "n8"
down_revision: str | None = "n7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    if _has_table("bill_import_jobs"):
        # Fresh install already created via Base.metadata.create_all() with
        # the final model shape.
        return

    op.create_table(
        "bill_import_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", name="fk_bill_import_jobs_plan_id"), nullable=False),
        sa.Column(
            "uploaded_by_member_id",
            sa.Integer(),
            sa.ForeignKey("members.id", name="fk_bill_import_jobs_uploaded_by_member_id"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("cleaned_text", sa.String(), nullable=False),
        sa.Column("selected_chunks_json", sa.String(), nullable=True),
        sa.Column("precedent_used_json", sa.String(), nullable=True),
        sa.Column("llm_raw_response", sa.String(), nullable=True),
        sa.Column("proposal_json", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column(
            "invoice_id",
            sa.Integer(),
            sa.ForeignKey("invoices.id", name="fk_bill_import_jobs_invoice_id"),
            nullable=True,
        ),
        sa.Column("diff_json", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("plan_id", "content_hash", name="uq_bill_import_job_plan_hash"),
    )


def downgrade() -> None:
    if _has_table("bill_import_jobs"):
        op.drop_table("bill_import_jobs")
