"""Add observability columns to bill_import_jobs.

Purely additive migration - lets the "Inspect a job" UI and the admin Eval
Dashboard show exactly what was sent to the LLM (system_prompt,
known_roster_json), what came back and cost (token_usage_json), and how many
times a duplicate upload short-circuited to this job's cached result
(cache_hit_count).

See docs/decisions/2026-07-04-roadmap/08-llm-bill-import-rag-architecture.md
for background.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "n10"
down_revision: str | None = "n9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    cols = _columns("bill_import_jobs")
    if not cols or "system_prompt" in cols:
        # Fresh install, or already created with the final model shape.
        return

    with op.batch_alter_table("bill_import_jobs") as batch_op:
        batch_op.add_column(sa.Column("system_prompt", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("known_roster_json", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("token_usage_json", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("cache_hit_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    cols = _columns("bill_import_jobs")
    if "system_prompt" in cols:
        with op.batch_alter_table("bill_import_jobs") as batch_op:
            batch_op.drop_column("system_prompt")
            batch_op.drop_column("known_roster_json")
            batch_op.drop_column("token_usage_json")
            batch_op.drop_column("cache_hit_count")
