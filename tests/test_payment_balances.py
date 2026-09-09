"""Regression tests for payment add/delete vs dashboard totals.

Dashboard recovered/outstanding figures are summed from PaymentApplication
rows (joined to a still-existing Payment), not from Payment.amount. Deleting
a payment without clearing or rebuilding applications used to leave those
totals unchanged.
"""

from __future__ import annotations

import unittest
from datetime import date

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Allocation,
    Base,
    Invoice,
    Member,
    Payment,
    PaymentApplication,
    Plan,
    PlanMember,
)
from app.services import crud
from app.services.accounting import member_balances, plan_totals
from app.services.payment_apply import (
    auto_apply_payment_fifo,
    purge_orphan_payment_applications,
    reconcile_all_members_fifo,
    reconcile_member_fifo,
)


def _member_row(rows, name):
    for r in rows:
        if r["member"] == name:
            return r
    raise AssertionError(f"member {name!r} not in {rows}")


class PaymentBalanceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db = self.Session()

        owner = Member(name="Justine", is_active=1)
        member = Member(name="Alex", is_active=1)
        self.db.add_all([owner, member])
        self.db.flush()

        plan = Plan(name="Family Plan", owner_member_id=owner.id)
        self.db.add(plan)
        self.db.flush()
        self.db.add_all(
            [
                PlanMember(plan_id=plan.id, member_id=owner.id),
                PlanMember(plan_id=plan.id, member_id=member.id),
            ]
        )

        inv = Invoice(plan_id=plan.id, year=2026, month="Jan", total_amount=100.0)
        self.db.add(inv)
        self.db.flush()
        self.db.add_all(
            [
                Allocation(invoice_id=inv.id, member_id=owner.id, amount_due=40.0),
                Allocation(invoice_id=inv.id, member_id=member.id, amount_due=60.0),
            ]
        )
        self.db.commit()

        self.plan_id = plan.id
        self.owner_id = owner.id
        self.member_id = member.id
        self.invoice_id = inv.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add_inbound(self, amount, when=None, apply=True):
        p = crud.add_payment(
            self.db,
            self.plan_id,
            when=when or date(2026, 1, 15),
            amount=amount,
            direction="INBOUND",
            description="test payment",
            member_id=self.member_id,
        )
        self.db.flush()
        if apply:
            auto_apply_payment_fifo(self.db, p.id)
        self.db.commit()
        return p

    def test_add_payment_updates_recovered_and_outstanding(self):
        before = plan_totals(self.db, plan_id=self.plan_id)
        self.assertEqual(before["plan_due_outstanding"], 60.0)
        self.assertEqual(before["plan_recovered"], 0.0)

        self._add_inbound(25.0)

        after = plan_totals(self.db, plan_id=self.plan_id)
        alex = _member_row(member_balances(self.db, plan_id=self.plan_id), "Alex")
        self.assertEqual(after["plan_recovered"], 25.0)
        self.assertEqual(after["plan_due_outstanding"], 35.0)
        self.assertEqual(alex["total_paid"], 25.0)
        self.assertEqual(alex["balance"], 35.0)

    def test_delete_payment_restores_balances(self):
        p = self._add_inbound(25.0)
        crud.delete_payment(self.db, p.id)
        reconcile_member_fifo(self.db, self.member_id, plan_id=self.plan_id)
        self.db.commit()

        after = plan_totals(self.db, plan_id=self.plan_id)
        alex = _member_row(member_balances(self.db, plan_id=self.plan_id), "Alex")
        leftover = self.db.execute(
            select(PaymentApplication).where(PaymentApplication.payment_id == p.id)
        ).scalars().all()

        self.assertEqual(after["plan_recovered"], 0.0)
        self.assertEqual(after["plan_due_outstanding"], 60.0)
        self.assertEqual(alex["total_paid"], 0.0)
        self.assertEqual(alex["balance"], 60.0)
        self.assertEqual(leftover, [])

    def test_orphaned_applications_do_not_count_toward_totals(self):
        """Reproduces the original bug: SQL DELETE of the payment leaves apps."""
        p = self._add_inbound(25.0)
        pid = p.id
        self.db.execute(text("DELETE FROM payments WHERE id = :id"), {"id": pid})
        self.db.commit()
        self.db.expire_all()

        orphans = self.db.execute(select(PaymentApplication)).scalars().all()
        self.assertTrue(orphans, "expected leftover application rows")

        totals = plan_totals(self.db, plan_id=self.plan_id)
        alex = _member_row(member_balances(self.db, plan_id=self.plan_id), "Alex")
        self.assertEqual(totals["plan_recovered"], 0.0)
        self.assertEqual(alex["total_paid"], 0.0)
        self.assertEqual(alex["balance"], 60.0)

    def test_purge_and_reconcile_all_heal_orphans(self):
        p = self._add_inbound(25.0)
        self.db.execute(text("DELETE FROM payments WHERE id = :id"), {"id": p.id})
        self.db.commit()
        self.db.expunge_all()

        removed = purge_orphan_payment_applications(self.db)
        self.db.commit()
        self.assertEqual(removed, 1)
        self.assertEqual(self.db.execute(select(PaymentApplication)).scalars().all(), [])

        # Re-create the orphaned state and heal via Reconcile ALL (the UI path
        # that used to skip members with zero remaining inbound payments).
        p2 = self._add_inbound(40.0)
        self.db.execute(text("DELETE FROM payments WHERE id = :id"), {"id": p2.id})
        self.db.commit()
        self.db.expunge_all()
        self.assertTrue(self.db.execute(select(PaymentApplication)).scalars().all())

        reconcile_all_members_fifo(self.db, plan_id=self.plan_id)
        self.db.commit()
        self.assertEqual(self.db.execute(select(PaymentApplication)).scalars().all(), [])
        self.assertEqual(plan_totals(self.db, plan_id=self.plan_id)["plan_recovered"], 0.0)

    def test_deleting_earlier_payment_reapplies_remaining_fifo(self):
        inv2 = Invoice(plan_id=self.plan_id, year=2026, month="Feb", total_amount=50.0)
        self.db.add(inv2)
        self.db.flush()
        self.db.add(Allocation(invoice_id=inv2.id, member_id=self.member_id, amount_due=50.0))
        self.db.commit()

        first = self._add_inbound(60.0, when=date(2026, 1, 10))
        second = self._add_inbound(50.0, when=date(2026, 2, 10))

        apps_before = self.db.execute(
            select(PaymentApplication.invoice_id, PaymentApplication.amount_applied).where(
                PaymentApplication.payment_id == second.id
            )
        ).all()
        self.assertEqual([(r[0], r[1]) for r in apps_before], [(inv2.id, 50.0)])

        crud.delete_payment(self.db, first.id)
        reconcile_member_fifo(self.db, self.member_id, plan_id=self.plan_id)
        self.db.commit()

        apps_after = self.db.execute(
            select(PaymentApplication.invoice_id, PaymentApplication.amount_applied).where(
                PaymentApplication.payment_id == second.id
            )
        ).all()
        # Remaining $50 now covers the oldest unpaid invoice (January $60).
        self.assertEqual([(r[0], r[1]) for r in apps_after], [(self.invoice_id, 50.0)])
        alex = _member_row(member_balances(self.db, plan_id=self.plan_id), "Alex")
        self.assertEqual(alex["total_paid"], 50.0)
        self.assertEqual(alex["balance"], 60.0)

    def test_adding_earlier_payment_reapplies_fifo(self):
        inv2 = Invoice(plan_id=self.plan_id, year=2026, month="Feb", total_amount=50.0)
        self.db.add(inv2)
        self.db.flush()
        self.db.add(Allocation(invoice_id=inv2.id, member_id=self.member_id, amount_due=50.0))
        self.db.commit()

        later = self._add_inbound(60.0, when=date(2026, 2, 10))
        earlier = crud.add_payment(
            self.db,
            self.plan_id,
            when=date(2026, 1, 5),
            amount=60.0,
            direction="INBOUND",
            member_id=self.member_id,
        )
        self.db.flush()
        reconcile_member_fifo(self.db, self.member_id, plan_id=self.plan_id)
        self.db.commit()

        earlier_apps = [
            (r[0], r[1])
            for r in self.db.execute(
                select(PaymentApplication.invoice_id, PaymentApplication.amount_applied).where(
                    PaymentApplication.payment_id == earlier.id
                )
            ).all()
        ]
        later_apps = [
            (r[0], r[1])
            for r in self.db.execute(
                select(PaymentApplication.invoice_id, PaymentApplication.amount_applied).where(
                    PaymentApplication.payment_id == later.id
                )
            ).all()
        ]
        self.assertEqual(earlier_apps, [(self.invoice_id, 60.0)])
        self.assertEqual(later_apps, [(inv2.id, 50.0)])

    def test_edit_amount_rebuilds_applications(self):
        p = self._add_inbound(25.0)
        crud.update_payment(
            self.db,
            payment_id=p.id,
            when=date(2026, 1, 15),
            amount=60.0,
            direction="INBOUND",
            member_id=self.member_id,
        )
        self.db.flush()
        reconcile_member_fifo(self.db, self.member_id, plan_id=self.plan_id)
        self.db.commit()

        alex = _member_row(member_balances(self.db, plan_id=self.plan_id), "Alex")
        self.assertEqual(alex["total_paid"], 60.0)
        self.assertEqual(alex["balance"], 0.0)
        applied = self.db.execute(
            select(PaymentApplication.amount_applied).where(PaymentApplication.payment_id == p.id)
        ).scalar_one()
        self.assertEqual(float(applied), 60.0)


if __name__ == "__main__":
    unittest.main()
