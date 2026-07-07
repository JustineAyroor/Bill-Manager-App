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
