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

## Incident: guest-OS wedge from disk pressure, then a self-inflicted second wedge during cleanup

**Symptom (reported by the owner):** "I can't connect to my VM directly from gcloud... the application is down but the VM is still up." `gcloud compute instances describe` confirmed status `RUNNING`, but *both* SSH (port 22) and the app (port 7860) timed out identically - no connection refused, no response at all, just a hang until timeout.

### Diagnosis: reading the serial console instead of guessing

Both ports failing the same way pointed away from a firewall/network-config problem (and indeed, `gcloud compute firewall-rules list` showed `allow-gradio-7860` and `default-allow-ssh` both present, enabled, `0.0.0.0/0` - untouched) and toward the VM's own network stack being unresponsive. When *nothing* over the network works but the instance API still reports `RUNNING`, the only way in is the **serial console** - it's exposed by the hypervisor directly, independent of whatever state the guest OS's network stack is in:

```bash
gcloud compute instances get-serial-port-output billingmanager-jayroor --zone=us-central1-f --port=1
```

This is a genuinely useful trick worth remembering: it requires no SSH, no working network inside the guest, and no prior setup - it just works as long as the instance is running at all. The tail of the log told the whole story:

```text
[3063481.860552] systemd[1]: systemd-journald.service: Watchdog timeout (limit 3min)!
[3063958.287614] systemd[1]: Failed to start systemd-journald.service - Journal Service.
[3064320.486850] systemd[1]: Failed to start systemd-journald.service - Journal Service.
   ... (repeated hundreds of times over multiple hours) ...
[3087976.569434] systemctl[765993]: Failed to retrieve unit state: Transport endpoint is not connected
[3088318.341807] systemctl[765997]: Failed to get load state of NetworkManager.service: Connection timed out
```

**Root cause:** `journald` (the systemd logging service) hit its internal watchdog timeout and could not restart - repeatedly, for hours. Because `journald` is a core, socket-activated systemd component, its failure cascaded into `systemd` itself becoming unable to answer *any* control request (`Transport endpoint is not connected`), which in turn meant nothing else - including the network stack - could be managed or restarted either. The instance was technically "running" at the hypervisor level the whole time, but the guest OS inside it was completely wedged. The most likely trigger: the VM had been up **38 days without a reboot**, and disk usage had already been flagged at ~83% back in July (see the dev-to-master promotion doc) - on an `e2-micro` with a single small boot disk, sustained disk pressure combined with `journald`'s own disk-backed buffering is a well-known way to trigger exactly this failure mode.

### Fix #1: a hard reset

```bash
gcloud compute instances reset billingmanager-jayroor --zone=us-central1-f
```

This power-cycles the VM at the hypervisor level - equivalent to holding a physical power button, and critically, **it does not touch the boot disk**, so no data risk. Within ~80 seconds of boot, `systemd` (now working again) auto-started `tmobile-bill-manager.service` on its own - exactly the payoff of having moved off `tmux` and onto `systemd` with `enabled` + `Restart=on-failure` back in the original hardening round. The app answered `HTTP 200` shortly after.

### The mistake: fixing disk pressure caused a second outage

With the app back up, the obvious next question was "why was disk so full, and will this happen again?" A directory-size sweep (`sudo du -h -d 2 /`) found the single largest offender immediately:

```text
3.6G  /snap
2.8G  /snap/google-cloud-cli
```

The `gcloud` CLI had been installed on the VM itself as a snap package at some point - and it's completely unused there (all deploys, backups, and diagnostics run *from the owner's laptop*, never from the VM). Confirmed unused (`grep -rl gcloud deploy/ app/` found nothing) and removed:

```bash
sudo snap remove google-cloud-cli
```

That alone freed disk usage from 81% down to 78% safely. But immediately afterward, in the same session, three more cleanup commands were run in quick succession: `sudo apt-get clean`, `sudo apt-get -y autoremove --purge` (to clear old kernel packages), and letting the pending `fstrim` run. On an `e2-micro` (1 shared vCPU, **~1GB RAM total**), stacking that much simultaneous disk/CPU/IO work - on top of the app itself already running - was enough to push the box back into trouble: `snapd` crashed outright (`SIGABRT`), systemd's watchdog killed and restarted it, `systemd-resolved` logged repeated "under memory pressure, flushing caches," and the VM went unreachable a second time - a self-inflicted repeat of the exact same class of failure, this time from memory pressure rather than disk pressure.

**Fix #2:** the same hard reset as before. App confirmed healthy again afterward, and disk usage had in fact improved for real this time - **69% used, 2.7G free** (the `fstrim` and package cleanup had actually completed; they just couldn't do so gracefully while contending with everything else live).

### Prevention: a swap file, chosen deliberately over a bigger instance

Two options were on the table: resize to a larger machine type (`e2-small`, 2GB RAM) for more headroom, or add a swap file to cushion memory spikes on the existing `e2-micro` for zero extra cost. The owner chose the swap file - free, no resize/reboot required, and sufficient as a safety margin rather than routine capacity:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # persist across reboots
sudo sysctl -w vm.swappiness=10                              # only swap under real pressure, not routinely
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

`vm.swappiness=10` (default is 60) deliberately keeps the kernel from swapping proactively - this is meant purely as an emergency cushion for a memory spike, not as a substitute for having enough RAM for normal operation. Net result versus where the incident started: disk at 80% (the swap file itself used back some of the reclaimed space) but now backed by 1GB of swap that didn't exist before, and the unnecessary 2.8GB `gcloud` snap gone for good.

### Takeaways

1. **The serial console is the right first diagnostic step whenever a GCE VM is `RUNNING` but totally unreachable on the network.** It requires no working SSH and no prior setup, and it will show you *why* in the guest's own words, rather than guessing between firewall/network/guest-OS causes from the outside.
2. **A VM that's `RUNNING` at the API level can still have a fully wedged guest OS.** `journald`'s watchdog timeout cascading into "systemd can't do anything, including networking" is a real and apparently reproducible failure mode under sustained disk pressure - not something we'd previously seen documented for this project.
3. **On a resource-constrained instance (`e2-micro`, ~1GB RAM), maintenance operations that are individually safe are not necessarily safe *together*.** `snap remove`, `apt-get autoremove --purge`, and `fstrim` are all routine, low-risk operations on a normal machine; stacked concurrently on a 1GB-RAM box that's also serving live traffic, they pushed the same class of failure right back. The fix going forward: run maintenance operations **one at a time**, watching health in between, on a machine this small - or accept a short planned restart window instead of trying to stay zero-downtime through heavy cleanup.
4. **A swap file is a legitimate, free, zero-downtime way to add a memory safety margin to an already-provisioned instance** - it's not a substitute for right-sizing if the box is *routinely* short on memory, but it's a sensible first response to an occasional spike, especially paired with a low `vm.swappiness` so it doesn't become a routine performance crutch.

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
