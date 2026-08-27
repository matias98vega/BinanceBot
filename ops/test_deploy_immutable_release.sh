#!/usr/bin/env bash
set -Eeuo pipefail

# Offline contract harness for deploy_immutable_release.sh. Everything created
# by this file lives below /tmp; the deploy main function is never executed.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEPLOY_SCRIPT="${SCRIPT_DIR}/deploy_immutable_release.sh"

export BINANCEBOT_DEPLOY_HARNESS=1
# shellcheck source=deploy_immutable_release.sh
source "$DEPLOY_SCRIPT"

HARNESS_ROOT="$(mktemp -d /tmp/binancebot-deploy-harness.XXXXXX)"
trap 'rm -rf -- "$HARNESS_ROOT"' EXIT

PASS_COUNT=0

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1" >&2
  exit 1
}

assert_true() {
  local label="$1"
  shift
  "$@" || fail "$label"
  pass "$label"
}

assert_false() {
  local label="$1"
  shift
  if "$@"; then
    fail "$label"
  fi
  pass "$label"
}

sha40() {
  printf '%040x' "$1"
}

fixture_release() {
  local root="$1" commit="$2" current="$3" venv="$4"
  local relative target
  mkdir -p "$root" "$venv/bin"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$venv/bin/python"
  chmod 0755 "$venv/bin/python"
  printf '%s\n' "$commit" > "$root/.release-commit"
  printf '{}\n' > "$root/.release-version-commits.json"
  printf 'fixture\n' > "$root/application.txt"
  for relative in "${MUTABLE_PATHS[@]}"; do
    target="$(readlink "${current}/${relative}")"
    mkdir -p "$(dirname "${root}/${relative}")"
    ln -s "$target" "${root}/${relative}"
  done
  ln -s "$(readlink "${current}/.env")" "$root/.env"
  ln -s "$venv" "$root/.venv"
  release_source_manifest "$root" "$root/.release-tree.sha256"
}

fixture_current() {
  local root="$1" mutable="$2" venv="$3"
  local relative target
  mkdir -p "$root" "$mutable" "$venv/bin"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$venv/bin/python"
  chmod 0755 "$venv/bin/python"
  printf '%s\n' "$(sha40 1)" > "$root/.release-commit"
  for relative in "${MUTABLE_PATHS[@]}"; do
    target="${mutable}/${relative}"
    mkdir -p "$(dirname "$target")" "$(dirname "${root}/${relative}")"
    if [[ "$relative" == "data" || "$relative" == "trading/reports" ]]; then
      mkdir -p "$target"
    else
      : > "$target"
    fi
    ln -s "$target" "${root}/${relative}"
  done
  : > "${mutable}/environment"
  ln -s "${mutable}/environment" "$root/.env"
  ln -s "$venv" "$root/.venv"
}

test_candidate_contracts() {
  local candidate current origin dirty
  candidate="$(sha40 2)"
  current="$(sha40 1)"
  origin="$candidate"
  dirty=' M trading/example.py'
  assert_true 'D1 clean candidate is valid' candidate_state_valid main "$candidate" "$origin" '' "$current"
  assert_false 'D2 dirty working tree blocks' candidate_state_valid main "$candidate" "$origin" "$dirty" "$current"
  assert_false 'D3 HEAD different from origin blocks' candidate_state_valid main "$candidate" "$(sha40 3)" '' "$current"
  assert_false 'D4 candidate equal to current blocks' candidate_state_valid main "$candidate" "$origin" '' "$candidate"
}

test_release_reuse() {
  local area current mutable current_venv candidate candidate_venv commit scratch
  area="${HARNESS_ROOT}/reuse"
  current="${area}/current-release"
  mutable="${area}/mutable"
  current_venv="${area}/current-venv"
  candidate="${area}/candidate-release"
  candidate_venv="${area}/candidate-venv"
  commit="$(sha40 2)"
  scratch="${area}/scratch.sha256"
  fixture_current "$current" "$mutable" "$current_venv"
  fixture_release "$candidate" "$commit" "$current" "$candidate_venv"
  assert_true 'D5 complete matching release is reusable' \
    validate_existing_release "$candidate" "$candidate_venv" "$commit" "$current" "$scratch"
  : > "$candidate/.BUILDING"
  assert_false 'D6 incomplete existing release blocks' \
    validate_existing_release "$candidate" "$candidate_venv" "$commit" "$current" "$scratch"
}

test_cutover_and_rollback() {
  local area previous candidate current_link before rollback_lock
  area="${HARNESS_ROOT}/cutover"
  previous="${area}/previous"
  candidate="${area}/candidate"
  current_link="${area}/current"
  rollback_lock="${area}/rollback.lock"
  mkdir -p "$previous" "$candidate"
  ln -s "$previous" "$current_link"
  assert_false 'D7 unsafe preflight prevents cutover' cutover_allowed READY UNSAFE
  before="$(readlink -f "$current_link")"
  assert_false 'D8 failed validation prevents cutover' cutover_allowed FAILED READY
  [[ "$(readlink -f "$current_link")" == "$before" ]] || fail 'D8 current changed after failed validation'
  assert_true 'D9 atomic current switch succeeds' atomic_switch_current "$current_link" "$candidate"
  [[ "$(readlink -f "$current_link")" == "$candidate" ]] || fail 'D9 wrong current target'
  assert_true 'D10 post-cutover failure can restore previous' rollback_current "$current_link" "$previous"
  [[ "$(readlink -f "$current_link")" == "$previous" ]] || fail 'D10 rollback target mismatch'
  assert_true 'D11 first rollback claim succeeds' claim_rollback_once "$rollback_lock"
  assert_false 'D11 second rollback claim is rejected' claim_rollback_once "$rollback_lock"
}

test_static_safety_contracts() {
  local area dropin before after prepare_line validation_line pause_line safety_body
  area="${HARNESS_ROOT}/static"
  mkdir -p "$area"
  dropin="${area}/immutable-runtime.conf"
  printf '[Service]\nWorkingDirectory=/opt/binancebot/current\n' > "$dropin"
  before="$(sha256sum "$dropin")"
  mkdir -p "${area}/old" "${area}/new"
  ln -s "${area}/old" "${area}/current"
  atomic_switch_current "${area}/current" "${area}/new" || fail 'D12 fixture current switch failed'
  after="$(sha256sum "$dropin")"
  [[ "$before" == "$after" ]] || fail 'D12 drop-in fixture was rewritten'
  ! grep -Eq 'daemon-reload|>[[:space:]]*/etc/systemd|tee[[:space:]]+/etc/systemd|install .*immutable-runtime\.conf' "$DEPLOY_SCRIPT" || \
    fail 'D12 deploy script rewrites systemd contract'
  pass 'D12 existing systemd drop-ins are reused without rewrite'

  prepare_line="$(grep -n 'section "PREPARE"' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  validation_line="$(grep -n '^  run_candidate_isolated_validation$' "$DEPLOY_SCRIPT" | cut -d: -f1 | tail -n 1)"
  pause_line="$(grep -n 'section "PAUSE"' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  [[ "$prepare_line" -lt "$validation_line" && "$validation_line" -lt "$pause_line" ]] || \
    fail 'D13 build/validation ordering is unsafe'
  pass 'D13 build, venv and validation finish before pause'

  ! grep -Eq "\.venv/bin/python[[:space:]]+trading/(bot|main)\.py|systemctl[[:space:]]+start[[:space:]]+binancebot\.service" "$DEPLOY_SCRIPT" || \
    fail 'D14 manual bot cycle detected'
  pass 'D14 deployment performs no manual bot cycle'

  safety_body="${area}/safety-body.txt"
  sed -n '/^run_get_only_safety_gate()/,/^observe_natural_cycles()/p' "$DEPLOY_SCRIPT" > "$safety_body"
  grep -q "spot_signed('GET'" "$safety_body" || fail 'D15 GET-only Spot check missing'
  ! grep -Eq "['\"](POST|PUT|DELETE|PATCH)['\"]|create_order|cancel_order|transfer|rebalance" "$safety_body" || \
    fail 'D15 Binance mutation detected'
  pass 'D15 safety preflight is GET-only and contains no Binance mutation'
}

test_state_isolation_contract() {
  local area current mutable current_venv candidate candidate_venv
  local before after changed truncated product_hash append_line isolated_line link_line safety_line
  area="${HARNESS_ROOT}/state-isolation"
  current="${area}/current-release"
  mutable="${area}/mutable"
  current_venv="${area}/current-venv"
  candidate="${area}/candidate"
  candidate_venv="${area}/candidate-venv"
  fixture_current "$current" "$mutable" "$current_venv"
  mkdir -p "$candidate" "$candidate_venv/bin" "${area}/files"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$candidate_venv/bin/python"
  chmod 0755 "$candidate_venv/bin/python"
  ln -s "$candidate_venv" "$candidate/.venv"

  before="${area}/files/append-before.jsonl"
  after="${area}/files/append-after.jsonl"
  printf '{"cycle":1}\n' > "$before"
  cp "$before" "$after"
  printf '{"cycle":2}\n' >> "$after"
  assert_true 'T1 natural production append passes' \
    mutable_change_allowed append production true true "$before" "$after"

  printf '{"cycle":1}\n' > "${area}/files/bot-before.json"
  printf '{"cycle":2}\n' > "${area}/files/bot-after.json"
  assert_true 'T2 correlated natural bot_state rewrite passes when candidate is isolated' \
    mutable_change_allowed atomic production true true \
      "${area}/files/bot-before.json" "${area}/files/bot-after.json"

  mkdir -p "$candidate/trading"
  ln -s "${current}/trading/state.json" "$candidate/trading/state.json"
  assert_false 'T3 candidate access to real state blocks' candidate_validation_isolated "$candidate"
  assert_false 'T3 candidate-attributed state mutation blocks' \
    mutable_change_allowed append candidate true false "$before" "$after"
  rm -f -- "$candidate/trading/state.json"

  changed="${area}/files/append-prefix-changed.jsonl"
  printf '{"cycle":0}\n{"cycle":2}\n' > "$changed"
  assert_false 'T4 modified append-only prefix blocks' \
    mutable_change_allowed append production true true "$before" "$changed"

  truncated="${area}/files/append-truncated.jsonl"
  : > "$truncated"
  assert_false 'T5 append-only truncation blocks' \
    mutable_change_allowed append production true true "$before" "$truncated"

  assert_false 'T6 uncorrelated bot_state rewrite blocks' \
    mutable_change_allowed atomic production false true \
      "${area}/files/bot-before.json" "${area}/files/bot-after.json"

  product_hash="$(sha256sum "${current}/trading/state.json" | awk '{print $1}')"
  prepare_candidate_validation_tree "$candidate" "$candidate_venv" || fail 'T7 prepare isolation failed'
  printf '{"fixture":true}\n' > "${area}/temp-state.json"
  [[ "$(sha256sum "${current}/trading/state.json" | awk '{print $1}')" == "$product_hash" ]] || \
    fail 'T7 production state changed'
  assert_true 'T7 tests have only temporary state and no production links' \
    candidate_validation_isolated "$candidate"

  copy_file_stable "${current}/trading/telegram_alert_state.json" "${area}/telegram-copy.json" || \
    fail 'T8 Telegram copy failed'
  printf '{"event_conditions":{}}\n' > "${area}/telegram-copy.json"
  [[ "$(sha256sum "${current}/trading/telegram_alert_state.json" | awk '{print $1}')" == \
     "$(sha256sum "${mutable}/trading/telegram_alert_state.json" | awk '{print $1}')" ]] || \
    fail 'T8 production Telegram state changed'
  grep -q 'target_dir="${VALIDATION_SANDBOX}/telegram"' "$DEPLOY_SCRIPT" || \
    fail 'T8 Telegram sandbox contract missing'
  pass 'T8 Telegram compatibility writes only its sandbox copy'

  printf '{"cycle":3}\n{"cycle":4}\n' >> "$after"
  assert_true 'T9 multiple natural production appends pass' \
    mutable_change_allowed append production true true "$before" "$after"

  assert_true 'T10 candidate has no mutable production symlinks during tests' \
    candidate_validation_isolated "$candidate"

  isolated_line="$(grep -n '^  run_candidate_isolated_validation$' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  link_line="$(grep -n '^    link_candidate_mutable_state ' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  safety_line="$(grep -n 'section "SAFETY"' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  [[ "$isolated_line" -lt "$link_line" && "$link_line" -lt "$safety_line" ]] || \
    fail 'T11 state linking order is unsafe'
  pass 'T11 production state linking occurs after isolated validation and before safety/cutover'

  mkdir -p "${area}/previous" "${area}/new"
  ln -s "${area}/previous" "${area}/current"
  assert_false 'T12 PREPARE failure is not cutover-ready' cutover_allowed FAILED READY
  [[ "$(readlink -f "${area}/current")" == "${area}/previous" ]] || \
    fail 'T12 PREPARE failure changed current'
  pass 'T12 PREPARE failure leaves current intact'
}

test_spot_safety_contracts() {
  local area output python_pass_count candidate venv previous current safety_body
  area="${HARNESS_ROOT}/spot-safety"
  output="$(
    PYTHONDONTWRITEBYTECODE=1 "${WORKTREE}/.venv/bin/python" \
      "${SCRIPT_DIR}/test_deploy_spot_safety.py" --temp-root "${area}/python"
  )" || fail 'S1-S22/C1-C2 Python Spot safety fixtures failed'
  printf '%s\n' "$output"
  python_pass_count="$(grep -c '^\[PASS\] ' <<< "$output")"
  [[ "$python_pass_count" -eq 24 ]] || fail "unexpected Spot Python assertion count: ${python_pass_count}"
  PASS_COUNT=$((PASS_COUNT + python_pass_count))

  candidate="${area}/isolated-candidate"
  venv="${area}/isolated-venv"
  mkdir -p "$candidate" "$venv"
  ln -s "$venv" "${candidate}/.venv"
  assert_true 'S23 candidate validation remains isolated' candidate_validation_isolated "$candidate"

  mkdir -p "${area}/previous" "${area}/new"
  current="${area}/current"
  ln -s "${area}/previous" "$current"
  previous="$(readlink -f "$current")"
  assert_false 'S24 PREPARE failure is not cutover-ready' cutover_allowed FAILED READY
  [[ "$(readlink -f "$current")" == "$previous" ]] || fail 'S24 PREPARE failure changed current'

  safety_body="${area}/safety-body.txt"
  sed -n '/^run_get_only_safety_gate()/,/^observe_natural_cycles()/p' "$DEPLOY_SCRIPT" > "$safety_body"
  grep -q "spot_signed('GET'" "$safety_body" || fail 'S25 GET-only Spot order observation missing'
  ! grep -Eq '(^|[^A-Z])(POST|PUT|DELETE|PATCH)([^A-Z]|$)|create_order|cancel_order|transfer|rebalance' \
    "$safety_body" "${SCRIPT_DIR}/deploy_spot_safety.py" || fail 'S25 Binance mutation detected'
  pass 'S25 deploy safety gate contains no Binance mutation'
}

test_candidate_contracts
test_release_reuse
test_cutover_and_rollback
test_static_safety_contracts
test_state_isolation_contract
test_spot_safety_contracts

[[ "$PASS_COUNT" -eq 57 ]] || fail "unexpected assertion count: ${PASS_COUNT}"
printf '[RESULT] OFFLINE_DEPLOY_HARNESS_PASS D1-D15 T1-T12 S1-S25 C1-C2\n'
