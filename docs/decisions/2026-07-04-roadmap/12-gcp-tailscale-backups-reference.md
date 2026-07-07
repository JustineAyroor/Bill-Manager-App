# GCP, Tailscale, and Backups: Complete Reference

A durable, self-contained reference for the infrastructure this app runs on - written so you can reproduce, recover, or explain any part of it without needing AI help. Each section explains *why* the choice was made, not just the commands.

For the app-level deploy flow (systemd, `uv`, `deploy.sh`, CI/CD), see [`DEPLOYMENT.md`](../../../DEPLOYMENT.md). This doc is specifically about the surrounding infrastructure: the GCP project/VM/network itself, the private Tailscale access path, and the backup/restore story.

## 1. GCP setup

### What's actually running today (verified against the live project)

| Resource | Value |
|---|---|
| Project ID | `project-2833c0b8-116a-4b4b-ac5` |
| VM name | `billingmanager-jayroor` |
| Zone | `us-central1-f` |
| Machine type | `e2-micro` (free-tier eligible) |
| Boot image | Ubuntu 25.10 minimal |
| Static external IP | `34.42.180.111` (reserved as `billingmanager-ip`) |
| Firewall rule | `allow-gradio-7860` (ingress, `0.0.0.0/0`, `tcp:7860`) |

### Why a single `e2-micro` VM instead of Cloud Run / managed Postgres / GKE

Given the stated constraints - no expected high load, personal use and learning only - a single low-cost VM is the right shape: it's free-tier eligible, matches the existing SQLite storage model with no forced migration, and keeps the operational surface area (and the skills learned building it - systemd, Tailscale, GitHub Actions) proportional to the actual need. See [01-deployment-and-gcp-architecture.md](01-deployment-and-gcp-architecture.md) for the full comparison table.

**Note:** that original architecture doc proposed a GCS-bucket backup approach and a `tailscale/github-action`-based CI runner. Neither was what actually got built - see the Backups and Tailscale sections below, and [10-deployment-implementation.md](10-deployment-implementation.md), for what's actually live and why the pivot happened (a GCP org policy blocks service-account key creation, and the VM's own service account scopes are read-only for storage).

### Recreating this infrastructure from scratch

If this VM were ever lost entirely (not just its disk - see the Backups section for that recovery path), here's the sequence to rebuild the surrounding GCP resources:

```bash
# 1. Reserve a static external IP up front, so it doesn't change if the VM is recreated
gcloud compute addresses create billingmanager-ip --region=us-central1

# 2. Create the VM, attaching that static IP
gcloud compute instances create billingmanager-jayroor \
  --zone=us-central1-f \
  --machine-type=e2-micro \
  --image-family=ubuntu-2510-minimal \
  --image-project=ubuntu-os-cloud \
  --address=billingmanager-ip \
  --tags=http-server,https-server

# 3. Open the app's port (the default firewall rules already cover SSH/ICMP/internal traffic)
gcloud compute firewall-rules create allow-gradio-7860 \
  --network=default \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:7860 \
  --source-ranges=0.0.0.0/0
```

Then follow the VM-side setup in [`DEPLOYMENT.md`](../../../DEPLOYMENT.md) (install `uv`, clone the repo, install the systemd units, install Tailscale per section 2 below).

### Useful ongoing commands

```bash
# SSH in
gcloud compute ssh billingmanager-jayroor --zone=us-central1-f

# Check disk/memory - both are tight on an e2-micro, worth checking periodically
gcloud compute ssh billingmanager-jayroor --zone=us-central1-f --command="df -h / && free -h"

# Resize the boot disk live if it ever gets too tight (no reboot needed)
gcloud compute disks resize billingmanager-jayroor --zone=us-central1-f --size=15GB
# then on the VM: sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1
```

## 2. Tailscale (private access)

### Why Tailscale at all

The public IP (`34.42.180.111:7860`) already works for anyone, so Tailscale isn't required - it's an **additive** convenience: a private, zero-config path to the VM (both the app and SSH) that:

- doesn't depend on the public firewall rule staying open
- works from any device on any network without port-forwarding or a VPN server to run yourself
- gives a stable hostname (`billingmanager-jayroor.tailef4e41.ts.net`) instead of remembering an IP

Nothing in the app or deploy pipeline depends on Tailscale - if it's ever broken, the public IP is completely unaffected.

### How it's set up

```bash
# on the VM, once
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --ssh --hostname=billingmanager-jayroor
# open the printed https://login.tailscale.com/a/... URL and log into your account
```

To add another device (laptop, phone): install the Tailscale app, log into the same account. No server-side config needed - it just shows up in the tailnet.

### Verifying it's working

```bash
tailscale status                          # peer should show up, not "offline"
tailscale ping 100.92.244.94              # replace with the VM's actual tailnet IP
curl http://100.92.244.94:7860            # should return the app's HTML
```

### Troubleshooting: "Connected" but nothing actually loads

**This exact gotcha was hit and debugged live during this project.** Symptom: the Tailscale menu bar app says "Connected," `tailscale status` shows the peer, but a browser tab to the tailnet IP just spins forever, and even `curl`/`tailscale ping` from the same machine time out with no response.

**Root cause:** a corporate or public-network VPN client was active on the laptop. Tailscale's "Connected" status only reflects its **control-channel** to Tailscale's coordination servers (ordinary HTTPS, which most VPNs don't interfere with). Actual peer-to-peer traffic needs the OS routing table to send packets for Tailscale's private range (`100.64.0.0/10`, the "CGNAT" range) through Tailscale's own network interface. A corporate VPN typically installs its own "route everything" tunnel that competes for that same routing priority - so packets meant for Tailscale get silently swallowed by the corporate tunnel instead, which has no idea what to do with a `100.x` address, and they just vanish. No error, no timeout message - just an infinite spinner.

**Fix:** disconnect the conflicting VPN (or ask about split-tunneling for the `100.64.0.0/10` range, if the corporate VPN client supports it - rarely worth the effort for a personal project). Confirmed: after disconnecting the VPN, `tailscale ping` and the app both worked immediately.

**How to recognize it fast next time:** "Connected" in the Tailscale UI + `tailscale ping`/`curl` timing out (not erroring, just hanging) + you're on an unfamiliar network or have a VPN client running = check the VPN first, before assuming Tailscale itself is broken.

## 3. Backups

Two independent, complementary layers:

### Layer 1 - Local rotating SQLite backups

[`deploy/backup_db.sh`](../../../deploy/backup_db.sh) does a hot (non-blocking) SQLite backup via Python's `sqlite3` module, gzips it, and prunes anything older than 14 days. Runs automatically:
- Daily via `tmobile-bill-manager-backup.timer` (08:30 UTC + up to 10 minutes of random delay)
- Before every `deploy.sh` run (belt-and-suspenders before a migration)

This is for a **fast, single-file rollback** (e.g. "that last migration did something wrong, give me the file from an hour ago") - not disaster recovery, since it lives on the same disk as everything else.

### Layer 2 - GCE boot-disk snapshot schedule (the real disaster recovery)

A GCP-managed resource policy (`billingmanager-daily-backup`) takes a full incremental snapshot of the VM's entire boot disk daily. This survives a lost or corrupted VM/disk entirely, needs **no credentials stored on the VM**, and was chosen specifically because this project's GCP org policy blocks minting new service-account keys, ruling out a straightforward GCS-bucket-from-the-VM approach.

Actual live configuration (verified via `gcloud compute resource-policies describe`):
- Daily snapshot, starts 09:00 UTC, up to a 4-hour execution window
- 14-day retention (`maxRetentionDays: 14`)
- Snapshots kept even if the source disk is deleted (`onSourceDiskDelete: KEEP_AUTO_SNAPSHOTS`)
- Stored in the `us` multi-region

Setup (already done, documented for reference/recreation):

```bash
gcloud compute resource-policies create snapshot-schedule billingmanager-daily-backup \
  --region=us-central1 --max-retention-days=14 --daily-schedule --start-time=09:00 \
  --storage-location=us --on-source-disk-delete=keep-auto-snapshots

gcloud compute disks add-resource-policies billingmanager-jayroor \
  --zone=us-central1-f --resource-policies=billingmanager-daily-backup
```

### Restoring from a GCE snapshot - step by step

First, find the snapshot you want:

```bash
gcloud compute snapshots list --filter="sourceDisk:billingmanager-jayroor"
```

**Option A - "just get an old copy of the database back" (safer, doesn't touch the live VM):**

```bash
# 1. Create a new disk from the snapshot
gcloud compute disks create recovery-disk-temp \
  --zone=us-central1-f --source-snapshot=SNAPSHOT_NAME

# 2. Create a small throwaway VM and attach the recovered disk as a second disk
gcloud compute instances create recovery-vm-temp \
  --zone=us-central1-f --machine-type=e2-micro \
  --disk=name=recovery-disk-temp,device-name=recovery-disk-temp

# 3. SSH into recovery-vm-temp, mount the second disk, copy out just tmobile.db
gcloud compute ssh recovery-vm-temp --zone=us-central1-f
#   sudo mkdir /mnt/recovery && sudo mount /dev/sdb1 /mnt/recovery
#   (the DB is at /mnt/recovery/home/justineayroor/apps/Bill-Manager-App/tmobile.db)

# 4. scp it back down, then delete the throwaway VM + disk
gcloud compute instances delete recovery-vm-temp --zone=us-central1-f
gcloud compute disks delete recovery-disk-temp --zone=us-central1-f
```

**Option B - "the whole VM/disk is gone, rebuild it from the snapshot" (swaps the live disk):**

```bash
# 1. Stop the VM
gcloud compute instances stop billingmanager-jayroor --zone=us-central1-f

# 2. Detach the (broken) boot disk, create a new one from the snapshot, attach it as boot
gcloud compute instances detach-disk billingmanager-jayroor --zone=us-central1-f --disk=billingmanager-jayroor
gcloud compute disks create billingmanager-jayroor-restored \
  --zone=us-central1-f --source-snapshot=SNAPSHOT_NAME
gcloud compute instances attach-disk billingmanager-jayroor --zone=us-central1-f \
  --disk=billingmanager-jayroor-restored --boot

# 3. Start it back up
gcloud compute instances start billingmanager-jayroor --zone=us-central1-f
```

Option A is almost always the right first move if the goal is just "I need last Tuesday's data back" - it never touches the running app.

### Verifying backups are actually healthy (check this periodically)

```bash
# Local backup timer is scheduled and recent files exist
gcloud compute ssh billingmanager-jayroor --zone=us-central1-f --command="
  systemctl list-timers tmobile-bill-manager-backup.timer
  ls -la ~/backups
"

# GCE snapshot schedule is attached and actually producing snapshots
gcloud compute resource-policies describe billingmanager-daily-backup --region=us-central1
gcloud compute snapshots list --filter="sourceDisk:billingmanager-jayroor"
```

### Cost

Snapshot storage is billed per-GB/month, but GCE snapshots are **incremental** after the first one - only changed blocks are stored in each subsequent daily snapshot. For an ~8.6GB disk with a database that changes by a few KB/day, the daily incrementals are a small fraction of the full disk size - in practice, a few cents per month at 14-day retention. The first (full) snapshot is the only "expensive" one, and even that is well under $1/month at this disk size.
