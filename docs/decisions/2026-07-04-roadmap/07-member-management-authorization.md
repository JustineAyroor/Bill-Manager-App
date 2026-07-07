# Member Management Authorization

## Problem

Phase 5 (multi-plan authorization) restricted invoices, payments, and plan membership by `can_manage_plan`, but a plan owner (a `MEMBER`-role user) could still:

- Remove **any** member from a plan they own - even people they didn't add, including members added by the application `OWNER` or by another plan.
- There was no way for a `MEMBER`-role plan owner to edit the profile (name/contact/reminder prefs) of people they added to their own plan at all - that entire "Members" screen was `OWNER`-only.

## Decisions

1. **Removing a member from a plan is OWNER-only.** No `MEMBER`, even a plan owner, can remove another person from a plan - this action is now gated by a hard role check (`authz.can_delete_plan_member`), not `can_manage_plan`, and the "Remove member from plan" control is hidden entirely for `MEMBER` users in the Plans tab.
2. **Editing a member's profile is scoped to who created them.** A new `Member.created_by_member_id` column (migration `n7`, additive) records which member added a given person (set when a `MEMBER`-role plan owner adds a brand-new person via the Plans tab's "add a brand-new member" flow). The application `OWNER` can still edit anyone; a `MEMBER` can only edit a member where `created_by_member_id` matches their own member id. Members with no recorded creator (all pre-existing/legacy data, or anyone added directly by the `OWNER`) remain editable only by the `OWNER`.

## What was built

- `alembic/versions/n7_member_created_by.py`: adds nullable `members.created_by_member_id` (self-referential FK), no backfill needed - existing members intentionally stay `NULL` (OWNER-only).
- `app/services/crud.py`: `get_or_create_member` accepts an optional `created_by_member_id`, set only at creation time.
- `app/services/authz.py`: added `can_delete_plan_member(role)` (OWNER-only) and `can_manage_member(db, role, member_id, target_member_id)` / `manageable_member_ids(db, role, member_id)` for the "who created whom" check.
- `app/ui/plans_tab.py`: `_add_member_to_plan` now stamps `created_by_member_id` when a `MEMBER` creates a brand-new person; `_remove_member_from_plan` requires `can_delete_plan_member`; the "Remove member from plan" row is hidden for non-`OWNER` roles.
- `app/ui/screens.py` (Members tab): added a new "Members you manage" section under a `MEMBER`'s own profile, listing only members they personally added (via `authz.manageable_member_ids`), with the same edit fields (name/email/phone/active/reminder prefs) as the `OWNER`'s panel, double-checked server-side with `can_manage_member` before saving.

## Verification

On scratch copies of the dev database:
- A plan-owning `MEMBER` adding a brand-new person correctly stamps `created_by_member_id`; that member (and only that member) can subsequently edit them, while another member of the same plan (not the creator) is denied, and the `OWNER` can edit anyone including pre-existing/legacy members with no recorded creator.
- Removing a member from a plan is denied for a `MEMBER` even when they own that plan, and succeeds for the `OWNER`.
- No changes to the real `tmobile.db` occurred during testing (checksummed before/after); the `n7` migration itself was applied cleanly to the real database (verified via `alembic upgrade head`) after taking a `tmobile_pre_n7_backup.db` snapshot.

## Status

**Implemented and verified (2026-07-04).**
