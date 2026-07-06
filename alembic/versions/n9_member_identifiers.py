"""Add member_identifiers table (generalized member-matching identifiers).

Purely additive migration:
  1. Create `member_identifiers` (id, member_id, plan_id nullable,
     identifier_type, identifier_value, created_at). plan_id nullable so an
     identifier can be scoped to one plan or left global across a member's
     plans.
  2. Backfill one PHONE_LAST4 row per existing non-null Member.phone_last4,
     scoped globally (plan_id=NULL), so anything already mapped via the
     legacy Bill Import "save mapping to DB" step carries over with no
     re-entry. Member.phone_last4 itself is untouched - the legacy pipeline
     keeps reading/writing it exactly as before.

See docs/decisions/2026-07-04-roadmap/08-llm-bill-import-rag-architecture.md
for background.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "n9"
down_revision: str | None = "n8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table("member_identifiers"):
        op.create_table(
            "member_identifiers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "member_id",
                sa.Integer(),
                sa.ForeignKey("members.id", name="fk_member_identifiers_member_id"),
                nullable=False,
            ),
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("plans.id", name="fk_member_identifiers_plan_id"),
                nullable=True,
            ),
            sa.Column("identifier_type", sa.String(), nullable=False),
            sa.Column("identifier_value", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    member_cols = _columns("members")
    if member_cols and "phone_last4" in member_cols:
        existing_count = conn.execute(text("SELECT COUNT(*) FROM member_identifiers")).scalar()
        if not existing_count:
            rows = conn.execute(
                text("SELECT id, phone_last4 FROM members WHERE phone_last4 IS NOT NULL AND phone_last4 != ''")
            ).fetchall()
            for member_id, last4 in rows:
                conn.execute(
                    text(
                        "INSERT INTO member_identifiers (member_id, plan_id, identifier_type, identifier_value) "
                        "VALUES (:mid, NULL, 'PHONE_LAST4', :val)"
                    ),
                    {"mid": member_id, "val": str(last4).strip()},
                )


def downgrade() -> None:
    if _has_table("member_identifiers"):
        op.drop_table("member_identifiers")
