#!/usr/bin/env bash
# Redeploy script - run on the VM itself (invoked manually, or via the
# GitHub Actions workflow over SSH). Idempotent: safe to re-run even if a
# previous run failed partway through.
#
# Order matters:
#   1. back up the DB *before* touching anything, so a bad migration is
#      always a one-file rollback away (deploy/backup_db.sh)
#   2. pull the target branch (fast-forward only - never rebases/merges
#      here, so a dirty working tree fails loudly instead of clobbering
#      anything)
#   3. uv sync to match the committed lockfile exactly
#   4. run Alembic migrations (create_db.py is idempotent: create_all()
#      only adds whole missing tables, then `alembic upgrade head`)
#   5. restart the systemd service
#   6. health-check localhost:7860 and fail loudly if it doesn't come up
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="tmobile-bill-manager"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-master}"
HEALTH_URL="http://localhost:7860"
# The e2-micro's throttled/burstable CPU means a cold Gradio + pandas +
# matplotlib + plotly import can genuinely take 1-3 minutes before the app
# responds, even with warm bytecode/font caches - this is not a bug, just
# how slow this machine's baseline vCPU is. Budget generously (~5 minutes)
# rather than false-alarming on a still-starting-up process.
HEALTH_RETRIES=60
HEALTH_SLEEP_SECS=5

cd "${REPO_DIR}"

echo "==> [1/6] Pre-deploy DB backup"
bash "${REPO_DIR}/deploy/backup_db.sh"

echo "==> [2/6] git pull --ff-only origin ${DEPLOY_BRANCH}"
git fetch origin "${DEPLOY_BRANCH}"
git checkout "${DEPLOY_BRANCH}"
git merge --ff-only "origin/${DEPLOY_BRANCH}"

echo "==> [3/6] uv sync --frozen"
export PATH="${HOME}/.local/bin:${PATH}"
uv sync --frozen

echo "==> [4/6] Running migrations"
uv run python create_db.py

echo "==> [5/6] Restarting ${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo "==> [6/6] Health check"
ok=false
for i in $(seq 1 "${HEALTH_RETRIES}"); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${HEALTH_URL}" || true)"
  if [ "${code}" = "200" ]; then
    ok=true
    break
  fi
  echo "    attempt ${i}/${HEALTH_RETRIES}: got HTTP ${code:-none}, retrying in ${HEALTH_SLEEP_SECS}s..."
  sleep "${HEALTH_SLEEP_SECS}"
done

if [ "${ok}" != "true" ]; then
  echo "!! Deploy finished but the app never returned HTTP 200 on ${HEALTH_URL}."
  echo "!! Check: sudo journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
  exit 1
fi

echo "==> Deploy complete, ${SERVICE_NAME} is healthy."
