#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/kp/workspace/projects/telegram_control}"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "${PROJECT_DIR}/.env"
  set +a
fi

REMOTE_USER="${REMOTE_USER:-rpi}"
REMOTE_HOST="${REMOTE_HOST:-192.168.100.222}"
REMOTE_ROOT="${REMOTE_ROOT:-/home/rpi/backups/telegram_control}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/rpi_bkp_usr}"
BACKUP_PROMETHEUS_METRICS_ENABLED="${BACKUP_PROMETHEUS_METRICS_ENABLED:-false}"
BACKUP_PROMETHEUS_METRICS_FILE="${BACKUP_PROMETHEUS_METRICS_FILE:-${PROJECT_DIR}/data/metrics/telegram_control_backup.prom}"
BACKUP_PROMETHEUS_METRICS_PUSH_URL="${BACKUP_PROMETHEUS_METRICS_PUSH_URL:-}"
BACKUP_PROMETHEUS_METRICS_USERNAME="${BACKUP_PROMETHEUS_METRICS_USERNAME:-}"
BACKUP_PROMETHEUS_METRICS_PASSWORD="${BACKUP_PROMETHEUS_METRICS_PASSWORD:-}"
BACKUP_PROMETHEUS_METRICS_JOB="${BACKUP_PROMETHEUS_METRICS_JOB:-telegram_control_backup}"
BACKUP_PROMETHEUS_METRICS_INSTANCE="${BACKUP_PROMETHEUS_METRICS_INSTANCE:-$(hostname)}"
BACKUP_PROMETHEUS_METRICS_TIMEOUT_SECONDS="${BACKUP_PROMETHEUS_METRICS_TIMEOUT_SECONDS:-10}"

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes)
RSYNC_SSH="ssh -i $SSH_KEY -o BatchMode=yes"
STARTED_AT="$(date +%s)"

prometheus_escape_label() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//$'\n'/\\n}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

emit_backup_metrics() {
  local exit_code="$1"
  local previous_errexit=0
  case "$-" in
    *e*) previous_errexit=1 ;;
  esac
  set +e

  if [[ "${BACKUP_PROMETHEUS_METRICS_ENABLED}" != "true" && "${BACKUP_PROMETHEUS_METRICS_ENABLED}" != "1" ]]; then
    [[ "$previous_errexit" -eq 1 ]] && set -e
    return
  fi

  local now duration status success failure labels output_dir
  now="$(date +%s)"
  duration="$((now - STARTED_AT))"
  status="failure"
  success=0
  failure=1
  if [[ "$exit_code" -eq 0 ]]; then
    status="success"
    success=1
    failure=0
  fi

  labels="{instance=\"$(prometheus_escape_label "${BACKUP_PROMETHEUS_METRICS_INSTANCE}")\",job=\"$(prometheus_escape_label "${BACKUP_PROMETHEUS_METRICS_JOB}")\",remote_host=\"$(prometheus_escape_label "${REMOTE_HOST}")\",status=\"${status}\"}"
  output_dir="$(dirname "${BACKUP_PROMETHEUS_METRICS_FILE}")"
  mkdir -p "$output_dir"
  cat > "${BACKUP_PROMETHEUS_METRICS_FILE}" <<EOF
# HELP telegram_control_backup_last_run_timestamp_seconds Unix timestamp of the latest backup run.
# TYPE telegram_control_backup_last_run_timestamp_seconds gauge
telegram_control_backup_last_run_timestamp_seconds${labels} ${now}
# HELP telegram_control_backup_last_success_timestamp_seconds Unix timestamp of the latest successful backup run.
# TYPE telegram_control_backup_last_success_timestamp_seconds gauge
telegram_control_backup_last_success_timestamp_seconds${labels} $([[ "$success" -eq 1 ]] && printf '%s' "$now" || printf '0')
# HELP telegram_control_backup_last_failure_timestamp_seconds Unix timestamp of the latest failed backup run.
# TYPE telegram_control_backup_last_failure_timestamp_seconds gauge
telegram_control_backup_last_failure_timestamp_seconds${labels} $([[ "$failure" -eq 1 ]] && printf '%s' "$now" || printf '0')
# HELP telegram_control_backup_last_duration_seconds Duration of the latest backup run.
# TYPE telegram_control_backup_last_duration_seconds gauge
telegram_control_backup_last_duration_seconds${labels} ${duration}
# HELP telegram_control_backup_last_exit_code Exit code of the latest backup run.
# TYPE telegram_control_backup_last_exit_code gauge
telegram_control_backup_last_exit_code${labels} ${exit_code}
EOF

  if [[ -n "${BACKUP_PROMETHEUS_METRICS_PUSH_URL}" ]]; then
    local auth_args=()
    if [[ -n "${BACKUP_PROMETHEUS_METRICS_USERNAME}" || -n "${BACKUP_PROMETHEUS_METRICS_PASSWORD}" ]]; then
      auth_args=(-u "${BACKUP_PROMETHEUS_METRICS_USERNAME}:${BACKUP_PROMETHEUS_METRICS_PASSWORD}")
    fi
    curl -fsS \
      --max-time "${BACKUP_PROMETHEUS_METRICS_TIMEOUT_SECONDS}" \
      -X PUT \
      -H "Content-Type: text/plain; version=0.0.4; charset=utf-8" \
      --data-binary "@${BACKUP_PROMETHEUS_METRICS_FILE}" \
      "${auth_args[@]}" \
      "${BACKUP_PROMETHEUS_METRICS_PUSH_URL}" >/dev/null || \
      echo "[$(date --iso-8601=seconds)] Prometheus backup metrics push failed" >&2
  fi

  [[ "$previous_errexit" -eq 1 ]] && set -e
}

on_exit() {
  local exit_code="$?"
  emit_backup_metrics "$exit_code"
}

trap on_exit EXIT

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
