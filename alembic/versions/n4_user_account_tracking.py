"""Add user account tracking and password reset fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "n4"
down_revision: str | None = "n3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    cols = _columns("users")
    if not cols:
        return

    with op.batch_alter_table("users") as batch_op:
        if "last_login_at" not in cols:
            batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        if "invite_sent_at" not in cols:
            batch_op.add_column(sa.Column("invite_sent_at", sa.DateTime(timezone=True), nullable=True))
        if "password_reset_token" not in cols:
            batch_op.add_column(sa.Column("password_reset_token", sa.String(), nullable=True))
        if "password_reset_expires_at" not in cols:
            batch_op.add_column(sa.Column("password_reset_expires_at", sa.DateTime(timezone=True), nullable=True))
        if "password_reset_sent_at" not in cols:
            batch_op.add_column(sa.Column("password_reset_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    cols = _columns("users")
    if not cols:
        return

    with op.batch_alter_table("users") as batch_op:
        for column_name in (
            "password_reset_sent_at",
            "password_reset_expires_at",
            "password_reset_token",
            "invite_sent_at",
            "last_login_at",
        ):
            if column_name in cols:
                batch_op.drop_column(column_name)
