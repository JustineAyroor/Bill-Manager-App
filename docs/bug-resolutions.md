# Bug resolutions

Ledger and related bugs found while investigating dashboard totals that did not move after deleting a payment. Each item is the observed behavior, the cause, and what changed.

## Payment add / delete vs dashboard totals

### Bug 1 — Deleting a payment left dashboard balances unchanged (reported)

**What happened:** Recording an inbound payment and reconciling it correctly lowered outstanding / raised recovered on the Dashboard. Deleting that payment because it was added by mistake removed the row from the Payments ledger, but Dashboard KPIs and member balances stayed as if the payment were still applied.

**Cause:** Dashboard totals are not summed from `payments.amount`. They come from `PaymentApplication` (how much of each payment was applied to an invoice). `crud.delete_payment` only deleted the `Payment` row. SQLite does not enforce foreign keys unless `PRAGMA foreign_keys` is on, and the `Payment` model had no ORM cascade, so the application rows were left behind as orphans. Reconcile did not heal this either (see bugs 2 and 3).

**Resolution:**

- `delete_payment` now clears `PaymentApplication` rows for that payment before deleting the payment.
- After delete (and after edit), the UI rebuilds FIFO applications for the affected member so remaining payments shift onto the oldest unpaid invoices.
- `Payment.applications` is a cascade `delete-orphan` relationship; the FK is documented as `ON DELETE CASCADE` for new installs.
- Balance queries inner-join `Payment` so orphaned applications cannot affect recovered/outstanding even if they still exist.
- Migration `n11` deletes existing orphan application rows on upgrade.
- Dashboard / Applications tabs listen to a `ledger_revision` counter that increments on add, edit, delete, and reconcile, so KPI cards refresh without a manual “Refresh everything”.

### Bug 2 — Reconcile ALL skipped members with no remaining inbound payments

**What happened:** After deleting a member’s only inbound payment, clicking **Reconcile ALL members** left their old applications in place. The member now had zero payments, so the reconcile loop `continue`d past them.

**Cause:** `reconcile_all_members_fifo` skipped any member whose inbound payment count was 0, assuming there was nothing to rebuild.

**Resolution:** Members with leftover application rows are still reconciled (which now wipes those rows). Members with neither payments nor applications are still skipped.

### Bug 3 — Reconcile only cleared applications for payments that still existed

**What happened:** Even reconciling a member who still had other payments would not remove applications belonging to a payment that had already been deleted. Those orphan `payment_id`s were not in the remaining-payment list, so they were never deleted, then the remaining payments were re-applied *on top of* the orphans.

**Cause:** `reconcile_member_fifo` deleted applications with `payment_id IN (remaining inbound ids)` instead of clearing every application for that member (scoped to the plan).

**Resolution:** Reconcile now clears all of that member’s applications in the plan (including orphans), then reapplies remaining inbound payments FIFO.

### Bug 4 — Editing a payment did not rebuild applications

**What happened:** Changing an inbound amount, date, member, or direction updated the `Payment` row but left the old `PaymentApplication` splits in place until someone remembered to click Reconcile. The Dashboard therefore showed the pre-edit recovered/outstanding figures. The Payments tab itself also told you to “Use Reconcile after editing payments.”

**Cause:** `_save_payment_edits_v4` called `update_payment` and committed; it never cleared or reapplied applications.

**Resolution:** Saving edits rebuilds FIFO for the previous member (if any) and the new member (if the payer changed), on the payment’s plan.

### Bug 5 — Dashboard / Applications tabs kept stale numbers after a payment change

**What happened:** Even after the ledger math was correct, switching back to Dashboard did not redraw the KPI cards or balances table unless you clicked **Refresh everything** or changed the Active plan. Gradio does not re-run another tab’s load handlers on tab switch.

**Cause:** Dashboard only refreshed on `demo.load`, role/member/plan change, or the manual refresh buttons. Payment mutations lived on a different tab with no shared signal.

**Resolution:** A `ledger_revision` `gr.State` in `app/main.py` increments after add, save, delete, and reconcile. Dashboard and Applications subscribe to it and reload totals.

### Bug 6 — Deleting an earlier payment did not re-FIFO remaining payments

**What happened:** Two inbound payments: the first covered January, the second covered February. Deleting the January payment (even if its own application rows were removed) left the February payment applied to February. January looked unpaid, which is not FIFO.

**Cause:** Delete did not rebuild remaining applications. FIFO is only correct if applications are recomputed from the remaining chronological inbound payments.

**Resolution:** Delete (and edit) call `reconcile_member_fifo` for the affected member after the payment row is gone.

## Other issues noted (not changed, or only documented)

### Bug 7 — Adding a back-dated payment stacked on leftover dues instead of re-running FIFO

**What happened:** If a member already had later payments applied, adding an earlier-dated inbound payment applied only to whatever invoices were still open. January could stay “paid” by a February payment while the new January payment sat on February.

**Cause:** `_add_payment_v4` called `auto_apply_payment_fifo` on the new payment alone, which never rewound applications already created by later payments.

**Resolution:** Adding an inbound payment now runs `reconcile_member_fifo` for that member so all of their inbound payments are reapplied in date order. The on-screen FIFO preview is built from the new payment’s resulting application rows.

## Other issues noted (not changed, or only documented)

### Optional “link to invoice” is stored but FIFO still applies oldest-first

The payment form has **Link to invoice (optional)**. That `invoice_id` is saved on the `Payment` row for reference and filtering. Auto-apply still walks allocations oldest-to-newest and does not pin the payment to the linked invoice. Treating that dropdown as “apply only to this invoice” would be a product change, not a balance bug. Left as-is.

### SQLite foreign keys are still not enforced at the connection level

`app/db/database.py` does not emit `PRAGMA foreign_keys=ON`. Enabling that globally could start failing other parent deletes that never cleaned children (members, invoices, plans). The payment path now deletes children explicitly, and n11 repairs existing orphans. A broader FK-enforcement pass is separate work.

### Owner member balance is not the outbound KPI

The owner’s row in the balances table credits their allocations as paid (`total_paid += total_due`). **Owner Outbound Paid** on the Dashboard is a separate sum of `OUTBOUND` `Payment.amount`. Deleting an outbound carrier payment therefore changes the outbound KPI but not other members’ outstanding. That is intentional.

### `get_or_create_member` matches globally by name

Creating/editing an allocation looks up members by unique name across all plans, then attaches them to the current plan. Two people with the same name in different plans cannot both exist. Pre-existing constraint from `Member.name` uniqueness; not part of this payment-delete fix.
