"""Add payments.plan_id (deterministic plan attribution for payments).

Non-destructive/additive migration, same style as n5:
  1. If `payments` already has `plan_id` (fresh install created via
     create_all with the final model shape), there is nothing to backfill.
  2. Otherwise, backfill each existing payment's plan by priority:
       a. via its linked invoice (`Payment.invoice_id` -> `Invoice.plan_id`), else
       b. via the payer's plan membership (`Payment.member_id` -> `plan_members`),
          only when that member belongs to exactly one plan, else
       c. the Default Plan (created by n5) - this covers unlinked outbound
          payments and members in zero/multiple plans.
  3. Tighten the column to NOT NULL.

See docs/decisions/2026-07-04-roadmap/04-multi-plan-schema.md for background.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "n6"
down_revision: str | None = "n5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_PLAN_NAME = "Default Plan"


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)

    payment_cols = _columns("payments")
    if not payment_cols or "plan_id" in payment_cols:
        # Fresh install (table not created yet, or created_all already built
        # it with the final shape) - nothing left to backfill.
        return

    if not insp.has_table("plans"):
        return

    default_plan_id = conn.execute(
        text("SELECT id FROM plans WHERE name = :name"), {"name": DEFAULT_PLAN_NAME}
    ).scalar()
    if default_plan_id is None:
        # No default plan (e.g. n5 never ran because this DB predates plans
        # entirely) - fall back to the first plan, if any.
        default_plan_id = conn.execute(text("SELECT id FROM plans ORDER BY id LIMIT 1")).scalar()

    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("plans.id", name="fk_payments_plan_id"),
                nullable=True,
            )
        )

    payments = conn.execute(text("SELECT id, invoice_id, member_id FROM payments")).fetchall()
    for payment_id, invoice_id, member_id in payments:
        plan_id = None

        if invoice_id is not None:
            plan_id = conn.execute(
                text("SELECT plan_id FROM invoices WHERE id = :iid"), {"iid": invoice_id}
            ).scalar()

        if plan_id is None and member_id is not None:
            member_plan_ids = [
                row[0]
                for row in conn.execute(
                    text("SELECT plan_id FROM plan_members WHERE member_id = :mid"), {"mid": member_id}
                ).fetchall()
            ]
            if len(member_plan_ids) == 1:
                plan_id = member_plan_ids[0]

        if plan_id is None:
            plan_id = default_plan_id

        if plan_id is not None:
            conn.execute(
                text("UPDATE payments SET plan_id = :pid WHERE id = :id"),
                {"pid": plan_id, "id": payment_id},
            )

    with op.batch_alter_table("payments") as batch_op:
        batch_op.alter_column("plan_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    payment_cols = _columns("payments")
    if "plan_id" in payment_cols:
        with op.batch_alter_table("payments") as batch_op:
            batch_op.drop_column("plan_id")
