# Bill Import v2 (RAG, beta) - Manual Test Plan

How to use this doc: work through each test in order, do exactly what the steps say, then fill in the **Result** and **Notes** lines right in this file (check `[x] Pass` or `[x] Fail`, type what you actually saw under Notes - screenshots/copy-pasted numbers are great if something looks off). Send the filled-in doc back instead of re-typing everything in chat - it's faster for both of us and nothing gets lost.

Current state: after a real (not synthetic) end-to-end pass through round 4 surfaced the round-5 fixes below, the test `Feb 2026` invoice/allocations/job created during that pass were deleted, the two now-orphaned `eval/history.jsonl` rows for that job were dropped, and `data/vectorstore/` was wiped and rebuilt from the remaining real `Invoice`/`Allocation` rows - so every test below starts from a clean state again, with real historical bills still intact.

### What changed since the last pass (context, not steps) - round 8, friendlier split entry + hard reconciliation

- **New: Section B's "Amounts by person" table now has a second editable column, "Percent"** (% of the bill total), alongside the existing "Amount" ($) column - edit either one for a row and the other recalculates automatically for that row (e.g. typing `60` into Percent sets Amount to 60% of the bill total in dollars, and vice versa).
- **New: a "⚖️ Equal split" button** above the table resets everyone currently shown to an even share of the bill total, with the plan owner absorbing any few-cent rounding remainder so it always sums exactly.
- **Changed: Approve is now hard-blocked, not just warned, if the amounts don't add up to the bill total.** Previously the reconciliation banner would just show a ⚠️ warning but still let you click Approve anyway; now `_v2_approve` itself refuses (with a clear error naming the exact gap) until the numbers actually reconcile.
- **Changed: an already-approved job (whether it started as `NORMAL` or `EVALUATE_ONLY`) is now permanently read-only.** Reopening it via "Load into Review," the auto-polling view, or the Inspector all show the same read-only "predicted vs. current invoice" comparison instead of a re-editable Section B + Approve button - further corrections only ever happen from Payments → Invoices now. `eval/run_eval.py` already scored approved `NORMAL` jobs the same way it scores `EVALUATE_ONLY` ones (both keyed off `job.invoice_id`, both read live invoice data), so no change was needed there.

### What changed since the last pass (context, not steps) - round 5, real-world testing feedback

A real (not synthetic) end-to-end pass - upload → approve in NORMAL mode → run `eval/run_eval.py` - surfaced one more real allocation-logic flaw plus two UX asks:

- **Changed: the shared/unassigned pool is now split equally across only the members who actually have a line on *this* bill - never across the whole roster by history.** Round 4's "distribute proportionally by history across every member with no bill line" (see Test 6 below) turned out to still charge people who simply aren't on the current bill/account at all (e.g. a roster member from a different phone plan tracked in this app for visibility only) - confirmed against a real bill where two such members received real dollars from a shared "Account" charge purely because they had *some* history on file. Section B now shows those members at **$0 by default** with a note like "not on this bill (last time: $X, <month>)" for context only - never money. The equal share instead goes only to members with a real matched line on this bill; the plan owner absorbs the case where nobody matched a line at all, or the few cents of rounding remainder.
- **Changed: the AI-assisted flow is now the default, visible flow.** The "🧪 AI-assisted import" checkbox is now **checked by default**; the old manual 5-step flow (Upload & extract → ... → Validate & approve) is still there, just collapsed under a "⚙️ Legacy manual bill import (old flow)" accordion instead of being the first thing on the page.
- **Fixed: both Approve buttons now guard against double-clicks.** Clicking Approve (either the AI flow's or the legacy flow's) immediately disables the button and shows "⏳ Approving..." until the write finishes (success or failure), then re-enables it - a second click while a write is in flight can no longer create a duplicate/racing write.

### What changed since the last pass (context, not steps) - round 4, a full review-UI rewrite

The review/approve screen and mapping tools were **rebuilt from scratch** this round based on "confusing to edit," "precedent rows are bad UX," and "can't tell which member an ID refers to" feedback, plus two real backend bugs found while investigating a reported wrong-member-mapping case. If you tested a previous round, expect the whole "Review & approve" area (and the evaluate-only comparison) to look different:

- **New layout, three sections instead of one big table + two separate mapping tools:**
  - **Section A - "What we found on this bill"** (read-only): every real bill line, by member **name** (not raw ID), with a plain badge (`🔒 saved mapping` / `🤖 AI guess` / `❓ unresolved`).
  - **Section B - "Amounts by person"** (the only editable table): **one row per plan member** (not per bill line) - pre-filled from this bill where possible, otherwise from history, otherwise the owner absorbs it. Only the **Amount** column can be edited. This is what you approve.
  - **Section C - "Unresolved charges"** (only shows up if something needs it): one row per bill charge that matched nobody, with an inline member-picker + "Link" button - saves the mapping and updates Section B immediately, no page reload needed.
  - A **live reconciliation banner** above Section B always shows "Bill total $X · Assigned $Y · ✅ fully reconciled" (or a ⚠️ warning with the exact gap) - no more doing the math yourself.
- **Fixed (Bug 1): the model double-guessing the same member for two different lines.** Previously, tightening the prompt wasn't enough on its own - a specific case slipped through where two different phone numbers both ended up mapped to the same member (one legitimately, one a bogus zero-evidence guess). There's now a code-level check that clears any LLM guess that collides with an already-claimed member, regardless of what the model itself does.
- **Fixed (Bug 2): approving could silently overwrite one of a member's two allocations instead of adding them together.** Structural fix, not a patch - since Section B has exactly one row per member now, there's no longer a way for two writes to land on the same person and clobber each other.
- **Fixed (Bug 3): the evaluate-only/eval-dashboard accuracy numbers didn't reflect what the app actually does.** They used to split shared charges equally across everyone purely for the comparison math; now they use the exact same "owner absorbs the remainder" logic the real approve button uses, so the accuracy score means "how close would this have been if approved as proposed."
- **Changed: the "[from history] ..." per-member fallback rows are gone**, replaced by Section B always including every member with a history-based amount by default (labeled "from history" right in the row, not a separate pseudo-row) - and the math behind it changed too: unassigned money is now split across members with no bill line of their own **proportionally to their history**, not "everyone gets their full last amount stacked on top," which used to silently break the total.
- **Evaluate-only mode is now explicit about being read-only** - a note directly under the comparison and in the "Load this job into Review & Approve" status message says there's nothing to approve and that a real invoice correction happens from Payments → Invoices instead.
- Everything from round 3 (phone last-4 reconciliation, Total rows, Inspector staleness fixes, "🔄 Check status now" / "⬆️ Load this job into Review & Approve" manual buttons, prompt tightening) is still in place underneath this rewrite.

## Before you start

- [ ] Confirm the app is running (`http://127.0.0.1:7860` if local) and you're logged in as the plan owner (or app `OWNER`) - the Inspector and mapping tools require `authz.can_manage_plan`.
- [ ] Have at least 2 different bill PDFs ready: one whose `(plan, year, month)` has **no** existing invoice yet (tests `NORMAL` mode), and one that matches a period you already have an approved invoice for (tests `EVALUATE_ONLY` mode). Re-uploading a PDF from a period you've already approved works for the second case.
- [ ] Keep the "🔎 Inspect a job" accordion in mind throughout - for any result that looks wrong, open it for that job and check the cleaned text / chunks sent / precedent used / exact prompt / raw LLM response before assuming it's a bug.

---

## Test 1 - First-ever upload (NORMAL mode)

**What you're checking:** the happy path works end to end, "no precedent yet" is handled gracefully, and you can actually get from upload to a saved invoice.

**Steps:**
1. Confirm the "🧪 AI-assisted import" checkbox in Bill Import is already checked (it's the default now) and the AI section is visible without needing to click anything.
2. Upload a PDF for a `(plan, year, month)` with no existing invoice.
3. Watch the status line poll from "queued/processing" to "✅ Done - new bill, review below."
4. Confirm the "Review & approve (new bill)" section appears and **stays visible** (doesn't flash and disappear):
   - **Section A ("What we found on this bill")** lists the real lines extracted, each with a member name (or "— not linked —") and a source badge.
   - **Section B ("Amounts by person")** has exactly **one row per plan member**, with the reconciliation banner above it.
   - If it doesn't appear or disappears, click **"🔄 Check status now"** first. If that doesn't help, open "🔎 Inspect a job," pick this job from the dropdown, and click **"⬆️ Load this job into Review & Approve"** - note in your results below whether the auto-view or only the manual button worked.
5. Check the reconciliation banner: does it say "✅ fully reconciled," or a "⚠️ Remaining: $X" warning? If a warning, look at which member(s) in Section B have basis "no data - defaulted to $0" and check whether that's actually correct (i.e. they genuinely have no line on this bill), and note it below.
6. If a "❓ Unresolved charges" section appears below, that means a real bill line couldn't be matched to anyone - pick the right member from the dropdown next to it and click **Link**; confirm Section B's amounts update immediately without needing to refresh anything else.
7. Edit any **Amount** value in Section B you disagree with directly (it's the only editable column), then click **"✅ Approve & create invoice (beta)"** - confirm it immediately shows "⏳ Approving..." and is disabled, then re-enables once the status line updates. Try clicking it again mid-flight if you can catch the disabled window - it should simply not register.
8. Confirm a new Invoice/Allocation set appears for that plan/period matching what you approved (check the plan's normal Payments/Invoices view), and that the sum of allocations matches the invoice total.

**Result:** [ ] Pass  [ ] Fail  [ ] Partial

**Notes / what you observed:**
>

---

## Test 2a - Evaluate-only mode against a real historical bill

**What you're checking:** the ledger is genuinely untouched, and the diff view's numbers visibly reconcile.

**Steps:**
1. Upload a PDF whose `(plan, year, month)` already has an approved invoice.
2. Confirm status ends in "✅ Done (evaluate-only - a bill for this period already exists)."
3. Confirm the accordion shows, in order: the banner text, **"What we found on this bill"** (same Section-A style as NORMAL mode), then the per-member **Actual / Proposed / Diff / Basis** table.
4. In that table, find the **"Total"** row at the bottom and confirm `Proposed` there matches the "Proposed total" shown in the banner text above.
5. Check the banner text for a note about unmatched dollars (real bill content that couldn't be matched to a member) - not a bug by itself, see Test 6 - and confirm any such money shows up folded into the plan owner's row with basis "owner absorbs remainder" in the table (not as a separate zeroed-out bucket).
6. Confirm the banner explicitly states this is **read-only** and never touches the ledger; if you click **"⬆️ Load this job into Review & Approve"** for this job from the Inspector, confirm its status message also says there's nothing to approve here.
7. Compare a few `Actual` vs `Proposed` values per member against what you know is true for that bill - note any that seem wrong beyond what's explained by unmatched/no-identifier members.
8. If a real bill charge matched nobody, confirm a **"❓ Unresolved charges"** section appears below with a way to link it - link one and confirm the diff table updates on the next poll/check-status.

**Result:** [ ] Pass  [ ] Fail  [ ] Partial

**Notes / what you observed (include specific $ amounts that looked wrong, if any):**
>

## Test 2b - Evaluate-only view doesn't flicker/disappear

**What you're checking:** specifically re-testing the polling race condition fix - the diff should appear once and stay. If it still doesn't, this test also checks that the new manual fallback buttons reliably work around it.

**Steps:**
1. Upload a PDF that will run in evaluate-only mode (same period as an existing invoice).
2. Watch the screen closely from the moment you click enqueue through to the final result - don't refresh or click anything else.
3. Confirm the diff section appears **exactly once** and then stays visible/stable - it should not appear, vanish, and reappear.
4. If it still misbehaves: click **"🔄 Check status now"** and confirm the diff reliably (re)appears immediately. If needed, also try "🔎 Inspect a job" → pick this job → **"⬆️ Load this job into Review & Approve"** and confirm that reliably shows it too.

**Result:** [ ] Pass (auto-view worked)  [ ] Partial (only the manual buttons worked)  [ ] Fail (nothing showed it)

**Notes / what you observed:**
>

---

## Test 3 - Precedent is diverse and never from the future

**What you're checking:** precedent facts cover multiple members, and never leak dates after the uploaded bill's own period.

**Steps:**
1. Pick a plan with outcome facts for **multiple** members spanning multiple periods (any plan with real invoice history qualifies).
2. Upload a bill dated well in the past relative to today (e.g. an old month).
3. Open the Inspector for that job → **Historical precedent used** section.
   - [ ] Contains facts for more than one member (not many copies of the same name).
   - [ ] Every fact's period is **before** the uploaded bill's own date.
4. Repeat with a **recent** bill and confirm precedent can now include facts right up to (but not including) the bill's own period.

**Result:** [ ] Pass  [ ] Fail  [ ] Partial

**Notes / what you observed:**
>

---

## Test 4 - Content-hash caching (repeat upload = free)

**What you're checking:** re-uploading the exact same PDF short-circuits instead of re-calling the LLM.

**Steps:**
1. Upload a PDF, let it finish (`DONE`).
2. Upload the **exact same PDF** again (same plan).
3. Confirm the response is near-instant and the status indicates a cached result.
4. In the Inspector, confirm `cache_hit_count` incremented on the original job.

**Result:** [ ] Pass  [ ] Fail

**Notes / what you observed:**
>

---

## Test 5 - Per-plan rate limit

**What you're checking:** the hourly cap (`BILL_IMPORT_MAX_JOBS_PER_HOUR_PER_PLAN`, default 10) rejects politely instead of erroring.

**Steps:**
1. Upload `N+1` **distinct** (different content) bills for the same plan within an hour, where `N` = the configured limit.
2. Confirm the `(N+1)`th upload is rejected with a friendly message, not a stack trace, and no job row is created for it.

**Result:** [ ] Pass  [ ] Fail  [ ] Skipped (not tested this round)

**Notes / what you observed:**
>

---

## Test 6 - Identifier mapping + who gets the shared/unassigned pool

**What you're checking:** the full spectrum of how a line/member can end up in Section B, that the shared pool only goes to members actually on this bill, and that no member gets double-guessed.

**Steps:**
1. Upload a NORMAL-mode bill where you know: (a) at least one phone/email has a saved `member_identifiers` row, (b) at least one does not, (c) at least one roster member won't appear on the bill at all (no line for them this month).
2. In **Section A**, confirm (a) shows badge `🔒 saved mapping` and (b) shows either `🤖 AI guess` (still just a suggestion) or `❓ unresolved` if the model wasn't confident.
3. In **Section B**, confirm every member **with a real line on this bill** (basis "from this bill") has their line amount plus an equal share of any shared/unassigned charges baked in (check the note column - it should say something like "$X.XX equal share of $Y.YY shared/unassigned").
4. Confirm (c)'s row shows **$0** with basis "no data - defaulted to $0" and, if they have prior bill history, a note like "not on this bill (last time: $X, <month>)" - this is informational only, it should **not** be added to their amount.
5. Confirm no member's Section B amount looks like it's double-counting - i.e. nobody who has a real bill line (Section A) is *also* getting an unrelated, unexplained boost. If you see two different Section-A rows pointing at the same member where only one has real evidence for it, that's the exact Bug 1 fix this covers - flag it with the job ID.
6. Confirm the reconciliation banner says "✅ fully reconciled" once you're done reviewing (i.e. the per-member amounts, including the equal shared-pool shares, add up to the bill total exactly - not more, not less, and not spread onto members from step (c)).

**Result:** [ ] Pass  [ ] Fail  [ ] Partial

**Notes / what you observed:**
>

---

## Test 7 - Linking an unresolved charge (both modes)

**What you're checking:** the "Unresolved charges" (Section C) tool, shared by NORMAL and evaluate-only modes.

**Steps:**
1. Run a job (NORMAL or evaluate-only) where at least one real bill line is unmatched but you know which member it belongs to.
2. Under "❓ Unresolved charges," pick that identifier and the correct member, click **Link**.
3. Confirm a success message appears, and that **no** Invoice/Allocation row was touched (check Payments/Invoices for that period is unchanged).
4. Confirm Section B (NORMAL mode) or the diff table (evaluate-only) updates automatically right after linking, without needing a page reload - the linked member's amount should now include that charge.
5. Upload a different bill for the same plan/member and confirm that identifier now auto-matches with badge `🔒 saved mapping` in Section A.

**Result:** [ ] Pass  [ ] Fail

**Notes / what you observed:**
>

---

## Test 8 - Inspector + Admin Eval Dashboard access control

**What you're checking:** these owner-only surfaces are actually gated, not just hidden by convention.

**Steps:**
1. Log in as a plan `MEMBER` who is **not** the plan owner and not app `OWNER`.
2. Confirm the "🔎 Inspect a job" accordion and the "AI Eval (admin)" tab are not visible/usable.
3. Log back in as the plan owner or app `OWNER` and confirm both are visible and populated again.

**Result:** [ ] Pass  [ ] Fail

**Notes / what you observed:**
>

---

## Test 9 - Eval harness end-to-end (fresh history, correct model slugs)

**What you're checking:** `uv run python eval/run_eval.py ...` runs standalone from the command line, and the admin dashboard reflects it.

**Steps:**
1. Do Tests 1 and 2a first, so there are at least 2 scoreable jobs.
2. Run: `uv run python eval/run_eval.py --models openai/gpt-4o-mini,anthropic/claude-sonnet-4.6 --limit 5` (no `PYTHONPATH=.` needed anymore).
3. Confirm it completes without a traceback and prints a `scored job=...` line per job/model pair, and `eval/history.jsonl` gets new lines.
4. Open the "AI Eval (admin)" tab and confirm the jobs table, model-comparison chart, and accuracy-over-time chart all populate (not blank).

**Result:** [ ] Pass  [ ] Fail

**Notes / what you observed:**
>

---

## Test 10 - Generic (non-T-Mobile) carrier prompt

**What you're checking:** carrier-agnostic prompting still works for a plan with a different `carrier_type`.

**Steps:**
1. Upload a bill for a plan whose `carrier_type` is not T-Mobile (or is unset/unrecognized).
2. Confirm the Inspector's "System prompt" section shows the generic prompt, not the T-Mobile one.
3. Confirm extraction still produces a sane proposal (identifiers may be email/name/account-based instead of phone).

**Result:** [ ] Pass  [ ] Fail  [ ] Skipped (not tested this round)

**Notes / what you observed:**
>

---

## Test 11 - Equal / % / manual split, and hard reconciliation on Approve

**What you're checking:** the new split-entry options in Section B calculate correctly and stay in sync with each other, and Approve genuinely can't be clicked through when the numbers don't add up.

**Steps:**
1. Get any job to the "Review & approve" screen (Test 1's steps 1-4).
2. In Section B, type a new value into the **Percent** column for one member (e.g. `60`) - confirm that row's **Amount** column immediately recalculates to 60% of the bill total, and the reconciliation banner updates to reflect the new (likely now unbalanced) assigned total.
3. Type a new value directly into that same member's **Amount** column instead - confirm the **Percent** column for that row recalculates back to match.
4. Click **"⚖️ Equal split"** - confirm every row's Amount becomes an even share of the bill total (check the math: total ÷ number of rows), Percent updates to match, and the banner reads "✅ fully reconciled."
5. Deliberately edit one Amount so the total is off by a few dollars, then click **"✅ Approve & create invoice (beta)"** - confirm it's **rejected** with a message naming the exact mismatch (not silently approved), and nothing gets written (check Payments → Invoices for that period).
6. Fix the mismatch (edit an amount, or click Equal split again) until the banner says fully reconciled, then Approve again - confirm it succeeds this time.
7. Reopen this same job afterward (Inspector → "Load this job into Review & Approve") - confirm it now shows the **read-only** comparison instead of an editable Section B, and that Approve is gone.

**Result:** [ ] Pass  [ ] Fail  [ ] Partial

**Notes / what you observed:**
>

---

## Overall accuracy notes (freeform)

Use this space for anything that doesn't fit a specific test above - e.g. "member X's amount was off by $Y and here's why I think that happened," screenshots, or a specific job ID you want investigated further.

>

**Sign-off:** once you've filled in results above, send this file back (or paste the relevant sections) and I'll dig into any Fail/Partial results using the job IDs and Inspector data rather than guessing.
