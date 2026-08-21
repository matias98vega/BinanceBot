#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" && "${BINANCEBOT_DEPLOY_HARNESS:-}" != "1" ]]; then
  printf '[RESULT] BLOCKED_SOURCED_EXECUTION\n'
  return 1
fi

set -Eeuo pipefail

# Operator-run incremental deployment for an existing immutable-release-v1 runtime.
# Preparation and offline validation finish before the short cutover pause.
# The script performs no manual bot cycle and no Binance mutation. Its safety
# preflight uses authenticated GET requests only.

readonly WORKTREE="/home/binancebot/BinanceBot"
readonly RUNTIME_ROOT="/opt/binancebot"
readonly RELEASES_ROOT="${RUNTIME_ROOT}/releases"
readonly VENVS_ROOT="${RUNTIME_ROOT}/venvs"
readonly CURRENT_LINK="${RUNTIME_ROOT}/current"
readonly CURRENT_NEW="${RUNTIME_ROOT}/current.new"
readonly POST_CUTOVER_CYCLES=3
readonly POST_CUTOVER_TIMEOUT_SECONDS=720

readonly -a SERVICE_UNITS=(
  "binancebot.service"
  "binancebot-guardian.service"
  "binancebot-dashboard.service"
  "binancebot-telegram.service"
)
readonly -a TIMER_UNITS=(
  "binancebot.timer"
  "binancebot-guardian.timer"
)
readonly -a RESIDENT_SERVICES=(
  "binancebot-dashboard.service"
  "binancebot-telegram.service"
)
readonly -a ONESHOT_SERVICES=(
  "binancebot.service"
  "binancebot-guardian.service"
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

declare -A INITIAL_ACTIVE=()
declare -A INITIAL_ENABLED=()

UTC_STAMP=""
UTC_STARTED=""
CUTOVER_PAUSE_UTC=""
LOG_PATH=""
TEMP_ROOT=""
RELEASE_COMMIT=""
CURRENT_BEFORE=""
CURRENT_BEFORE_COMMIT=""
RELEASE_BUILD=""
RELEASE_FINAL=""
VENV_BUILD=""
VENV_FINAL=""
VALIDATION_ROOT=""
PRODUCTION_PYTHON=""
CANDIDATE_VERSION=""
RELEASE_SOURCE_FINGERPRINT=""
MUTABLE_BEFORE=""
MUTABLE_AFTER=""
CUTOVER_STARTED=0
CURRENT_SWITCHED=0
ROLLBACK_RUNNING=0
ROLLBACK_LOCK_DIR=""
ROLLBACK_STATE_FILE=""

now_utc() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

section() {
  printf '\n[%s] %s %s\n' "$1" "$(now_utc)" "$2"
}

info() {
  printf '%s %s\n' "$(now_utc)" "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "BLOCKED_MISSING_COMMAND" "$1"
}

candidate_state_valid() {
  local branch="$1" head="$2" origin="$3" tree_state="$4" current_commit="$5"
  [[ "$branch" == "main" ]] || return 1
  [[ "$head" =~ ^[0-9a-f]{40}$ && "$head" == "$origin" ]] || return 1
  [[ -z "$tree_state" ]] || return 1
  [[ "$head" != "$current_commit" ]] || return 1
}

cutover_allowed() {
  [[ "$1" == "READY" && "$2" == "READY" ]]
}

claim_rollback_once() {
  local lock_dir="$1"
  [[ -n "$lock_dir" ]] || return 1
  mkdir "$lock_dir" 2>/dev/null
}

set_rollback_state() {
  local value="$1"
  [[ -n "$ROLLBACK_STATE_FILE" ]] || return 1
  printf '%s\n' "$value" > "$ROLLBACK_STATE_FILE"
}

release_source_manifest() {
  local root="$1" destination="$2"
  (
    cd "$root"
    find -P . -type f \
      ! -path './.release-commit' \
      ! -path './.release-tree.sha256' \
      ! -path './.BUILDING' \
      -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
  ) > "$destination"
}

mutable_links_match() {
  local candidate="$1" current="$2" candidate_venv="$3"
  local relative candidate_path current_path
  for relative in "${MUTABLE_PATHS[@]}"; do
    candidate_path="${candidate}/${relative}"
    current_path="${current}/${relative}"
    [[ -L "$candidate_path" && -L "$current_path" ]] || return 1
    [[ "$(readlink "$candidate_path")" == "$(readlink "$current_path")" ]] || return 1
  done
  [[ -L "${candidate}/.env" && -L "${current}/.env" ]] || return 1
  [[ "$(readlink "${candidate}/.env")" == "$(readlink "${current}/.env")" ]] || return 1
  [[ -L "${candidate}/.venv" ]] || return 1
  [[ "$(readlink "${candidate}/.venv")" == "$candidate_venv" ]] || return 1
}

validate_existing_release() {
  local release="$1" venv="$2" commit="$3" current="$4" scratch_manifest="$5"
  [[ -d "$release" && ! -L "$release" ]] || return 1
  [[ -f "${release}/.release-commit" && ! -L "${release}/.release-commit" ]] || return 1
  [[ "$(sed -n '1p' "${release}/.release-commit")" == "$commit" ]] || return 1
  [[ -f "${release}/.release-version-commits.json" ]] || return 1
  [[ -f "${release}/.release-tree.sha256" ]] || return 1
  [[ ! -e "${release}/.BUILDING" ]] || return 1
  [[ -x "${venv}/bin/python" ]] || return 1
  mutable_links_match "$release" "$current" "$venv" || return 1
  release_source_manifest "$release" "$scratch_manifest"
  cmp -s "${release}/.release-tree.sha256" "$scratch_manifest"
}

atomic_switch_current() {
  local current_link="$1" target="$2" next_link="${1}.new"
  [[ -d "$target" && ! -L "$target" ]] || return 1
  if [[ -e "$next_link" || -L "$next_link" ]]; then
    [[ -L "$next_link" ]] || return 1
    rm -f -- "$next_link"
  fi
  ln -s "$target" "$next_link"
  [[ "$(readlink -f "$next_link")" == "$target" ]] || return 1
  mv -Tf "$next_link" "$current_link"
  [[ "$(readlink -f "$current_link")" == "$target" ]]
}

rollback_current() {
  local current_link="$1" previous="$2"
  [[ -d "$previous" && ! -L "$previous" ]] || return 1
  if [[ "$(readlink -f "$current_link" 2>/dev/null || true)" == "$previous" ]]; then
    return 0
  fi
  atomic_switch_current "$current_link" "$previous"
}

record_unit_state() {
  local unit="$1"
  INITIAL_ACTIVE["$unit"]="$(systemctl is-active "$unit" 2>/dev/null || true)"
  INITIAL_ENABLED["$unit"]="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
}

validate_systemd_contract() {
  local unit load_state working_directory exec_start
  for unit in "${SERVICE_UNITS[@]}"; do
    load_state="$(systemctl show "$unit" -p LoadState --value 2>/dev/null || true)"
    [[ "$load_state" == "loaded" ]] || return 1
    working_directory="$(systemctl show "$unit" -p WorkingDirectory --value)"
    exec_start="$(systemctl show "$unit" -p ExecStart --value)"
    [[ "$working_directory" == "$CURRENT_LINK" ]] || return 1
    [[ "$exec_start" == *"$CURRENT_LINK"* && "$exec_start" != *"$WORKTREE"* ]] || return 1
  done
  for unit in "${TIMER_UNITS[@]}"; do
    [[ "$(systemctl show "$unit" -p LoadState --value 2>/dev/null || true)" == "loaded" ]] || return 1
  done
}

start_initial_runtime() {
  local service timer
  for service in "${RESIDENT_SERVICES[@]}"; do
    if [[ "${INITIAL_ACTIVE[$service]:-inactive}" == "active" ]]; then
      systemctl start "$service"
    fi
  done
  for timer in "${TIMER_UNITS[@]}"; do
    if [[ "${INITIAL_ACTIVE[$timer]:-inactive}" == "active" ]]; then
      systemctl start "$timer"
    fi
  done
}

stop_runtime_for_cutover() {
  local timer service deadline
  for timer in "${TIMER_UNITS[@]}"; do
    systemctl stop "$timer"
  done
  for timer in "${TIMER_UNITS[@]}"; do
    systemctl is-active --quiet "$timer" && return 1
  done
  for service in "${ONESHOT_SERVICES[@]}"; do
    deadline=$((SECONDS + 180))
    while systemctl is-active --quiet "$service" || \
          [[ "$(systemctl show "$service" -p ActiveState --value)" == "activating" ]]; do
      (( SECONDS < deadline )) || return 1
      sleep 2
    done
  done
  for service in "${RESIDENT_SERVICES[@]}"; do
    systemctl stop "$service"
  done
  for service in "${RESIDENT_SERVICES[@]}"; do
    [[ "$(systemctl show "$service" -p MainPID --value)" == "0" ]] || return 1
  done
}

rollback() {
  local original_rc="${1:-1}"
  (( ROLLBACK_RUNNING == 0 )) || exit "$original_rc"
  claim_rollback_once "$ROLLBACK_LOCK_DIR" || exit "$original_rc"
  ROLLBACK_RUNNING=1
  trap - ERR
  set +e
  set_rollback_state "STARTED" || true
  section "ROLLBACK" "Restoring previous immutable release"
  for timer in "${TIMER_UNITS[@]}"; do systemctl stop "$timer" >/dev/null 2>&1 || true; done
  for service in "${RESIDENT_SERVICES[@]}"; do systemctl stop "$service" >/dev/null 2>&1 || true; done
  if ! rollback_current "$CURRENT_LINK" "$CURRENT_BEFORE"; then
    set_rollback_state "INCOMPLETE:current" || true
    printf '[RESULT] DEPLOY_ROLLBACK_INCOMPLETE current\n'
    exit "$original_rc"
  fi
  if ! start_initial_runtime; then
    set_rollback_state "INCOMPLETE:services" || true
    printf '[RESULT] DEPLOY_ROLLBACK_INCOMPLETE services\n'
    exit "$original_rc"
  fi
  set_rollback_state "COMPLETED" || true
  printf '[RESULT] IMMUTABLE_RELEASE_DEPLOY_ROLLED_BACK\n'
  printf 'CURRENT_RELEASE=%s\n' "$CURRENT_BEFORE"
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

on_error() {
  local rc=$? line="${BASH_LINENO[0]:-unknown}"
  printf 'ERROR_LINE=%s ERROR_RC=%s\n' "$line" "$rc"
  if (( CUTOVER_STARTED == 1 )); then
    rollback "$rc"
  fi
  printf '[RESULT] DEPLOY_FAILED_BEFORE_CUTOVER\n'
  [[ -n "$LOG_PATH" ]] && printf 'DEPLOY_LOG=%s\n' "$LOG_PATH"
  exit "$rc"
}

snapshot_mutable() {
  local destination="$1" relative target
  {
    for relative in "${MUTABLE_PATHS[@]}"; do
      target="$(readlink "${CURRENT_BEFORE}/${relative}")"
      [[ "$target" == /* ]] || die "BLOCKED_MUTABLE_LINK_INVALID" "$relative"
      if [[ -f "$target" ]]; then
        printf '%s\0' "$target"
      elif [[ -d "$target" ]]; then
        find -P "$target" -type f -print0
      else
        die "BLOCKED_MUTABLE_STATE_MISSING" "$target"
      fi
    done
  } | LC_ALL=C sort -z -u | xargs -0 -r sha256sum > "$destination"
}

link_candidate_mutable_state() {
  local relative release_path source_link target
  for relative in "${MUTABLE_PATHS[@]}"; do
    release_path="${VALIDATION_ROOT}/${relative}"
    source_link="${CURRENT_BEFORE}/${relative}"
    [[ -L "$source_link" ]] || die "BLOCKED_MUTABLE_LINK_INVALID" "$source_link"
    target="$(readlink "$source_link")"
    [[ "$target" == /* ]] || die "BLOCKED_MUTABLE_LINK_INVALID" "$source_link"
    [[ "$release_path" == "${VALIDATION_ROOT}/"* && "$release_path" != "$VALIDATION_ROOT" ]] || \
      die "BLOCKED_RELEASE_PATH_INVALID" "$release_path"
    if [[ -e "$release_path" || -L "$release_path" ]]; then
      rm -rf -- "$release_path"
    fi
    install -d -m 0755 "$(dirname "$release_path")"
    ln -s "$target" "$release_path"
  done
  [[ -L "${CURRENT_BEFORE}/.env" ]] || die "BLOCKED_ENV_MISMATCH" "current .env"
  rm -f -- "${VALIDATION_ROOT}/.env" "${VALIDATION_ROOT}/.venv"
  ln -s "$(readlink "${CURRENT_BEFORE}/.env")" "${VALIDATION_ROOT}/.env"
  ln -s "$VENV_BUILD" "${VALIDATION_ROOT}/.venv"
}

validate_telegram_state_compatibility() {
  local python="$1"
  PYTHONPATH="${VALIDATION_ROOT}/trading" "$python" - "$TEMP_ROOT" <<'PY'
import json
import shutil
import sys
from pathlib import Path

import telegram_alerts

root = Path(sys.argv[1])
source = Path('/home/binancebot/BinanceBot/trading/telegram_alert_state.json')
target = root / 'telegram-alert-state-compatibility.json'
original = json.loads(source.read_text(encoding='utf-8'))
if not isinstance(original, dict):
    raise SystemExit('BLOCKED_TELEGRAM_STATE_MIGRATION_REQUIRED')
shutil.copyfile(source, target)
telegram_alerts.ALERT_STATE_FILE = str(target)
loaded = telegram_alerts._read_state()
if loaded != original:
    raise SystemExit('BLOCKED_TELEGRAM_STATE_MIGRATION_REQUIRED')
fp = telegram_alerts._fingerprint(
    'WARNING', 'BinanceBot', 'fixture 4.1%',
    event_key='preventive_btc_rise:close_shorts',
)
if not telegram_alerts._record_sent(
    'WARNING', 'BinanceBot', 'fixture 4.1%', fp,
    event_key='preventive_btc_rise:close_shorts',
):
    raise SystemExit('BLOCKED_TELEGRAM_STATE_MIGRATION_REQUIRED')
after = json.loads(target.read_text(encoding='utf-8'))
for key, value in original.items():
    if key not in {'alerts', 'updated_at', 'event_conditions'} and after.get(key) != value:
        raise SystemExit('BLOCKED_TELEGRAM_STATE_MIGRATION_REQUIRED')
if not after.get('event_conditions', {}).get('preventive_btc_rise:close_shorts', {}).get('active'):
    raise SystemExit('BLOCKED_TELEGRAM_STATE_MIGRATION_REQUIRED')
print('TELEGRAM_STATE_BACKWARD_COMPATIBLE=true')
PY
}

run_candidate_validation() {
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPYCACHEPREFIX="${TEMP_ROOT}/pycache"
  export BINANCEBOT_TEST_MODE=true
  export BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS=true
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
  )
  validate_telegram_state_compatibility "${VENV_BUILD}/bin/python"
}

run_get_only_safety_gate() {
  PYTHONPATH="${RELEASE_FINAL}/trading" "${VENV_FINAL}/bin/python" - <<'PY'
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
    'unknown_orders': isinstance(futures_orders, list) and isinstance(spot_orders, list),
}
for name, passed in checks.items():
    print(f'SAFETY_{name.upper()}={str(passed).lower()}')
if not all(checks.values()):
    raise SystemExit('BLOCKED_PRE_CUTOVER')
print('PRE_CUTOVER_SAFETY=PASS')
PY
}

observe_natural_cycles() {
  local main_before guardian_before deadline start_mark result exec_status cycle_count=0
  declare -A observed_starts=()
  main_before="$(systemctl show binancebot.service -p ExecMainStartTimestampMonotonic --value)"
  guardian_before="$(systemctl show binancebot-guardian.service -p ExecMainStartTimestampMonotonic --value)"
  observed_starts["$main_before"]=1
  deadline=$((SECONDS + POST_CUTOVER_TIMEOUT_SECONDS))
  while (( cycle_count < POST_CUTOVER_CYCLES && SECONDS < deadline )); do
    sleep 5
    start_mark="$(systemctl show binancebot.service -p ExecMainStartTimestampMonotonic --value)"
    [[ -n "$start_mark" && -z "${observed_starts[$start_mark]:-}" ]] || continue
    systemctl is-active --quiet binancebot.service && continue
    observed_starts["$start_mark"]=1
    result="$(systemctl show binancebot.service -p Result --value)"
    exec_status="$(systemctl show binancebot.service -p ExecMainStatus --value)"
    [[ "$result" == "success" && "$exec_status" == "0" ]] || return 1
    cycle_count=$((cycle_count + 1))
    info "NATURAL_CYCLE_${cycle_count}=success"
    "${VENV_FINAL}/bin/python" - "$CANDIDATE_VERSION" <<'PY'
import json
import sys
from pathlib import Path
state = json.loads(Path('/home/binancebot/BinanceBot/trading/bot_state.json').read_text(encoding='utf-8-sig'))
short = ((state.get('positions') or {}).get('short') or {})
reconciliation = short.get('reconciliation') or {}
if str(state.get('bot_version')) != sys.argv[1]:
    raise SystemExit('BLOCKED_POST_CUTOVER_VERSION')
if str((state.get('pre_entry_safety_summary') or {}).get('mode')) != 'AUDIT_ONLY':
    raise SystemExit('BLOCKED_POST_CUTOVER_GATE_MODE')
if str(reconciliation.get('status')) != 'ALINEADO':
    raise SystemExit('BLOCKED_POST_CUTOVER_RECONCILIATION')
PY
  done
  (( cycle_count == POST_CUTOVER_CYCLES )) || return 1
  [[ "$(systemctl show binancebot-guardian.service -p ExecMainStartTimestampMonotonic --value)" != "$guardian_before" ]] || return 1
  [[ "$(systemctl show binancebot-guardian.service -p Result --value)" == "success" ]] || return 1
  [[ "$(systemctl show binancebot-guardian.service -p ExecMainStatus --value)" == "0" ]] || return 1
}

validate_post_cutover() {
  local service pid cwd effective
  [[ "$(readlink -f "$CURRENT_LINK")" == "$RELEASE_FINAL" ]] || return 1
  [[ "$(sed -n '1p' "${CURRENT_LINK}/.release-commit")" == "$RELEASE_COMMIT" ]] || return 1
  validate_systemd_contract || return 1
  for service in "${RESIDENT_SERVICES[@]}"; do
    systemctl is-active --quiet "$service" || return 1
    [[ "$(systemctl show "$service" -p SubState --value)" == "running" ]] || return 1
    pid="$(systemctl show "$service" -p MainPID --value)"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    cwd="$(readlink -f "/proc/${pid}/cwd")"
    [[ "$cwd" == "$RELEASE_FINAL" ]] || return 1
  done
  [[ -L "${RELEASE_FINAL}/trading/telegram_alert_state.json" ]] || return 1
  [[ "$(readlink "${RELEASE_FINAL}/trading/telegram_alert_state.json")" == \
     "$(readlink "${CURRENT_BEFORE}/trading/telegram_alert_state.json")" ]] || return 1
  for service in "${SERVICE_UNITS[@]}"; do
    if journalctl -u "$service" --since "$CUTOVER_PAUSE_UTC" --no-pager | \
       grep -Eqi 'Traceback|ImportError|ModuleNotFoundError|PermissionError|Permission denied|Telegram alert state read failed|JSONDecodeError'; then
      return 1
    fi
  done
  return 0
}

main() {
  if (( EUID != 0 )); then
    printf '[RESULT] BLOCKED_NOT_ROOT\n'
    exit 1
  fi
  for command_name in awk bash chmod chown cmp date find git grep install journalctl ln mkdir mv readlink rm runuser sed sha256sum sleep sort stat systemctl tar tee touch wc xargs; do
    require_command "$command_name"
  done

  UTC_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  UTC_STARTED="$(now_utc)"
  TEMP_ROOT="/var/tmp/binancebot-immutable-deploy-${UTC_STAMP}"
  ROLLBACK_LOCK_DIR="${TEMP_ROOT}/rollback.lock"
  ROLLBACK_STATE_FILE="${TEMP_ROOT}/rollback.state"
  install -d -m 0700 -o root -g root "$TEMP_ROOT"
  install -d -m 0755 -o root -g root /var/log/binancebot
  LOG_PATH="/var/log/binancebot/immutable-release-deploy-${UTC_STAMP}.log"
  touch "$LOG_PATH"
  chmod 0640 "$LOG_PATH"
  chown root:binancebot "$LOG_PATH"
  exec > >(tee -a "$LOG_PATH") 2>&1
  trap on_error ERR

  section "PREFLIGHT" "Git, current release, runtime dependencies and systemd"
  cd "$WORKTREE"
  local branch head origin tree_state current_resolved
  branch="$(git branch --show-current)"
  head="$(git rev-parse HEAD)"
  origin="$(git rev-parse origin/main)"
  tree_state="$(git status --short)"
  [[ -L "$CURRENT_LINK" ]] || die "BLOCKED_CURRENT_INVALID" "current is not a symlink"
  current_resolved="$(readlink -f "$CURRENT_LINK")"
  [[ "$current_resolved" == "${RELEASES_ROOT}/"* && -d "$current_resolved" ]] || die "BLOCKED_CURRENT_INVALID" "$current_resolved"
  [[ -f "${current_resolved}/.release-commit" ]] || die "BLOCKED_CURRENT_INVALID" "missing commit marker"
  CURRENT_BEFORE="$current_resolved"
  CURRENT_BEFORE_COMMIT="$(sed -n '1p' "${CURRENT_BEFORE}/.release-commit")"
  candidate_state_valid "$branch" "$head" "$origin" "$tree_state" "$CURRENT_BEFORE_COMMIT" || \
    die "BLOCKED_UNEXPECTED_GIT_STATE" "branch/head/origin/tree/current candidate contract"
  RELEASE_COMMIT="$head"
  RELEASE_BUILD="${RELEASES_ROOT}/${RELEASE_COMMIT}.building"
  RELEASE_FINAL="${RELEASES_ROOT}/${RELEASE_COMMIT}"
  VENV_BUILD="${VENVS_ROOT}/${RELEASE_COMMIT}.building"
  VENV_FINAL="${VENVS_ROOT}/${RELEASE_COMMIT}"
  VALIDATION_ROOT="$RELEASE_BUILD"
  MUTABLE_BEFORE="${TEMP_ROOT}/mutable-before.sha256"
  MUTABLE_AFTER="${TEMP_ROOT}/mutable-after.sha256"
  [[ ! -e "$RELEASE_BUILD" && ! -e "$VENV_BUILD" ]] || die "BLOCKED_INCOMPLETE_CANDIDATE_EXISTS" "$RELEASE_COMMIT"
  validate_systemd_contract || die "BLOCKED_SYSTEMD_CONTRACT" "drop-ins do not point to current"
  local unit
  for unit in "${SERVICE_UNITS[@]}" "${TIMER_UNITS[@]}"; do record_unit_state "$unit"; done
  PRODUCTION_PYTHON="${CURRENT_BEFORE}/.venv/bin/python"
  [[ -x "$PRODUCTION_PYTHON" ]] || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "production python"
  [[ -z "$("$PRODUCTION_PYTHON" -m pip list --editable --format=freeze)" ]] || die "BLOCKED_VENV_NOT_REPRODUCIBLE" "editable package"
  "$PRODUCTION_PYTHON" -m pip freeze --all | LC_ALL=C sort > "${TEMP_ROOT}/production-freeze.txt"
  "$PRODUCTION_PYTHON" -m pip check
  cmp -s "${CURRENT_BEFORE}/requirements.txt" "${WORKTREE}/requirements.txt" || \
    die "BLOCKED_DEPENDENCY_CHANGE_REQUIRES_WORKFLOW_UPDATE" "requirements changed"
  CANDIDATE_VERSION="$(PYTHONPATH="${WORKTREE}/trading" "$PRODUCTION_PYTHON" -c 'import version_history; print(version_history.current_version())')"
  info "CURRENT_BEFORE=${CURRENT_BEFORE}"
  info "CURRENT_BEFORE_COMMIT=${CURRENT_BEFORE_COMMIT}"
  info "RELEASE_COMMIT=${RELEASE_COMMIT}"
  info "CANDIDATE_VERSION=${CANDIDATE_VERSION}"

  section "PREPARE" "Materializing and validating candidate before pause"
  install -d -m 0755 -o root -g root "$RELEASES_ROOT" "$VENVS_ROOT"
  if [[ -e "$RELEASE_FINAL" || -e "$VENV_FINAL" ]]; then
    [[ -d "$RELEASE_FINAL" && -d "$VENV_FINAL" ]] || die "BLOCKED_EXISTING_RELEASE_INVALID" "$RELEASE_COMMIT"
    validate_existing_release "$RELEASE_FINAL" "$VENV_FINAL" "$RELEASE_COMMIT" "$CURRENT_BEFORE" "${TEMP_ROOT}/reuse-manifest.sha256" || \
      die "BLOCKED_EXISTING_RELEASE_INVALID" "$RELEASE_FINAL"
    "$VENV_FINAL/bin/python" -m pip freeze --all | LC_ALL=C sort > "${TEMP_ROOT}/reuse-freeze.txt"
    cmp -s "${TEMP_ROOT}/production-freeze.txt" "${TEMP_ROOT}/reuse-freeze.txt" || \
      die "BLOCKED_VENV_NOT_REPRODUCIBLE" "reused venv differs"
    VALIDATION_ROOT="$RELEASE_FINAL"
    snapshot_mutable "$MUTABLE_BEFORE"
    run_candidate_validation
    snapshot_mutable "$MUTABLE_AFTER"
    cmp -s "$MUTABLE_BEFORE" "$MUTABLE_AFTER" || \
      die "VERIFICATION_FAILED" "reused candidate validation changed mutable state"
    info "REUSING_VALIDATED_RELEASE=${RELEASE_FINAL}"
  else
    install -d -m 0755 -o root -g root "$RELEASE_BUILD"
    touch "${RELEASE_BUILD}/.BUILDING"
    git archive --format=tar "$RELEASE_COMMIT" | tar -x -C "$RELEASE_BUILD"
    printf '%s\n' "$RELEASE_COMMIT" > "${RELEASE_BUILD}/.release-commit"
    "$PRODUCTION_PYTHON" "${WORKTREE}/trading/check_version_consistency.py" \
      --emit-release-metadata "$RELEASE_COMMIT" > "${RELEASE_BUILD}/.release-version-commits.json"
    "$PRODUCTION_PYTHON" -m venv "$VENV_BUILD"
    "$VENV_BUILD/bin/python" -m pip install --disable-pip-version-check -r "${TEMP_ROOT}/production-freeze.txt"
    "$VENV_BUILD/bin/python" -m pip check
    "$VENV_BUILD/bin/python" -m pip freeze --all | LC_ALL=C sort > "${TEMP_ROOT}/candidate-freeze.txt"
    cmp -s "${TEMP_ROOT}/production-freeze.txt" "${TEMP_ROOT}/candidate-freeze.txt" || \
      die "BLOCKED_VENV_NOT_REPRODUCIBLE" "candidate venv differs"
    link_candidate_mutable_state
    release_source_manifest "$VALIDATION_ROOT" "${TEMP_ROOT}/release-tree-pre-validation.sha256"
    install -m 0644 -o root -g root "${TEMP_ROOT}/release-tree-pre-validation.sha256" "${VALIDATION_ROOT}/.release-tree.sha256"
    snapshot_mutable "$MUTABLE_BEFORE"
    run_candidate_validation
    snapshot_mutable "$MUTABLE_AFTER"
    cmp -s "$MUTABLE_BEFORE" "$MUTABLE_AFTER" || die "VERIFICATION_FAILED" "candidate validation changed mutable state"
    release_source_manifest "$VALIDATION_ROOT" "${TEMP_ROOT}/release-tree-post-validation.sha256"
    cmp -s "${VALIDATION_ROOT}/.release-tree.sha256" "${TEMP_ROOT}/release-tree-post-validation.sha256" || \
      die "VERIFICATION_FAILED" "release source changed"
    mv -T "$VENV_BUILD" "$VENV_FINAL"
    rm -f -- "${VALIDATION_ROOT}/.venv"
    ln -s "$VENV_FINAL" "${VALIDATION_ROOT}/.venv"
    rm -f -- "${RELEASE_BUILD}/.BUILDING"
    find -P "$RELEASE_BUILD" -type f -exec chown root:root {} + -exec chmod a-w {} +
    find -P "$RELEASE_BUILD" -type d -exec chown root:root {} + -exec chmod 0555 {} +
    find -P "$RELEASE_BUILD" -type l -exec chown -h root:root {} +
    mv -T "$RELEASE_BUILD" "$RELEASE_FINAL"
    find -P "$VENV_FINAL" -type f -exec chown root:root {} + -exec chmod a-w {} +
    find -P "$VENV_FINAL" -type d -exec chown root:root {} + -exec chmod 0555 {} +
    find -P "$VENV_FINAL" -type l -exec chown -h root:root {} +
  fi
  runuser -u binancebot -- test ! -w "${RELEASE_FINAL}/trading/bot.py" || die "BLOCKED_RELEASE_WRITABLE" "source"
  runuser -u binancebot -- test ! -w "${VENV_FINAL}/bin/python" || die "BLOCKED_RELEASE_WRITABLE" "venv"
  RELEASE_SOURCE_FINGERPRINT="$(sha256sum "${RELEASE_FINAL}/.release-tree.sha256" | awk '{print $1}')"
  info "CANDIDATE_VALIDATION=READY"
  info "RELEASE_SOURCE_FINGERPRINT=${RELEASE_SOURCE_FINGERPRINT}"

  section "SAFETY" "Fresh authenticated GET-only pre-cutover gate"
  run_get_only_safety_gate || die "BLOCKED_PRE_CUTOVER" "safety gate"
  cutover_allowed READY READY || die "BLOCKED_PRE_CUTOVER" "candidate or safety not ready"

  section "PAUSE" "Short cutover pause after all preparation"
  CUTOVER_STARTED=1
  stop_runtime_for_cutover || die "ABORT_CUTOVER_RUNTIME_BUSY" "could not stop safely"
  CUTOVER_PAUSE_UTC="$(now_utc)"
  info "CUTOVER_PAUSE_UTC=${CUTOVER_PAUSE_UTC}"

  section "ACTIVATE" "Atomically switching current without rewriting drop-ins"
  atomic_switch_current "$CURRENT_LINK" "$RELEASE_FINAL" || die "BLOCKED_CURRENT_INVALID" "atomic switch"
  CURRENT_SWITCHED=1
  validate_systemd_contract || die "BLOCKED_SYSTEMD_CONTRACT" "effective paths changed"
  start_initial_runtime || die "BLOCKED_POST_CUTOVER_SERVICE" "restore runtime"

  section "VERIFY" "Observing three natural cycles and resident services"
  observe_natural_cycles || die "BLOCKED_POST_CUTOVER_CYCLE" "natural validation"
  validate_post_cutover || die "BLOCKED_POST_CUTOVER_VALIDATION" "runtime, Telegram, or journals"

  CUTOVER_STARTED=0
  trap - ERR
  section "RESULT" "Immutable release deployment completed"
  printf '[RESULT] IMMUTABLE_RELEASE_DEPLOYED\n'
  printf 'RELEASE_COMMIT=%s\n' "$RELEASE_COMMIT"
  printf 'CURRENT_RELEASE=%s\n' "$RELEASE_FINAL"
  printf 'RELEASE_SOURCE_FINGERPRINT=%s\n' "$RELEASE_SOURCE_FINGERPRINT"
  printf 'DEPLOY_LOG=%s\n' "$LOG_PATH"
  printf 'MANUAL_CYCLES=NO\n'
  printf 'BINANCE_MUTATIONS=NO\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
