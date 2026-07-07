"""Add members.created_by_member_id (attribution for who added a member).

Non-destructive/additive migration:
  1. Add nullable `members.created_by_member_id` FK to `members.id`.
  2. No backfill - existing members stay NULL, meaning "not attributable to
     a specific creator" (treated as admin/legacy, editable only by the
     application OWNER). Only members created going forward by a MEMBER-role
     plan owner (via the Plans tab's "add a brand-new member" flow) get this
     column populated, which is what unlocks that owner's ability to edit
     them later.

See docs/decisions/2026-07-04-roadmap/07-member-management-authorization.md
for background.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "n7"
down_revision: str | None = "n6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    member_cols = _columns("members")
    if not member_cols or "created_by_member_id" in member_cols:
        # Fresh install, or already created with the final model shape.
        return

    with op.batch_alter_table("members") as batch_op:
        batch_op.add_column(
            sa.Column(
                "created_by_member_id",
                sa.Integer(),
                sa.ForeignKey("members.id", name="fk_members_created_by_member_id"),
                nullable=True,
            )
        )


def downgrade() -> None:
    member_cols = _columns("members")
    if "created_by_member_id" in member_cols:
        with op.batch_alter_table("members") as batch_op:
            batch_op.drop_column("created_by_member_id")
