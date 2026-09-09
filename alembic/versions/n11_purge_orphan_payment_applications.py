"""Purge payment_applications whose parent payment no longer exists.

SQLite does not enforce foreign keys unless PRAGMA foreign_keys is on, and
delete_payment used to remove the Payment row without touching its
PaymentApplication rows. Dashboard recovered/outstanding totals are summed
from applications, so those orphans kept balances unchanged after a delete.

This is a data-repair migration: it deletes the leftover rows. Runtime
delete/edit/reconcile paths now clear applications and rebuild FIFO.

See docs/bug-resolutions.md.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect, text

revision: str = "n11"
down_revision: str | None = "n10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table("payment_applications") or not insp.has_table("payments"):
        return
    conn.execute(
        text(
            "DELETE FROM payment_applications "
            "WHERE payment_id NOT IN (SELECT id FROM payments)"
        )
    )


def downgrade() -> None:
    # Data repair cannot be reversed.
    return
