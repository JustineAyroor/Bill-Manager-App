# Deployment Hardening: Implementation Notes

This round finally executed the deployment work deferred at the top of this
folder ([00-index.md](00-index.md)) back when the owner chose to prioritize
functional/product work first. See
[01-deployment-and-gcp-architecture.md](01-deployment-and-gcp-architecture.md)
for the original architecture discussion this implements.

## What changed

- **Local tooling**: installed the Google Cloud CLI on the owner's laptop
  (no Homebrew available - used the direct SDK archive instead), authenticated
  with `gcloud auth login`, and verified `gcloud compute ssh` access to the
  VM (`billingmanager-jayroor`, project `project-2833c0b8-116a-4b4b-ac5`,
  zone `us-central1-f`). This became the mechanism for making every
  remaining change below directly, without a manual copy-paste runbook.
- **uv on the VM**: replaced the pip/venv flow with `uv sync --frozen`
  against the already-committed `uv.lock`, matching local dev.
- **systemd instead of `tmux`**: added
  [deploy/systemd/tmobile-bill-manager.service](../../../deploy/systemd/tmobile-bill-manager.service),
  enabled on boot, `Restart=on-failure`. The `tmux` session was retired.
- **`deploy/deploy.sh`**: a single idempotent script (pre-migration backup,
  fast-forward `git pull` of `master`, `uv sync --frozen`, run migrations,
  restart the service, poll for health) used both for manual redeploys and
  by CI.
- **Tailscale**: the VM joined the owner's existing tailnet
  (`billingmanager-jayroor`, additive - the public IP/port still works
  exactly as before for plan members).
- **Backups, two layers**:
  1. `deploy/backup_db.sh` + a daily systemd timer - a fast, local,
     file-based SQLite backup with 14-day retention, for quick rollback of a
     bad migration/edit.
  2. A GCE **boot-disk snapshot schedule** (`billingmanager-daily-backup`,
     daily, 14-day retention) - true off-VM disaster recovery, needing zero
     credentials on the VM itself.
- **GitHub Actions CI/CD**: [.github/workflows/deploy.yml](../../../.github/workflows/deploy.yml)
  deploys automatically on every push to `master` (or manual dispatch) via a
  dedicated, restricted deploy SSH key.
- **DEPLOYMENT.md**: rewritten end to end for the above.

## Decisions made this round

**Production stayed on old code (`master`), deliberately.** While setting
this up, we discovered the VM was still running a `master` branch commit
from before the multi-plan schema, auth rework, and RAG v2 bill import - all
of which live on `dev` and have never been merged. Given the real production
DB is still on the old single-plan schema, the owner chose to defer that
promotion to a separate, deliberate step rather than bundle a first-time
schema migration into infra work. This round only changed *how* the
(unchanged) app is run and deployed.

**`master` is the deploy branch of record, not `dev`.** CI only triggers on
`master`; `dev` is where feature work happens, promoted via a deliberate
merge when a round is tested and ready. This mirrors the "test before
commit" pattern already used throughout this project's history.

**No service-account key on the VM for backups.** The original plan called
for a GCS bucket + a service account key for `gsutil cp` pushes from the VM.
Two things blocked that during implementation:

1. This GCP project has an org policy, `constraints/iam.disableServiceAccountKeyCreation`,
   that blocks minting any new service-account key.
2. The VM's own attached service account only has `devstorage.read_only`
   scope; broadening it to allow writes requires **stopping the VM**
   (GCE only allows changing an instance's service-account scopes while it's
   terminated), which the owner did not want to do for what was meant to be
   a zero-downtime infra change.

Rather than force either of those, we pivoted to a **GCE boot-disk snapshot
schedule** - a fully-managed, credential-free, zero-downtime GCP resource
that snapshots the whole disk (DB included) daily. Arguably a better backup
than a lone `.db` file anyway, since it captures the whole VM state. The
originally-created backup GCS bucket and one-off service account were torn
back down since they ended up unused.

**Deploy key is scoped as tightly as SSH allows.** The GitHub Actions deploy
key's `authorized_keys` entry uses a forced `command=".../deploy/deploy.sh"`
plus `no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty` - even
a leaked key can only ever run that one script on that one VM, nothing else.

## Incident: a secret briefly hit the systemd journal

Early in the systemd migration, the unit file used
`EnvironmentFile=.env` to pass secrets through to the process. The `.env`
file uses `export KEY=value` syntax (per the original `DEPLOYMENT.md`
example), which systemd's `EnvironmentFile` parser does not understand - it
logged each line as an "invalid environment assignment" to the journal,
**including the raw `OPENROUTER_API_KEY` value**. The app itself was never
affected (it loads `.env` directly via `python-dotenv`, independent of
systemd), but the fix was to drop `EnvironmentFile=` from the unit entirely
- it was redundant given `python-dotenv` already handles this. The journal
was rotated and vacuumed afterward to remove the leaked value, and the key
should still be rotated by the owner as a precaution (it was also visible in
the agent's own debugging output during that session).

**Takeaway for future `.env`-consuming tooling on this VM:** never point a
new tool at `.env` via a raw `EnvironmentFile=`/shell-sourcing mechanism
without first checking it tolerates (or stripping) the `export ` prefix -
or better, keep relying on `python-dotenv` inside the app and don't give any
new tool direct filesystem access to `.env` unless it must have it.

## Observed operational quirk (not a bug)

The `e2-micro`'s baseline vCPU is heavily throttled. A cold start of this
app (Gradio + pandas + matplotlib + plotly + SQLAlchemy + Twilio + OpenAI
imports) can take **1-3 minutes** before it answers HTTP requests, even with
warm bytecode/font caches, purely from CPU throttling on such a small
instance. `deploy.sh`'s health-check loop budgets for up to 5 minutes
accordingly. This is worth knowing before assuming a "stuck" deploy is
actually broken.

## Incident: CI/CD deploys silently broken since day one (missing execute bit)

**Symptom:** every push to `master` after the initial hand-run deploy showed up as a **failed** run on the GitHub Actions tab, consistently in ~15-30 seconds - far too fast to be a real deploy (which takes 1-3 minutes just for the app's cold start). `gh run view --log` on the failing run showed:

```text
bash: line 1: /home/***/apps/Bill-Manager-App/deploy/deploy.sh: Permission denied
2026/07/07 02:51:15 Process exited with status 126
```

**Root cause:** `deploy/deploy.sh` and `deploy/backup_db.sh` were committed to git with mode `100644` (non-executable) instead of `100755`. Nobody noticed locally because the file is normally invoked as `bash deploy/deploy.sh` (which doesn't care about the execute bit) - but the GitHub Actions deploy key's forced SSH command invokes the file path directly (`command="/home/.../deploy/deploy.sh"`, see the "GitHub Actions CI/CD" section above), which *does* require it. Every automated deploy since CI/CD was set up failed at the very first line, before `deploy.sh` ever ran `git pull` - meaning the VM's checkout had been silently stuck on an old commit for several pushes (three, by the time this was caught), even though `git push` itself always succeeded and looked fine from the laptop side.

**Why the very first deploy worked:** it was run manually (`bash ~/apps/Bill-Manager-App/deploy/deploy.sh` over SSH), which sidesteps the execute-bit requirement entirely - masking the bug until the *automated* path was actually exercised end-to-end.

**Fix:**

1. `chmod +x deploy/deploy.sh deploy/backup_db.sh` locally, then `git add` + commit - git tracks the executable bit as part of the tree entry, so this is a real, durable fix once committed (mode `100644` -> `100755`), not just a local workaround.
2. Because the *currently checked-out* files on the VM needed the bit fixed immediately (the next Action run would otherwise still fail before it could even `git pull` the fix), `chmod +x` was also run directly on the VM as an immediate unblock.
3. That direct VM-side `chmod` then caused a *second*, different failure on the next run: `git pull --ff-only` refused to proceed because the working tree had a local mode-only diff relative to the last commit ("local changes... would be overwritten by merge"). Fixed with `git checkout -- deploy/deploy.sh deploy/backup_db.sh` (discard the local-only mode diff) immediately followed by `git pull --ff-only origin master`, which then landed the commit that already carries the correct mode - so the working tree and the index agree again, and future pulls stay clean.
4. Ran `deploy.sh` once more by hand to fully complete the interrupted deploy (dependency sync, migrations, restart, health check), since the Action itself had aborted before reaching those steps on every prior attempt.

**Takeaway:** when a forced SSH command invokes a script by path (not via an interpreter), the script's execute bit is part of its behavior contract - verify `git ls-files -s <script>` shows `100755` for anything a deploy key or cron/systemd unit will exec directly, and prefer testing the *automated* trigger at least once (not just a manual run) before considering a CI/CD pipeline verified.

## Deferred / explicitly not done this round

- Promoting `dev` to `master` and running the accumulated migrations against
  the real production DB (multi-plan schema, RAG v2 bill import, etc.) - a
  separate, deliberate step the owner will trigger when ready.
- Nginx reverse proxy / HTTPS - no domain yet; the static IP + port 7860
  pattern continues.
- Routing the GitHub Actions deploy connection through Tailscale instead of
  the public IP - the public SSH port is still open and used for the
  restricted deploy key; revisit only if the owner wants to close it
  entirely later.
