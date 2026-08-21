#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '[RESULT] BLOCKED_SOURCED_EXECUTION\n'
  return 1
fi

set -Eeuo pipefail

# One-time, operator-run migration from the editable production checkout to an
# immutable release. This script intentionally performs no manual bot cycle and
# no Binance mutation. Its exchange precheck uses authenticated GET requests.

readonly WORKTREE="/home/binancebot/BinanceBot"
readonly SCRIPT_REL="ops/migrate_to_immutable_runtime.sh"
readonly BASELINE_COMMIT="eac98265503f48a230a2742c7d396fb9fdd5a2d4"
readonly EXPECTED_COMMIT_SUBJECT="fix: harden systemd parsing in immutable migration"
readonly RUNTIME_ROOT="/opt/binancebot"
readonly RELEASES_ROOT="${RUNTIME_ROOT}/releases"
readonly VENVS_ROOT="${RUNTIME_ROOT}/venvs"
readonly CURRENT_LINK="${RUNTIME_ROOT}/current"
readonly CURRENT_NEW="${RUNTIME_ROOT}/current.new"
readonly SOURCE_ENV="${WORKTREE}/.env"
readonly RUNTIME_ENV="/etc/binancebot/runtime.env"
readonly SYSTEMD_ROOT="/etc/systemd/system"
readonly EXPECTED_VERSION="v1.5-preventive-futures-close-fix"
readonly EXPECTED_CAPABILITY="preventive-futures-close-confirmation-v1"
readonly MAIN_SERVICE="binancebot.service"
readonly MAIN_TIMER="binancebot.timer"
readonly GUARDIAN_SERVICE="binancebot-guardian.service"
readonly GUARDIAN_TIMER="binancebot-guardian.timer"
readonly POST_CUTOVER_CYCLES=3
readonly POST_CUTOVER_TIMEOUT_SECONDS=720

readonly -a EXPECTED_RELEASE_DIFF=(
  "ops/migrate_to_immutable_runtime.sh"
)

readonly -a REQUIRED_SERVICES=(
  "binancebot.service"
  "binancebot-guardian.service"
  "binancebot-dashboard.service"
  "binancebot-telegram.service"
)

readonly -a REQUIRED_TIMERS=(
  "binancebot.timer"
  "binancebot-guardian.timer"
)

readonly -a MUTABLE_PATHS=(
  "data"
  "trading/state.json"
  "trading/bot_state.json"
  "trading/trade_analytics.jsonl"
  "trading/decision_snapshots.jsonl"
  "trading/trades_log.txt"
  "trading/analysis_log.txt"
  "trading/.cycle_baseline.json"
  "trading/blacklist_dynamic.json"
  "trading/telegram_alert_state.json"
  "trading/telegram_offset.json"
  "trading/reports"
)

declare -a SERVICE_UNITS=()
declare -a TIMER_UNITS=()
declare -a RESIDENT_SERVICES=()
declare -a ONESHOT_SERVICES=()
declare -A INITIAL_ACTIVE=()
declare -A INITIAL_ENABLED=()

UTC_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
UTC_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_PATH=""
BACKUP_PATH=""
RELEASE_COMMIT=""
RELEASE_BUILD=""
RELEASE_FINAL=""
VENV_BUILD=""
VENV_FINAL=""
VALIDATION_ROOT=""
MUTABLE_BEFORE=""
MUTABLE_AFTER=""
PREVIOUS_CURRENT=""
CUTOVER_STARTED=0
ROLLBACK_RUNNING=0
REUSE_RELEASE=0
ROLLBACK_LOCK_DIR=""
ROLLBACK_STATE_FILE=""

timestamp() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

section() {
  printf '\n[%s] %s %s\n' "$1" "$(timestamp)" "$2"
}

info() {
  printf '%s %s\n' "$(timestamp)" "$*"
}

claim_rollback() {
  [[ -n "$ROLLBACK_LOCK_DIR" && "$ROLLBACK_LOCK_DIR" == "${TEMP_ROOT}/rollback.lock" ]] || return 1
  mkdir "$ROLLBACK_LOCK_DIR" 2>/dev/null
}

set_rollback_state() {
  local state="$1"
  [[ -n "$ROLLBACK_STATE_FILE" && "$ROLLBACK_STATE_FILE" == "${TEMP_ROOT}/rollback.state" ]] || return 1
  printf '%s\n' "$state" > "$ROLLBACK_STATE_FILE"
}

rollback_incomplete() {
  local original_rc="$1"
  shift
  set_rollback_state "INCOMPLETE:$*" || true
  printf '[RESULT] MIGRATION_ROLLBACK_INCOMPLETE %s\n' "$*"
  exit "$original_rc"
}

die() {
  local code="$1"
  shift
  printf '[RESULT] %s\n' "$code"
  printf 'ERROR=%s\n' "$*"
  if (( CUTOVER_STARTED == 1 )); then
    rollback 1
  fi
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "BLOCKED_MISSING_COMMAND" "$1"
}

unit_loaded() {
  [[ "$(systemctl show "$1" -p LoadState --value 2>/dev/null || true)" == "loaded" ]]
}

resolve_original_exec_start() {
  local service="$1"
  local fragment_path drop_in_path drop_in_paths
  local -a unit_files=() drop_ins=()
  fragment_path="$(systemctl show "$service" -p FragmentPath --value)"
  [[ -f "$fragment_path" ]] || return 1
  unit_files+=("$fragment_path")
  drop_in_paths="$(systemctl show "$service" -p DropInPaths --value)"
  if [[ -n "$drop_in_paths" ]]; then
    read -r -a drop_ins <<< "$drop_in_paths"
    for drop_in_path in "${drop_ins[@]}"; do
      [[ "$drop_in_path" == "${SYSTEMD_ROOT}/${service}.d/immutable-runtime.conf" ]] && continue
      [[ -f "$drop_in_path" ]] || return 1
      unit_files+=("$drop_in_path")
    done
  fi
  awk '/^[[:space:]]*ExecStart=/{value=substr($0,index($0,"=")+1); if(length(value)==0) count=0; else values[++count]=value} END{for(i=1; i<=count; i++) print values[i]}' "${unit_files[@]}"
}

record_unit_state() {
  local unit="$1"
  INITIAL_ACTIVE["$unit"]="$(systemctl is-active "$unit" 2>/dev/null || true)"
  INITIAL_ENABLED["$unit"]="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
}

start_timer_if_initially_active() {
  local timer="$1"
  if [[ "${INITIAL_ACTIVE[$timer]:-inactive}" == "active" ]]; then
    systemctl start "$timer"
  fi
}

start_initial_timers_in_order() {
  local timer
  start_timer_if_initially_active "$GUARDIAN_TIMER"
  for timer in "${TIMER_UNITS[@]}"; do
    [[ "$timer" == "$GUARDIAN_TIMER" || "$timer" == "$MAIN_TIMER" ]] && continue
    start_timer_if_initially_active "$timer"
  done
  start_timer_if_initially_active "$MAIN_TIMER"
}

restore_initial_runtime() {
  local service timer

  for timer in "${TIMER_UNITS[@]}"; do
    systemctl stop "$timer" >/dev/null 2>&1 || true
  done
  for service in "${RESIDENT_SERVICES[@]}"; do
    systemctl stop "$service" >/dev/null 2>&1 || true
  done
  for service in "${ONESHOT_SERVICES[@]}"; do
    local wait_deadline=$((SECONDS + 300))
    while systemctl is-active --quiet "$service" || [[ "$(systemctl show "$service" -p ActiveState --value 2>/dev/null)" == "activating" ]]; do
      if (( SECONDS >= wait_deadline )); then
        return 1
      fi
      sleep 5
    done
  done


  for service in "${SERVICE_UNITS[@]}"; do
    local override="${SYSTEMD_ROOT}/${service}.d/immutable-runtime.conf"
    local saved="${BACKUP_PATH}/drop-ins/${service}/immutable-runtime.conf"
    if [[ -f "$saved" ]]; then
      install -d -m 0755 "$(dirname "$override")"
      install -m 0644 "$saved" "$override"
    else
      rm -f -- "$override"
      rmdir --ignore-fail-on-non-empty "$(dirname "$override")" 2>/dev/null || true
    fi
  done

  if [[ -n "$PREVIOUS_CURRENT" ]]; then
    ln -sfn "$PREVIOUS_CURRENT" "${CURRENT_LINK}.rollback"
    mv -Tf "${CURRENT_LINK}.rollback" "$CURRENT_LINK"
  elif [[ -L "$CURRENT_LINK" ]]; then
    rm -f -- "$CURRENT_LINK"
  fi

  systemctl daemon-reload || return 1
  for service in "${SERVICE_UNITS[@]}"; do
    systemctl cat "$service" > "${TEMP_ROOT}/rollback-${service}.cat.txt" || return 1
    cmp -s "${BACKUP_PATH}/${service}.cat.txt" "${TEMP_ROOT}/rollback-${service}.cat.txt" || return 1
  done

  for service in "${RESIDENT_SERVICES[@]}"; do
    if [[ "${INITIAL_ACTIVE[$service]:-inactive}" == "active" ]]; then
      systemctl start "$service" || true
    fi
  done
  start_initial_timers_in_order
}

rollback() {
  local original_rc="${1:-1}"
  (( ROLLBACK_RUNNING == 0 )) || exit "$original_rc"
  claim_rollback || exit "$original_rc"
  ROLLBACK_RUNNING=1
  trap - ERR
  set +e
  set_rollback_state "STARTED" || rollback_incomplete "$original_rc" "state_file_unavailable"

  section "ROLLBACK" "Restoring the pre-migration runtime"
  if ! restore_initial_runtime; then
    rollback_incomplete "$original_rc" "restore_failed"
  fi

  local service timer
  for service in "${RESIDENT_SERVICES[@]}"; do
    if [[ "${INITIAL_ACTIVE[$service]:-inactive}" == "active" ]] && ! systemctl is-active --quiet "$service"; then
      rollback_incomplete "$original_rc" "resident=${service}"
    fi
  done
  for timer in "${TIMER_UNITS[@]}"; do
    if [[ "${INITIAL_ACTIVE[$timer]:-inactive}" == "active" ]] && ! systemctl is-active --quiet "$timer"; then
      rollback_incomplete "$original_rc" "timer=${timer}"
    fi
  done

  if [[ "${INITIAL_ACTIVE[$MAIN_TIMER]:-inactive}" == "active" ]]; then
    local deadline=$((SECONDS + 300))
    local before after result exec_status
    local restored_cycle=0
    before="$(systemctl show "$MAIN_SERVICE" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
    while (( SECONDS < deadline )); do
      sleep 5
      after="$(systemctl show "$MAIN_SERVICE" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
      if [[ -n "$after" && "$after" != "$before" ]] && ! systemctl is-active --quiet "$MAIN_SERVICE"; then
        result="$(systemctl show "$MAIN_SERVICE" -p Result --value 2>/dev/null)"
        exec_status="$(systemctl show "$MAIN_SERVICE" -p ExecMainStatus --value 2>/dev/null)"
        if [[ "$result" == "success" && "$exec_status" == "0" ]]; then
          restored_cycle=1
        fi
        break
      fi
    done
    if (( restored_cycle != 1 )); then
      rollback_incomplete "$original_rc" "natural_cycle_not_confirmed"
    fi
  fi

  set_rollback_state "COMPLETED" || rollback_incomplete "$original_rc" "state_file_unavailable"
  printf '[RESULT] MIGRATION_ROLLED_BACK\n'
  printf 'MIGRATION_LOG=%s\n' "$LOG_PATH"
  exit "$original_rc"
}

on_error() {
  local rc=$?
  local line="${BASH_LINENO[0]:-unknown}"
  printf 'ERROR_LINE=%s ERROR_RC=%s\n' "$line" "$rc"
  if (( CUTOVER_STARTED == 1 )); then
    rollback "$rc"
  fi
  printf '[RESULT] MIGRATION_FAILED_BEFORE_ACTIVATION\n'
  [[ -n "$LOG_PATH" ]] && printf 'MIGRATION_LOG=%s\n' "$LOG_PATH"
  exit "$rc"
}

trap on_error ERR


if (( EUID != 0 )); then
  printf '[RESULT] BLOCKED_NOT_ROOT\n'
  exit 1
fi

for command_name in awk basename bash chmod chown cmp date dirname find git grep install journalctl ln mkdir mv readlink rmdir runuser sed sha256sum sleep sort systemctl systemd-analyze tar tee timeout touch tr wc xargs; do
  require_command "$command_name"
done

install -d -m 0755 -o root -g root /var/log/binancebot
LOG_PATH="/var/log/binancebot/immutable-runtime-migration-${UTC_STAMP}.log"
touch "$LOG_PATH"
chmod 0640 "$LOG_PATH"
chown root:binancebot "$LOG_PATH"
exec > >(tee -a "$LOG_PATH") 2>&1

section "PRECHECK" "Root and Git baseline"
cd "$WORKTREE"

[[ "$(git branch --show-current)" == "main" ]] || die "BLOCKED_BASELINE_MISMATCH" "branch is not main"
[[ -z "$(git status --short)" ]] || die "BLOCKED_BASELINE_MISMATCH" "working tree is not clean"

RELEASE_COMMIT="$(git rev-parse HEAD)"
readonly RELEASE_COMMIT
[[ "$RELEASE_COMMIT" == "$(git rev-parse origin/main)" ]] || die "BLOCKED_BASELINE_MISMATCH" "HEAD differs from origin/main"
[[ "$(git rev-parse "${RELEASE_COMMIT}^")" == "$BASELINE_COMMIT" ]] || die "BLOCKED_BASELINE_MISMATCH" "release commit parent is not the audited baseline"
[[ "$(git show -s --format=%s "$RELEASE_COMMIT")" == "$EXPECTED_COMMIT_SUBJECT" ]] || die "BLOCKED_BASELINE_MISMATCH" "unexpected release commit subject"
[[ "$(git rev-list --count "${BASELINE_COMMIT}..${RELEASE_COMMIT}")" == "1" ]] || die "BLOCKED_BASELINE_MISMATCH" "expected exactly one migration-fix commit"
mapfile -t migration_diff < <(git diff --name-only "${BASELINE_COMMIT}..${RELEASE_COMMIT}" | LC_ALL=C sort)
mapfile -t expected_diff < <(printf '%s\n' "${EXPECTED_RELEASE_DIFF[@]}" | LC_ALL=C sort)
[[ "${#migration_diff[@]}" == "${#expected_diff[@]}" ]] || die "BLOCKED_BASELINE_MISMATCH" "unexpected release diff size"
for index in "${!expected_diff[@]}"; do
  [[ "${migration_diff[$index]}" == "${expected_diff[$index]}" ]] || die "BLOCKED_BASELINE_MISMATCH" "unexpected release file: ${migration_diff[$index]}"
  git cat-file -e "${RELEASE_COMMIT}:${expected_diff[$index]}" || die "BLOCKED_BASELINE_MISMATCH" "release file is absent: ${expected_diff[$index]}"
done
info "BRANCH=main"
info "RELEASE_COMMIT=${RELEASE_COMMIT}"
info "WORKING_TREE=CLEAN"

section "PRECHECK" "Discovering systemd units that execute the working tree"
declare -A found_services=()
while IFS= read -r unit_file; do
  [[ -n "$unit_file" ]] || continue
  unit_name="$(basename "$unit_file")"
  [[ "$unit_name" == *.service ]] || continue
  if unit_loaded "$unit_name"; then
    found_services["$unit_name"]=1
  fi
done < <(grep -rl --fixed-strings "$WORKTREE" /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system 2>/dev/null | sort -u)

mapfile -t SERVICE_UNITS < <(printf '%s\n' "${!found_services[@]}" | sed '/^$/d' | sort -u)
(( ${#SERVICE_UNITS[@]} > 0 )) || die "BLOCKED_SYSTEMD_DISCOVERY" "no services reference the working tree"

for required in "${REQUIRED_SERVICES[@]}"; do
  [[ -n "${found_services[$required]:-}" ]] || die "BLOCKED_SYSTEMD_DISCOVERY" "required service not discovered: ${required}"
done

declare -A found_timers=()
while read -r timer _; do
  [[ "$timer" == *.timer ]] || continue
  triggers="$(systemctl show "$timer" -p Triggers --value 2>/dev/null || true)"
  for service in "${SERVICE_UNITS[@]}"; do
    if [[ " $triggers " == *" $service "* ]]; then
      found_timers["$timer"]=1
    fi
  done
done < <(systemctl list-unit-files --type=timer --no-legend --no-pager)
for required in "${REQUIRED_TIMERS[@]}"; do
  unit_loaded "$required" || die "BLOCKED_SYSTEMD_DISCOVERY" "required timer is not loaded: ${required}"
  found_timers["$required"]=1
done
mapfile -t TIMER_UNITS < <(printf '%s\n' "${!found_timers[@]}" | sed '/^$/d' | sort -u)

for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
  record_unit_state "$unit"
  info "UNIT=${unit} ACTIVE=${INITIAL_ACTIVE[$unit]} ENABLED=${INITIAL_ENABLED[$unit]}"
done

for service in "${SERVICE_UNITS[@]}"; do
  service_type="$(systemctl show "$service" -p Type --value)"
  if [[ "$service_type" == "oneshot" ]]; then
    ONESHOT_SERVICES+=("$service")
  else
    RESIDENT_SERVICES+=("$service")
  fi
done

section "PRECHECK" "Environment and virtualenv reproducibility"
[[ -f "$SOURCE_ENV" && ! -L "$SOURCE_ENV" ]] || die "BLOCKED_ENV_MISSING" "$SOURCE_ENV"
readonly OLD_PYTHON="${WORKTREE}/.venv/bin/python"
[[ -x "$OLD_PYTHON" ]] || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "old venv Python is unavailable"
[[ -z "$("$OLD_PYTHON" -m pip list --editable --format=freeze)" ]] || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "editable packages detected"

readonly TEMP_ROOT="/var/tmp/binancebot-immutable-runtime-${UTC_STAMP}"
install -d -m 0700 -o root -g root "$TEMP_ROOT"
ROLLBACK_LOCK_DIR="${TEMP_ROOT}/rollback.lock"
ROLLBACK_STATE_FILE="${TEMP_ROOT}/rollback.state"
readonly ROLLBACK_LOCK_DIR ROLLBACK_STATE_FILE
readonly OLD_FREEZE="${TEMP_ROOT}/old-freeze.txt"
"$OLD_PYTHON" -m pip freeze --all | LC_ALL=C sort > "$OLD_FREEZE"
if grep -Eq '(^-e |@ file:|/home/binancebot)' "$OLD_FREEZE"; then
  die "BLOCKED_VENV_NOT_REPRODUCIBLE" "local or editable dependency detected"
fi
"$OLD_PYTHON" -m pip check
info "PYTHON_VERSION=$("$OLD_PYTHON" --version 2>&1)"
info "PACKAGE_COUNT=$(wc -l < "$OLD_FREEZE")"

section "PRECHECK" "Build-time version consistency and release metadata inputs"
"$OLD_PYTHON" "${WORKTREE}/trading/check_version_consistency.py" --strict
info "BUILD_TIME_VERSION_CONSISTENCY=PASS"

section "PRECHECK" "Fresh read-only production safety gate"
PYTHONPATH="${WORKTREE}/trading" "$OLD_PYTHON" - <<'PY'
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import binance_client

root = Path('/home/binancebot/BinanceBot')
state = json.loads((root / 'trading/state.json').read_text(encoding='utf-8-sig'))
bot_state = json.loads((root / 'trading/bot_state.json').read_text(encoding='utf-8-sig'))
local_positions = state.get('positions') or []
reconciliation = (((bot_state.get('positions') or {}).get('short') or {}).get('reconciliation') or {})

def amount(value):
    try:
        return Decimal(str(value or '0'))
    except (InvalidOperation, TypeError, ValueError):
        raise SystemExit('BLOCKED_PRE_CUTOVER invalid position amount')

client = binance_client.get_default_client()
try:
    exchange_positions = client.futures_position_risk({})
    futures_orders = client.futures_open_orders({})
    spot_orders = client.spot_signed('GET', '/api/v3/openOrders', {})
except Exception as exc:
    print('SAFETY_READ_ERROR=' + type(exc).__name__)
    raise SystemExit('BLOCKED_PRE_CUTOVER') from None
open_positions = [row for row in exchange_positions if amount(row.get('positionAmt')) != 0]

checks = {
    'local_positions': len(local_positions) == 0,
    'exchange_futures_positions': len(open_positions) == 0,
    'managed_futures': reconciliation.get('managed_count') == 0,
    'orphan_futures': reconciliation.get('orphan_count') == 0,
    'unmanaged_futures': reconciliation.get('unmanaged_count') == 0,
    'unprotected_futures': reconciliation.get('unprotected_count') == 0,
    'desynced_futures': reconciliation.get('desynced_count') == 0,
    'futures_reconciliation_aligned': reconciliation.get('aligned') is True and reconciliation.get('status') == 'ALINEADO',
    'futures_open_orders': len(futures_orders) == 0,
    'spot_open_orders': len(spot_orders) == 0,
}
for name, passed in checks.items():
    print(f'SAFETY_{name.upper()}={str(passed).lower()}')
if not all(checks.values()):
    raise SystemExit('BLOCKED_PRE_CUTOVER')
print('UNKNOWN_ORDERS=false')
print('PRE_CUTOVER_SAFETY=PASS')
PY

section "BACKUP" "Backing up systemd units and effective state"
BACKUP_PATH="/var/backups/binancebot-systemd/${UTC_STAMP}"
readonly BACKUP_PATH
install -d -m 0700 -o root -g root "$BACKUP_PATH" "${BACKUP_PATH}/units" "${BACKUP_PATH}/drop-ins"

for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do
  fragment="$(systemctl show "$unit" -p FragmentPath --value)"
  [[ -f "$fragment" ]] || die "BLOCKED_SYSTEMD_BACKUP" "missing fragment for ${unit}"
  install -m 0600 -o root -g root "$fragment" "${BACKUP_PATH}/units/${unit}"
  systemctl cat "$unit" > "${BACKUP_PATH}/${unit}.cat.txt"
  systemctl show "$unit" \
    -p Id -p FragmentPath -p DropInPaths -p Type -p ActiveState -p SubState \
    -p UnitFileState -p WorkingDirectory -p ExecStart \
    -p EnvironmentFiles -p User -p Group -p Result -p ExecMainStatus \
    -p MainPID -p Triggers > "${BACKUP_PATH}/${unit}.show.txt"
  chmod 0600 "${BACKUP_PATH}/${unit}.cat.txt" "${BACKUP_PATH}/${unit}.show.txt"
  printf '%s|%s|%s\n' "$unit" "${INITIAL_ACTIVE[$unit]}" "${INITIAL_ENABLED[$unit]}" >> "${BACKUP_PATH}/initial-state.txt"
  drop_in_paths="$(systemctl show "$unit" -p DropInPaths --value)"
  if [[ -n "$drop_in_paths" ]]; then
    install -d -m 0700 "${BACKUP_PATH}/drop-ins/${unit}/all"
    read -r -a backup_drop_ins <<< "$drop_in_paths"
    for drop_in_path in "${backup_drop_ins[@]}"; do
      [[ -f "$drop_in_path" ]] || die "BLOCKED_SYSTEMD_BACKUP" "missing drop-in for ${unit}"
      install -m 0600 "$drop_in_path" "${BACKUP_PATH}/drop-ins/${unit}/all/$(basename "$drop_in_path")"
    done
  fi
done
chmod 0600 "${BACKUP_PATH}/initial-state.txt"

for service in "${SERVICE_UNITS[@]}"; do
  override="${SYSTEMD_ROOT}/${service}.d/immutable-runtime.conf"
  if [[ -e "$override" ]]; then
    install -d -m 0700 "${BACKUP_PATH}/drop-ins/${service}"
    install -m 0600 "$override" "${BACKUP_PATH}/drop-ins/${service}/immutable-runtime.conf"
  fi
done
info "SYSTEMD_BACKUP_PATH=${BACKUP_PATH}"

section "BACKUP" "Preparing root-owned infrastructure and runtime environment"
install -d -m 0755 -o root -g root "$RUNTIME_ROOT" "$RELEASES_ROOT" "$VENVS_ROOT"
install -d -m 0750 -o root -g binancebot /etc/binancebot
if [[ -e "$RUNTIME_ENV" ]]; then
  [[ -f "$RUNTIME_ENV" && ! -L "$RUNTIME_ENV" ]] || die "BLOCKED_ENV_MISMATCH" "runtime.env is not a regular file"
  cmp -s "$SOURCE_ENV" "$RUNTIME_ENV" || die "BLOCKED_ENV_MISMATCH" "existing runtime.env differs from source"
else
  install -m 0640 -o root -g binancebot "$SOURCE_ENV" "$RUNTIME_ENV"
fi
chown root:binancebot "$RUNTIME_ENV"
chmod 0640 "$RUNTIME_ENV"
[[ "$(sha256sum "$SOURCE_ENV" | awk '{print $1}')" == "$(sha256sum "$RUNTIME_ENV" | awk '{print $1}')" ]] || die "BLOCKED_ENV_MISMATCH" "environment hashes differ"
info "RUNTIME_ENV=${RUNTIME_ENV}"
info "ENV_HASH_MATCH=true"

RELEASE_BUILD="${RELEASES_ROOT}/${RELEASE_COMMIT}.building"
RELEASE_FINAL="${RELEASES_ROOT}/${RELEASE_COMMIT}"
VENV_BUILD="${VENVS_ROOT}/${RELEASE_COMMIT}.building"
VENV_FINAL="${VENVS_ROOT}/${RELEASE_COMMIT}"
VALIDATION_ROOT="${RELEASE_BUILD}"
MUTABLE_BEFORE="${TEMP_ROOT}/mutable-before.sha256"
MUTABLE_AFTER="${TEMP_ROOT}/mutable-after.sha256"
readonly RELEASE_BUILD RELEASE_FINAL VENV_BUILD VENV_FINAL MUTABLE_BEFORE MUTABLE_AFTER
[[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "BLOCKED_RELEASE_PATH_INVALID" "commit is not a full SHA-1"
[[ "$RUNTIME_ROOT" == "/opt/binancebot" && "$RELEASES_ROOT" == "/opt/binancebot/releases" && "$VENVS_ROOT" == "/opt/binancebot/venvs" ]] || die "BLOCKED_RELEASE_PATH_INVALID" "runtime roots"
[[ "$RELEASE_BUILD" == "${RELEASES_ROOT}/${RELEASE_COMMIT}.building" && "$RELEASE_FINAL" == "${RELEASES_ROOT}/${RELEASE_COMMIT}" ]] || die "BLOCKED_RELEASE_PATH_INVALID" "release paths"
[[ "$VENV_BUILD" == "${VENVS_ROOT}/${RELEASE_COMMIT}.building" && "$VENV_FINAL" == "${VENVS_ROOT}/${RELEASE_COMMIT}" ]] || die "BLOCKED_RELEASE_PATH_INVALID" "venv paths"


[[ ! -e "$RELEASE_BUILD" ]] || die "BLOCKED_INCOMPLETE_RELEASE_EXISTS" "$RELEASE_BUILD"
[[ ! -e "$VENV_BUILD" ]] || die "BLOCKED_INCOMPLETE_VENV_EXISTS" "$VENV_BUILD"
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_CURRENT="$(readlink "$CURRENT_LINK")"
elif [[ -e "$CURRENT_LINK" ]]; then
  die "BLOCKED_CURRENT_NOT_SYMLINK" "$CURRENT_LINK"
fi

section "PAUSE" "Stopping new activations and resident services"
CUTOVER_STARTED=1

for timer in "${TIMER_UNITS[@]}"; do
  systemctl stop "$timer"
done
for timer in "${TIMER_UNITS[@]}"; do
  systemctl is-active --quiet "$timer" && die "ABORT_CUTOVER_TIMER_STILL_ACTIVE" "$timer"
done

for service in "${ONESHOT_SERVICES[@]}"; do
  deadline=$((SECONDS + 180))
  while systemctl is-active --quiet "$service" || [[ "$(systemctl show "$service" -p ActiveState --value)" == "activating" ]]; do
    (( SECONDS < deadline )) || die "ABORT_CUTOVER_ONESHOT_TIMEOUT" "$service"
    sleep 2
  done
done

for service in "${RESIDENT_SERVICES[@]}"; do
  systemctl stop "$service"
done
for service in "${SERVICE_UNITS[@]}"; do
  main_pid="$(systemctl show "$service" -p MainPID --value)"
  [[ "$main_pid" == "0" ]] || die "ABORT_CUTOVER_PROCESS_STILL_USING_WORKTREE" "${service} pid=${main_pid}"
done
for proc_dir in /proc/[0-9]*; do
  [[ -r "${proc_dir}/cmdline" ]] || continue
  proc_exe="$(readlink -f "${proc_dir}/exe" 2>/dev/null || true)"
  proc_cmdline="$(tr '\0' ' ' < "${proc_dir}/cmdline" 2>/dev/null || true)"
  if [[ "$proc_exe" == "${WORKTREE}/.venv/"* || "$proc_cmdline" == *"${WORKTREE}/scripts/run_once.sh"* || "$proc_cmdline" == *"${WORKTREE}/trading/"* || "$proc_cmdline" == *"${WORKTREE}/dashboard/"* ]]; then
    die "ABORT_CUTOVER_PROCESS_STILL_USING_WORKTREE" "pid=$(basename "$proc_dir")"
  fi
done
info "CUTOVER_PAUSE_UTC=$(timestamp)"

release_source_manifest() {
  local root="$1"
  local destination="$2"
  (
    cd "$root"
    find -P . -type f \
      ! -path './.release-commit' \
      ! -path './.release-tree.sha256' \
      ! -path './.BUILDING' \
      -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
  ) > "$destination"
}

section "RELEASE" "Materializing source from the exact Git commit"
if [[ -e "$RELEASE_FINAL" ]]; then
  [[ -f "${RELEASE_FINAL}/.release-commit" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "$RELEASE_FINAL"
  [[ "$(<"${RELEASE_FINAL}/.release-commit")" == "$RELEASE_COMMIT" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "commit marker mismatch"
  [[ -f "${RELEASE_FINAL}/.release-version-commits.json" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "version commit metadata missing"
  [[ ! -e "${RELEASE_FINAL}/.BUILDING" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "BUILDING marker present"
  VALIDATION_ROOT="$RELEASE_FINAL"
  REUSE_RELEASE=1
  info "REUSING_VALIDATED_RELEASE_CANDIDATE=${RELEASE_FINAL}"
else
  install -d -m 0755 -o root -g root "$RELEASE_BUILD"
  touch "${RELEASE_BUILD}/.BUILDING"
  git archive --format=tar "$RELEASE_COMMIT" | tar -x -C "$RELEASE_BUILD"
  printf '%s\n' "$RELEASE_COMMIT" > "${RELEASE_BUILD}/.release-commit"
  "$OLD_PYTHON" "${WORKTREE}/trading/check_version_consistency.py" \
    --emit-release-metadata "$RELEASE_COMMIT" \
    > "${RELEASE_BUILD}/.release-version-commits.json"
fi

section "RELEASE" "Creating or validating the dedicated virtualenv"
if [[ -d "$VENV_FINAL" ]]; then
  "$VENV_FINAL/bin/python" -m pip freeze --all | LC_ALL=C sort > "${TEMP_ROOT}/existing-freeze.txt"
  cmp -s "$OLD_FREEZE" "${TEMP_ROOT}/existing-freeze.txt" || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "existing release venv inventory differs"
  "$VENV_FINAL/bin/python" -m pip check
  selected_venv="$VENV_FINAL"
else
  "$OLD_PYTHON" -m venv "$VENV_BUILD"
  "$VENV_BUILD/bin/python" -m pip install --disable-pip-version-check -r "$OLD_FREEZE"
  "$VENV_BUILD/bin/python" -m pip check
  "$VENV_BUILD/bin/python" -m pip freeze --all | LC_ALL=C sort > "${TEMP_ROOT}/new-freeze.txt"
  cmp -s "$OLD_FREEZE" "${TEMP_ROOT}/new-freeze.txt" || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "new venv inventory differs"
  selected_venv="$VENV_BUILD"
fi
[[ -z "$("${selected_venv}/bin/python" -m pip list --editable --format=freeze)" ]] || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "new venv has editable packages"

section "RELEASE" "Linking the single mutable-state source and runtime environment"
[[ -d "${WORKTREE}/data" ]] || die "BLOCKED_MUTABLE_STATE_MISSING" "${WORKTREE}/data"
if (( REUSE_RELEASE == 1 )); then
  [[ "$selected_venv" == "$VENV_FINAL" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "dedicated venv missing"
  for relative_path in "${MUTABLE_PATHS[@]}"; do
    release_path="${VALIDATION_ROOT}/${relative_path}"
    target_path="${WORKTREE}/${relative_path}"
    [[ "$release_path" == "${VALIDATION_ROOT}/"* && "$release_path" != "$VALIDATION_ROOT" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "unsafe release path: ${relative_path}"
    [[ "$target_path" == "${WORKTREE}/"* && "$target_path" != "$WORKTREE" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "unsafe target path: ${relative_path}"
    [[ -L "$release_path" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "mutable link missing: ${relative_path}"
    [[ "$(readlink "$release_path")" == "$target_path" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "mutable link target: ${relative_path}"
  done
  [[ -L "${VALIDATION_ROOT}/.env" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" ".env link missing"
  [[ "$(readlink "${VALIDATION_ROOT}/.env")" == "$RUNTIME_ENV" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" ".env link target"
  [[ -L "${VALIDATION_ROOT}/.venv" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" ".venv link missing"
  [[ "$(readlink "${VALIDATION_ROOT}/.venv")" == "$VENV_FINAL" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" ".venv link target"
else
  install -d -m 0755 -o binancebot -g binancebot "${WORKTREE}/trading/reports"
  for relative_path in "${MUTABLE_PATHS[@]}"; do
    release_path="${VALIDATION_ROOT}/${relative_path}"
    target_path="${WORKTREE}/${relative_path}"
    [[ "$release_path" == "${VALIDATION_ROOT}/"* && "$release_path" != "$VALIDATION_ROOT" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "unsafe release path: ${relative_path}"
    [[ "$target_path" == "${WORKTREE}/"* && "$target_path" != "$WORKTREE" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "unsafe target path: ${relative_path}"
    if [[ -e "$release_path" || -L "$release_path" ]]; then
      rm -rf -- "$release_path"
    fi
    install -d -m 0755 "$(dirname "$release_path")"
    ln -s "$target_path" "$release_path"
    [[ -L "$release_path" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "$relative_path"
    [[ "$(readlink "$release_path")" == "$target_path" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "$relative_path"
  done
  rm -f -- "${VALIDATION_ROOT}/.env"
  ln -s "$RUNTIME_ENV" "${VALIDATION_ROOT}/.env"
  rm -f -- "${VALIDATION_ROOT}/.venv"
  ln -s "$selected_venv" "${VALIDATION_ROOT}/.venv"
fi
release_source_manifest "$VALIDATION_ROOT" "${TEMP_ROOT}/release-tree-pre-validation.sha256"
if (( REUSE_RELEASE == 1 )); then
  [[ -f "${VALIDATION_ROOT}/.release-tree.sha256" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "source manifest missing"
  cmp -s "${VALIDATION_ROOT}/.release-tree.sha256" "${TEMP_ROOT}/release-tree-pre-validation.sha256" || die "BLOCKED_EXISTING_RELEASE_INVALID" "source tree differs from recorded manifest"
else
  install -m 0644 -o root -g root "${TEMP_ROOT}/release-tree-pre-validation.sha256" "${VALIDATION_ROOT}/.release-tree.sha256"
fi
RELEASE_SOURCE_FINGERPRINT="$(sha256sum "${VALIDATION_ROOT}/.release-tree.sha256" | awk '{print $1}')"
[[ "$RELEASE_SOURCE_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]] || die "BLOCKED_RELEASE_FINGERPRINT_INVALID" "unexpected fingerprint"
info "RELEASE_SOURCE_FINGERPRINT=${RELEASE_SOURCE_FINGERPRINT}"

snapshot_mutable() {
  local destination="$1"
  {
    find -P "${WORKTREE}/data" -type f -print0
    for relative_path in "${MUTABLE_PATHS[@]:1}"; do
      target_path="${WORKTREE}/${relative_path}"
      [[ "$target_path" == "${WORKTREE}/"* && "$target_path" != "$WORKTREE" ]] || die "BLOCKED_MUTABLE_LINK_FAILED" "unsafe target path: ${relative_path}"
      [[ -f "$target_path" ]] && printf '%s\0' "$target_path"
      if [[ -d "$target_path" ]]; then
        find -P "$target_path" -type f -print0
      fi
    done
  } | sort -z -u | xargs -0 -r sha256sum > "$destination"
}

snapshot_mutable "$MUTABLE_BEFORE"

section "VERIFY" "Offline validation from the release candidate"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${TEMP_ROOT}/pycache"
(
  trap - ERR
  cd "$VALIDATION_ROOT"
  bash -n scripts/run_once.sh
  .venv/bin/python -m py_compile trading/*.py
  .venv/bin/python -m py_compile trading/orchestration/*.py
  .venv/bin/python -m py_compile dashboard/app.py
  .venv/bin/python -m unittest discover -s trading
  .venv/bin/python trading/check_version_consistency.py --strict
  .venv/bin/python trading/audit_data_quality.py
  .venv/bin/python -m pip check
  PYTHONPATH=trading .venv/bin/python - <<PY
import capability_history
import version_history
assert version_history.current_version() == '${EXPECTED_VERSION}'
item = next(value for value in capability_history.CAPABILITIES if value['id'] == '${EXPECTED_CAPABILITY}')
assert item['status'] == 'IMPLEMENTED'
print('RUNTIME_VERSION=${EXPECTED_VERSION}')
print('CAPABILITY=${EXPECTED_CAPABILITY}:IMPLEMENTED')
PY
)

snapshot_mutable "$MUTABLE_AFTER"
cmp -s "$MUTABLE_BEFORE" "$MUTABLE_AFTER" || die "VERIFICATION_FAILED" "offline validation changed mutable production data"
info "MUTABLE_STATE_UNCHANGED=true"
release_source_manifest "$VALIDATION_ROOT" "${TEMP_ROOT}/release-tree-post-validation.sha256"
cmp -s "${VALIDATION_ROOT}/.release-tree.sha256" "${TEMP_ROOT}/release-tree-post-validation.sha256" || die "VERIFICATION_FAILED" "release source changed during offline validation"
info "RELEASE_SOURCE_FINGERPRINT_VALIDATED=${RELEASE_SOURCE_FINGERPRINT}"


section "RELEASE" "Finalizing immutable source and virtualenv"
if [[ "$selected_venv" == "$VENV_BUILD" ]]; then
  mv -T "$VENV_BUILD" "$VENV_FINAL"
  selected_venv="$VENV_FINAL"
  rm -f -- "${VALIDATION_ROOT}/.venv"
  ln -s "$VENV_FINAL" "${VALIDATION_ROOT}/.venv"
fi

if [[ "$VALIDATION_ROOT" == "$RELEASE_BUILD" ]]; then
  rm -f -- "${RELEASE_BUILD}/.BUILDING"
  find -P "$RELEASE_BUILD" -type f -exec chown root:root {} + -exec chmod a-w {} +
  find -P "$RELEASE_BUILD" -type d -exec chown root:root {} + -exec chmod 0555 {} +
  find -P "$RELEASE_BUILD" -type l -exec chown -h root:root {} +
  mv -T "$RELEASE_BUILD" "$RELEASE_FINAL"
  VALIDATION_ROOT="$RELEASE_FINAL"
fi

find -P "$VENV_FINAL" -type f -exec chown root:root {} + -exec chmod a-w {} +
find -P "$VENV_FINAL" -type d -exec chown root:root {} + -exec chmod 0555 {} +
find -P "$VENV_FINAL" -type l -exec chown -h root:root {} +
runuser -u binancebot -- test ! -w "${RELEASE_FINAL}/trading/bot.py" || die "BLOCKED_RELEASE_WRITABLE" "source writable by binancebot"
runuser -u binancebot -- test ! -w "${VENV_FINAL}/bin/python" || die "BLOCKED_RELEASE_WRITABLE" "venv writable by binancebot"

section "ACTIVATE" "Atomically switching current"
if [[ -e "$CURRENT_NEW" || -L "$CURRENT_NEW" ]]; then
  [[ -L "$CURRENT_NEW" ]] || die "BLOCKED_CURRENT_NEW_INVALID" "$CURRENT_NEW"
  rm -f -- "$CURRENT_NEW"
fi
ln -s "$RELEASE_FINAL" "$CURRENT_NEW"
[[ "$(readlink -f "$CURRENT_NEW")" == "$RELEASE_FINAL" ]] || die "BLOCKED_CURRENT_NEW_INVALID" "target mismatch"
mv -Tf "$CURRENT_NEW" "$CURRENT_LINK"
[[ "$(readlink -f "$CURRENT_LINK")" == "$RELEASE_FINAL" ]] || die "BLOCKED_CURRENT_INVALID" "atomic switch failed"
info "CURRENT_RELEASE=${RELEASE_FINAL}"

section "SYSTEMD" "Creating path-only service overrides"
for service in "${SERVICE_UNITS[@]}"; do
  [[ "$service" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || die "BLOCKED_SYSTEMD_EXECSTART" "unsafe service name"
  if ! original_exec_output="$(resolve_original_exec_start "$service")"; then
    die "BLOCKED_SYSTEMD_EXECSTART" "cannot read or parse fragment/drop-ins for ${service}"
  fi
  [[ -n "$original_exec_output" ]] || die "BLOCKED_SYSTEMD_EXECSTART" "cannot resolve ${service}"
  mapfile -t original_execs <<< "$original_exec_output"
  (( ${#original_execs[@]} == 1 )) || die "BLOCKED_SYSTEMD_EXECSTART" "multiple ExecStart commands are not supported safely: ${service}"
  original_exec="${original_execs[0]}"
  migrated_exec="${original_exec//${WORKTREE}/${CURRENT_LINK}}"
  [[ "$migrated_exec" != "$original_exec" && "$migrated_exec" == *"${CURRENT_LINK}"* ]] || die "BLOCKED_SYSTEMD_EXECSTART" "cannot relocate ${service}"
  override_dir="${SYSTEMD_ROOT}/${service}.d"
  override_file="${override_dir}/immutable-runtime.conf"
  install -d -m 0755 -o root -g root "$override_dir"
  {
    printf '[Service]\n'
    printf 'WorkingDirectory=%s\n' "$CURRENT_LINK"
    printf 'ExecStart=\n'
    printf 'ExecStart=%s\n' "$migrated_exec"
    printf 'Environment=PYTHONDONTWRITEBYTECODE=1\n'
  } > "$override_file"
  chown root:root "$override_file"
  chmod 0644 "$override_file"
  info "OVERRIDE=${override_file}"
done

systemctl daemon-reload
systemd-analyze verify "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"

for service in "${SERVICE_UNITS[@]}"; do
  effective_workdir="$(systemctl show "$service" -p WorkingDirectory --value)"
  effective_exec="$(systemctl show "$service" -p ExecStart --value)"
  [[ "$effective_workdir" == "$CURRENT_LINK" ]] || die "BLOCKED_SYSTEMD_VALIDATION" "${service} WorkingDirectory"
  [[ "$effective_exec" == *"${CURRENT_LINK}"* && "$effective_exec" != *"${WORKTREE}"* ]] || die "BLOCKED_SYSTEMD_VALIDATION" "${service} ExecStart"
done

section "ACTIVATE" "Restoring previously active resident services and timers"
main_before="$(systemctl show "$MAIN_SERVICE" -p ExecMainStartTimestampMonotonic --value)"
guardian_before="$(systemctl show "$GUARDIAN_SERVICE" -p ExecMainStartTimestampMonotonic --value)"

for service in "${RESIDENT_SERVICES[@]}"; do
  if [[ "${INITIAL_ACTIVE[$service]}" == "active" ]]; then
    systemctl start "$service"
  fi
done
start_initial_timers_in_order

for service in "${RESIDENT_SERVICES[@]}"; do
  if [[ "${INITIAL_ACTIVE[$service]}" == "active" ]]; then
    systemctl is-active --quiet "$service" || die "BLOCKED_POST_CUTOVER_SERVICE" "$service"
  fi
done
for timer in "${TIMER_UNITS[@]}"; do
  if [[ "${INITIAL_ACTIVE[$timer]}" == "active" ]]; then
    systemctl is-active --quiet "$timer" || die "BLOCKED_POST_CUTOVER_TIMER" "$timer"
  fi
done

section "VERIFY" "Observing three natural main-timer cycles"
declare -A observed_starts=()
observed_starts["$main_before"]=1
cycle_count=0
deadline=$((SECONDS + POST_CUTOVER_TIMEOUT_SECONDS))
while (( cycle_count < POST_CUTOVER_CYCLES && SECONDS < deadline )); do
  sleep 5
  start_mark="$(systemctl show "$MAIN_SERVICE" -p ExecMainStartTimestampMonotonic --value)"
  [[ -n "$start_mark" && -z "${observed_starts[$start_mark]:-}" ]] || continue
  systemctl is-active --quiet "$MAIN_SERVICE" && continue
  observed_starts["$start_mark"]=1
  result="$(systemctl show "$MAIN_SERVICE" -p Result --value)"
  exec_status="$(systemctl show "$MAIN_SERVICE" -p ExecMainStatus --value)"
  [[ "$result" == "success" && "$exec_status" == "0" ]] || die "BLOCKED_POST_CUTOVER_CYCLE" "Result=${result} ExecMainStatus=${exec_status}"
  cycle_count=$((cycle_count + 1))
  printf 'NATURAL_CYCLE_%s_START=%s\n' "$cycle_count" "$(systemctl show "$MAIN_SERVICE" -p ExecMainStartTimestamp --value)"
  printf 'NATURAL_CYCLE_%s_RESULT=%s\n' "$cycle_count" "$result"
  printf 'NATURAL_CYCLE_%s_EXEC_MAIN_STATUS=%s\n' "$cycle_count" "$exec_status"
  "$VENV_FINAL/bin/python" - <<'PY'
import json
from pathlib import Path
state = json.loads(Path('/home/binancebot/BinanceBot/trading/bot_state.json').read_text(encoding='utf-8-sig'))
short = ((state.get('positions') or {}).get('short') or {})
reconciliation = short.get('reconciliation') or {}
version = str(state.get('bot_version'))
gate_mode = str((state.get('pre_entry_safety_summary') or {}).get('mode'))
reconciliation_status = str(reconciliation.get('status'))
if version != 'v1.5-preventive-futures-close-fix':
    raise SystemExit('BLOCKED_POST_CUTOVER_VERSION')
if gate_mode != 'AUDIT_ONLY':
    raise SystemExit('BLOCKED_POST_CUTOVER_GATE_MODE')
if reconciliation_status != 'ALINEADO':
    raise SystemExit('BLOCKED_POST_CUTOVER_RECONCILIATION')
print('NATURAL_CYCLE_RUNTIME_VERSION=' + version)
print('NATURAL_CYCLE_GATE_MODE=' + gate_mode)
print('NATURAL_CYCLE_FUTURES_POSITIONS=' + str(reconciliation.get('observed_count')))
print('NATURAL_CYCLE_RECONCILIATION=' + reconciliation_status)
PY
done
(( cycle_count == POST_CUTOVER_CYCLES )) || die "BLOCKED_POST_CUTOVER_TIMEOUT" "observed ${cycle_count} cycles"

guardian_after="$(systemctl show "$GUARDIAN_SERVICE" -p ExecMainStartTimestampMonotonic --value)"
[[ "$guardian_after" != "$guardian_before" ]] || die "BLOCKED_POST_CUTOVER_GUARDIAN" "no natural Guardian execution"
[[ "$(systemctl show "$GUARDIAN_SERVICE" -p Result --value)" == "success" ]] || die "BLOCKED_POST_CUTOVER_GUARDIAN" "Guardian Result"
[[ "$(systemctl show "$GUARDIAN_SERVICE" -p ExecMainStatus --value)" == "0" ]] || die "BLOCKED_POST_CUTOVER_GUARDIAN" "Guardian ExecMainStatus"

for resident in binancebot-dashboard.service binancebot-telegram.service; do
  systemctl is-active --quiet "$resident" || die "BLOCKED_POST_CUTOVER_SERVICE" "$resident"
  [[ "$(systemctl show "$resident" -p SubState --value)" == "running" ]] || die "BLOCKED_POST_CUTOVER_SERVICE" "${resident} is not running"
done

for service in "${SERVICE_UNITS[@]}"; do
  if journalctl -u "$service" --since "$UTC_STARTED" --no-pager | grep -Eqi 'Traceback|ImportError|ModuleNotFoundError|Permission denied'; then
    die "BLOCKED_POST_CUTOVER_LOG_ERROR" "${service} import/permission error"
  fi
done

section "VERIFY" "Proving runtime isolation without editing the working tree"
[[ "$(readlink -f "$CURRENT_LINK")" == "$RELEASE_FINAL" ]] || die "BLOCKED_ISOLATION_PROOF" "current target"
for service in "${SERVICE_UNITS[@]}"; do
  effective="$(systemctl show "$service" -p WorkingDirectory -p ExecStart)"
  [[ "$effective" != *"${WORKTREE}"* && "$effective" == *"${CURRENT_LINK}"* ]] || die "BLOCKED_ISOLATION_PROOF" "$service"
done
for resident in "${RESIDENT_SERVICES[@]}"; do
  pid="$(systemctl show "$resident" -p MainPID --value)"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
  cwd="$(readlink -f "/proc/${pid}/cwd")"
  cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
  [[ "$cwd" == "$RELEASE_FINAL" ]] || die "BLOCKED_ISOLATION_PROOF" "${resident} cwd=${cwd}"
  [[ "$cmdline" != *"${WORKTREE}"* ]] || die "BLOCKED_ISOLATION_PROOF" "${resident} command"
done
runuser -u binancebot -- test ! -w "${RELEASE_FINAL}/trading/bot.py" || die "BLOCKED_ISOLATION_PROOF" "release source writable"
runuser -u binancebot -- test ! -w "${VENV_FINAL}/bin/python" || die "BLOCKED_ISOLATION_PROOF" "release venv writable"
info "EDITING_WORKTREE_NO_LONGER_DEPLOYS_TO_PRODUCTION"

CUTOVER_STARTED=0
trap - ERR
UTC_FINISHED="$(timestamp)"
section "RESULT" "Immutable runtime migration completed"
printf '[RESULT] IMMUTABLE_RELEASE_RUNTIME_MIGRATED\n'
printf 'RELEASE_COMMIT=%s\n' "$RELEASE_COMMIT"
printf 'CURRENT_RELEASE=%s\n' "$RELEASE_FINAL"
printf 'RELEASE_SOURCE_FINGERPRINT=%s\n' "$RELEASE_SOURCE_FINGERPRINT"
printf 'DEDICATED_VENV=%s\n' "$VENV_FINAL"
printf 'SYSTEMD_BACKUP_PATH=%s\n' "$BACKUP_PATH"
printf 'MIGRATION_STARTED_UTC=%s\n' "$UTC_STARTED"
printf 'MIGRATION_FINISHED_UTC=%s\n' "$UTC_FINISHED"
printf 'MIGRATION_LOG=%s\n' "$LOG_PATH"
printf 'MANUAL_BINANCE_ORDERS=NO\n'
printf 'MANUAL_CYCLES=NO\n'
