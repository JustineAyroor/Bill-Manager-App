"""Expand reminder logs for multiple reminder channels."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "n2"
down_revision: str | None = "n1"
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
    cols = _columns("reminder_logs")
    if not cols:
        return

    with op.batch_alter_table("reminder_logs") as batch_op:
        if "email" in cols:
            batch_op.alter_column("email", existing_type=sa.String(), nullable=True)
        if "subject" in cols:
            batch_op.alter_column("subject", existing_type=sa.String(), nullable=True)
        if "channel" not in cols:
            batch_op.add_column(sa.Column("channel", sa.String(), nullable=False, server_default="EMAIL"))
        if "recipient" not in cols:
            batch_op.add_column(sa.Column("recipient", sa.String(), nullable=True))
        if "sender" not in cols:
            batch_op.add_column(sa.Column("sender", sa.String(), nullable=True))
        if "provider" not in cols:
            batch_op.add_column(sa.Column("provider", sa.String(), nullable=True))
        if "provider_message_id" not in cols:
            batch_op.add_column(sa.Column("provider_message_id", sa.String(), nullable=True))
        if "provider_status" not in cols:
            batch_op.add_column(sa.Column("provider_status", sa.String(), nullable=True))
        if "error_code" not in cols:
            batch_op.add_column(sa.Column("error_code", sa.String(), nullable=True))

    cols = _columns("reminder_logs")
    if "recipient" in cols and "email" in cols:
        conn.execute(text("UPDATE reminder_logs SET recipient = email WHERE recipient IS NULL"))
    if "provider" in cols:
        conn.execute(text("UPDATE reminder_logs SET provider = 'SMTP' WHERE provider IS NULL"))


def downgrade() -> None:
    cols = _columns("reminder_logs")
    if not cols:
        return

    with op.batch_alter_table("reminder_logs") as batch_op:
        for column_name in (
            "error_code",
            "provider_status",
            "provider_message_id",
            "provider",
            "sender",
            "recipient",
            "channel",
        ):
            if column_name in cols:
                batch_op.drop_column(column_name)
        if "subject" in cols:
            batch_op.alter_column("subject", existing_type=sa.String(), nullable=False)
        if "email" in cols:
            batch_op.alter_column("email", existing_type=sa.String(), nullable=False)
