"""Add member reminder preferences."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "n3"
down_revision: str | None = "n2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns("members")
    if not cols:
        return

    with op.batch_alter_table("members") as batch_op:
        if "email_enabled" not in cols:
            batch_op.add_column(sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        if "sms_enabled" not in cols:
            batch_op.add_column(sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "whatsapp_enabled" not in cols:
            batch_op.add_column(sa.Column("whatsapp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))

    cols = _columns("members")
    if "email_enabled" in cols and "email" in cols:
        conn.execute(text("UPDATE members SET email_enabled = CASE WHEN COALESCE(email, '') <> '' THEN 1 ELSE 0 END"))


def downgrade() -> None:
    cols = _columns("members")
    if not cols:
        return

    with op.batch_alter_table("members") as batch_op:
        for column_name in ("whatsapp_enabled", "sms_enabled", "email_enabled"):
            if column_name in cols:
                batch_op.drop_column(column_name)
