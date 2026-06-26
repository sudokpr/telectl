#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/kp/workspace/projects/telegram_control}"
REMOTE_USER="${REMOTE_USER:-rpi}"
REMOTE_HOST="${REMOTE_HOST:-192.168.100.222}"
REMOTE_ROOT="${REMOTE_ROOT:-/home/rpi/backups/telegram_control}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rpi_bkp_usr}"

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes)
RSYNC_SSH="ssh -i $SSH_KEY -o BatchMode=yes"

echo "[$(date --iso-8601=seconds)] Starting Telegram Control backup to ${REMOTE}:${REMOTE_ROOT}"

ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '${REMOTE_ROOT}/data' && chmod 700 '${REMOTE_ROOT}'"

rsync -az --itemize-changes \
  -e "$RSYNC_SSH" \
  "${PROJECT_DIR}/.env" \
  "${REMOTE}:${REMOTE_ROOT}/.env"

rsync -az --delete --itemize-changes \
  -e "$RSYNC_SSH" \
  "${PROJECT_DIR}/data/" \
  "${REMOTE}:${REMOTE_ROOT}/data/"

ssh "${SSH_OPTS[@]}" "$REMOTE" "chmod 600 '${REMOTE_ROOT}/.env' 2>/dev/null || true"

echo "[$(date --iso-8601=seconds)] Backup complete"
