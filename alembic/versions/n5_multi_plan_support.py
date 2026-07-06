"""Add plans/plan_members and scope invoices to a plan (multi-plan support).

Non-destructive/additive migration:
  1. Create `plans` and `plan_members` tables (usually already created by
     Base.metadata.create_all() in create_db.py before this runs; guarded
     here in case that ever changes).
  2. If `invoices` already has `plan_id` (fresh install created via
     create_all with the final model shape), there is nothing to backfill.
  3. Otherwise (an existing pre-multi-plan database): create one "Default
     Plan" representing everything that already exists, link every existing
     member to it, backfill invoices.plan_id to it, then tighten the column
     to NOT NULL and swap the old global (year, month) uniqueness for a
     per-plan (plan_id, year, month) uniqueness so a second plan can have its
     own invoice for the same calendar month.

See docs/decisions/2026-07-04-roadmap/04-multi-plan-schema.md for the full
design and rationale.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "n5"
down_revision: str | None = "n4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_PLAN_NAME = "Default Plan"
DEFAULT_PLAN_CARRIER = "T-Mobile"
LEGACY_OWNER_NAME = "Justine"


def _columns(table_name: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)

    if not insp.has_table("plans"):
        op.create_table(
            "plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False, unique=True),
            sa.Column("carrier_type", sa.String(), nullable=True),
            sa.Column("owner_member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not insp.has_table("plan_members"):
        op.create_table(
            "plan_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
            sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("plan_id", "member_id", name="uq_plan_member"),
        )

    invoice_cols = _columns("invoices")
    if not invoice_cols or "plan_id" in invoice_cols:
        # Fresh install (table not created yet, or created_all already built
        # it with the final shape) - nothing left to backfill.
        return

    if not insp.has_table("members"):
        return

    # 1) Create a default plan representing all data that existed before
    #    multi-plan support, so nothing is lost or reassigned.
    default_plan_id = conn.execute(
        text("SELECT id FROM plans WHERE name = :name"), {"name": DEFAULT_PLAN_NAME}
    ).scalar()
    if default_plan_id is None:
        owner_id = conn.execute(
            text("SELECT id FROM members WHERE name = :name"), {"name": LEGACY_OWNER_NAME}
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO plans (name, carrier_type, owner_member_id) "
                "VALUES (:name, :carrier, :owner_id)"
            ),
            {"name": DEFAULT_PLAN_NAME, "carrier": DEFAULT_PLAN_CARRIER, "owner_id": owner_id},
        )
        default_plan_id = conn.execute(
            text("SELECT id FROM plans WHERE name = :name"), {"name": DEFAULT_PLAN_NAME}
        ).scalar()

    # 2) Every existing member belonged to this one implicit plan - backfill
    #    plan_members so they stay visible under the default plan.
    member_ids = [row[0] for row in conn.execute(text("SELECT id FROM members")).fetchall()]
    already_linked = {
        row[0]
        for row in conn.execute(
            text("SELECT member_id FROM plan_members WHERE plan_id = :pid"), {"pid": default_plan_id}
        ).fetchall()
    }
    for member_id in member_ids:
        if member_id not in already_linked:
            conn.execute(
                text("INSERT INTO plan_members (plan_id, member_id) VALUES (:pid, :mid)"),
                {"pid": default_plan_id, "mid": member_id},
            )

    # 3) Add invoices.plan_id (nullable first so existing rows can be
    #    backfilled), point every existing invoice at the default plan, then
    #    tighten the column to NOT NULL and swap the unique constraint.
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("plans.id", name="fk_invoices_plan_id"),
                nullable=True,
            )
        )

    conn.execute(
        text("UPDATE invoices SET plan_id = :pid WHERE plan_id IS NULL"),
        {"pid": default_plan_id},
    )

    with op.batch_alter_table("invoices") as batch_op:
        batch_op.alter_column("plan_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint("uq_invoice_year_month", type_="unique")
        batch_op.create_unique_constraint("uq_invoice_plan_year_month", ["plan_id", "year", "month"])


def downgrade() -> None:
    invoice_cols = _columns("invoices")
    if "plan_id" in invoice_cols:
        with op.batch_alter_table("invoices") as batch_op:
            batch_op.drop_constraint("uq_invoice_plan_year_month", type_="unique")
            batch_op.create_unique_constraint("uq_invoice_year_month", ["year", "month"])
            batch_op.drop_column("plan_id")

    conn = op.get_bind()
    insp = inspect(conn)
    if insp.has_table("plan_members"):
        op.drop_table("plan_members")
    if insp.has_table("plans"):
        op.drop_table("plans")
