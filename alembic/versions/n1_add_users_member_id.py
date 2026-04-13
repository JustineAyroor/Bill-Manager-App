"""Add users.member_id when missing (legacy SQLite DBs)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "n1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table("users"):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "member_id" in cols:
        return
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("member_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table("users"):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "member_id" not in cols:
        return
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("member_id")
