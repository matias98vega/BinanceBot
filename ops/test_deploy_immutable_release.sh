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
  assert_true 'T1 clean candidate is valid' candidate_state_valid main "$candidate" "$origin" '' "$current"
  assert_false 'T2 dirty working tree blocks' candidate_state_valid main "$candidate" "$origin" "$dirty" "$current"
  assert_false 'T3 HEAD different from origin blocks' candidate_state_valid main "$candidate" "$(sha40 3)" '' "$current"
  assert_false 'T4 candidate equal to current blocks' candidate_state_valid main "$candidate" "$origin" '' "$candidate"
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
  assert_true 'T5 complete matching release is reusable' \
    validate_existing_release "$candidate" "$candidate_venv" "$commit" "$current" "$scratch"
  : > "$candidate/.BUILDING"
  assert_false 'T6 incomplete existing release blocks' \
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
  assert_false 'T7 unsafe preflight prevents cutover' cutover_allowed READY UNSAFE
  before="$(readlink -f "$current_link")"
  assert_false 'T8 failed validation prevents cutover' cutover_allowed FAILED READY
  [[ "$(readlink -f "$current_link")" == "$before" ]] || fail 'T8 current changed after failed validation'
  assert_true 'T9 atomic current switch succeeds' atomic_switch_current "$current_link" "$candidate"
  [[ "$(readlink -f "$current_link")" == "$candidate" ]] || fail 'T9 wrong current target'
  assert_true 'T10 post-cutover failure can restore previous' rollback_current "$current_link" "$previous"
  [[ "$(readlink -f "$current_link")" == "$previous" ]] || fail 'T10 rollback target mismatch'
  assert_true 'T11 first rollback claim succeeds' claim_rollback_once "$rollback_lock"
  assert_false 'T11 second rollback claim is rejected' claim_rollback_once "$rollback_lock"
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
  atomic_switch_current "${area}/current" "${area}/new" || fail 'T12 fixture current switch failed'
  after="$(sha256sum "$dropin")"
  [[ "$before" == "$after" ]] || fail 'T12 drop-in fixture was rewritten'
  ! grep -Eq 'daemon-reload|>[[:space:]]*/etc/systemd|tee[[:space:]]+/etc/systemd|install .*immutable-runtime\.conf' "$DEPLOY_SCRIPT" || \
    fail 'T12 deploy script rewrites systemd contract'
  pass 'T12 existing systemd drop-ins are reused without rewrite'

  prepare_line="$(grep -n 'section "PREPARE"' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  validation_line="$(grep -n '^    run_candidate_validation$' "$DEPLOY_SCRIPT" | cut -d: -f1 | tail -n 1)"
  pause_line="$(grep -n 'section "PAUSE"' "$DEPLOY_SCRIPT" | cut -d: -f1)"
  [[ "$prepare_line" -lt "$validation_line" && "$validation_line" -lt "$pause_line" ]] || \
    fail 'T13 build/validation ordering is unsafe'
  pass 'T13 build, venv and validation finish before pause'

  ! grep -Eq "\.venv/bin/python[[:space:]]+trading/(bot|main)\.py|systemctl[[:space:]]+start[[:space:]]+binancebot\.service" "$DEPLOY_SCRIPT" || \
    fail 'T14 manual bot cycle detected'
  pass 'T14 deployment performs no manual bot cycle'

  safety_body="${area}/safety-body.txt"
  sed -n '/^run_get_only_safety_gate()/,/^observe_natural_cycles()/p' "$DEPLOY_SCRIPT" > "$safety_body"
  grep -q "spot_signed('GET'" "$safety_body" || fail 'T15 GET-only Spot check missing'
  ! grep -Eq "['\"](POST|PUT|DELETE|PATCH)['\"]|create_order|cancel_order|transfer|rebalance" "$safety_body" || \
    fail 'T15 Binance mutation detected'
  pass 'T15 safety preflight is GET-only and contains no Binance mutation'
}

test_candidate_contracts
test_release_reuse
test_cutover_and_rollback
test_static_safety_contracts

[[ "$PASS_COUNT" -eq 16 ]] || fail "unexpected assertion count: ${PASS_COUNT}"
printf '[RESULT] OFFLINE_DEPLOY_HARNESS_PASS T1-T15\n'
