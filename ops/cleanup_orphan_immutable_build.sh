#!/usr/bin/env bash
set -Eeuo pipefail

# Operator-run cleanup for one audited, failed immutable-runtime build.
# This script does not stop, start, restart, reload, or otherwise mutate systemd.

readonly RELEASES_ROOT="/opt/binancebot/releases"
readonly TARGET_BASENAME="9cb86796645b913c844e1170729a745f482fbb98.building"
readonly TARGET="${RELEASES_ROOT}/${TARGET_BASENAME}"
readonly TARGET_COMMIT="9cb86796645b913c844e1170729a745f482fbb98"
readonly CURRENT_LINK="/opt/binancebot/current"
readonly EXPECTED_CURRENT="${RELEASES_ROOT}/f806baab306d1a3f871dc23007e175f7efed7248"

die() {
  printf '[RESULT] %s\n' "$1"
  shift
  printf 'ERROR=%s\n' "$*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "BLOCKED_MISSING_COMMAND" "$1"
}

resolves_into_target() {
  local path="$1"
  local resolved
  resolved="$(readlink -f -- "$path" 2>/dev/null || true)"
  [[ "$resolved" == "$TARGET" || "$resolved" == "$TARGET/"* ]]
}

check_symlink_tree() {
  local root="$1"
  local link links
  [[ -e "$root" ]] || return 0
  if ! links="$(find -P "$root" -type l -print)"; then
    die "BLOCKED_REFERENCE_CHECK_FAILED" "cannot inspect symlinks under ${root}"
  fi
  while IFS= read -r link; do
    [[ -n "$link" ]] || continue
    if resolves_into_target "$link"; then
      die "BLOCKED_BUILDING_DIR_IN_USE" "symlink=${link}"
    fi
  done <<<"$links"
}

check_systemd_references() {
  local references status unit units values
  if ! units="$(systemctl list-units --all --type=service --type=timer --no-legend --no-pager)"; then
    die "BLOCKED_REFERENCE_CHECK_FAILED" "cannot enumerate systemd units"
  fi
  while read -r unit _; do
    [[ -n "$unit" ]] || continue
    if ! values="$(systemctl show "$unit" \
        -p WorkingDirectory -p ExecStart -p FragmentPath -p DropInPaths \
        2>/dev/null)"; then
      die "BLOCKED_REFERENCE_CHECK_FAILED" "cannot inspect systemd_unit=${unit}"
    fi
    if grep -Fq -- "$TARGET" <<<"$values"; then
      die "BLOCKED_BUILDING_DIR_IN_USE" "systemd_unit=${unit}"
    fi
  done <<<"$units"

  if references="$(grep -RFl -- "$TARGET" \
      /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system 2>/dev/null)"; then
    die "BLOCKED_BUILDING_DIR_IN_USE" "systemd unit file references target: ${references%%$'\n'*}"
  else
    status=$?
    [[ "$status" -eq 1 ]] || \
      die "BLOCKED_REFERENCE_CHECK_FAILED" "cannot inspect systemd unit files"
  fi
}

check_process_references() {
  local proc pid name resolved fd matched comm
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    matched=""
    for name in cwd exe root; do
      resolved="$(readlink "$proc/$name" 2>/dev/null || true)"
      case "$resolved" in
        "$TARGET"|"$TARGET/"*) matched="${name}:${resolved}"; break ;;
      esac
    done
    if [[ -z "$matched" ]]; then
      for fd in "$proc"/fd/*; do
        [[ -e "$fd" || -L "$fd" ]] || continue
        resolved="$(readlink "$fd" 2>/dev/null || true)"
        case "$resolved" in
          "$TARGET"|"$TARGET/"*) matched="fd:${resolved}"; break ;;
        esac
      done
    fi
    if [[ -z "$matched" && -r "$proc/maps" ]] && grep -Fq -- "$TARGET" "$proc/maps"; then
      matched="maps"
    fi
    if [[ -z "$matched" && -r "$proc/cmdline" ]] && \
        tr '\0' '\n' < "$proc/cmdline" | grep -Fq -- "$TARGET"; then
      matched="cmdline"
    fi
    if [[ -n "$matched" ]]; then
      comm="$(sed -n '1p' "$proc/comm" 2>/dev/null || printf 'unknown')"
      die "BLOCKED_BUILDING_DIR_IN_USE" "pid=${pid} comm=${comm} reference=${matched}"
    fi
  done
}

[[ "$EUID" -eq 0 ]] || die "BLOCKED_NOT_ROOT" "run explicitly as root"

for command_name in cmp du find grep mktemp mountpoint readlink rm sed sha256sum sort stat systemctl tr; do
  require_command "$command_name"
done

[[ "$TARGET" == "$RELEASES_ROOT/$TARGET_BASENAME" ]] || \
  die "BLOCKED_INVALID_TARGET" "target mismatch"
[[ "$TARGET" == /opt/binancebot/releases/*.building ]] || \
  die "BLOCKED_INVALID_TARGET" "target is outside releases or lacks .building suffix"
[[ "$TARGET_BASENAME" == *.building ]] || \
  die "BLOCKED_INVALID_TARGET" "basename lacks .building suffix"
[[ -d "$TARGET" && ! -L "$TARGET" ]] || \
  die "BLOCKED_TARGET_MISSING" "$TARGET"
[[ ! -L "$RELEASES_ROOT" && -d "$RELEASES_ROOT" ]] || \
  die "BLOCKED_INVALID_TARGET" "releases root is unavailable or a symlink"
[[ -L "$CURRENT_LINK" ]] || die "BLOCKED_CURRENT_MISMATCH" "current is not a symlink"

current="$(readlink -f -- "$CURRENT_LINK")"
[[ "$current" == "$EXPECTED_CURRENT" ]] || \
  die "BLOCKED_CURRENT_MISMATCH" "current=${current}"
[[ "$current" != "$TARGET" ]] || \
  die "BLOCKED_BUILDING_DIR_IN_USE" "current resolves to target"
[[ -f "$TARGET/.BUILDING" && ! -L "$TARGET/.BUILDING" ]] || \
  die "BLOCKED_TARGET_IDENTITY" "missing regular .BUILDING marker"
[[ -f "$TARGET/.release-commit" && ! -L "$TARGET/.release-commit" ]] || \
  die "BLOCKED_TARGET_IDENTITY" "missing regular .release-commit"
[[ "$(sed -n '1p' "$TARGET/.release-commit")" == "$TARGET_COMMIT" ]] || \
  die "BLOCKED_TARGET_IDENTITY" "unexpected release commit"
mountpoint -q "$TARGET" && die "BLOCKED_BUILDING_DIR_IN_USE" "target is a mountpoint"

check_symlink_tree "/opt/binancebot"
check_symlink_tree "/var/backups/binancebot-systemd"
for rollback_root in /var/tmp/binancebot-immutable-runtime-*; do
  [[ -e "$rollback_root" ]] || continue
  check_symlink_tree "$rollback_root"
done
check_systemd_references
check_process_references

other_releases_before="$(mktemp /tmp/binancebot-other-releases-before.XXXXXX)"
other_releases_after="$(mktemp /tmp/binancebot-other-releases-after.XXXXXX)"
cleanup_temp() {
  rm -f -- "$other_releases_before" "$other_releases_after"
}
trap cleanup_temp EXIT

find "$RELEASES_ROOT" -mindepth 1 -maxdepth 1 \
  ! -name "$TARGET_BASENAME" -printf '%f|%i|%s|%T@\n' | LC_ALL=C sort \
  > "$other_releases_before"

printf 'TARGET=%s\n' "$TARGET"
stat -c 'TYPE=%F OWNER=%U GROUP=%G MODE=%a SIZE_ENTRY=%s MTIME=%y CTIME=%z INODE=%i DEVICE=%d' "$TARGET"
du -sb --one-file-system "$TARGET"
sha256sum "$TARGET/.BUILDING" "$TARGET/.release-commit"
if [[ -f "$TARGET/.release-tree.sha256" && ! -L "$TARGET/.release-tree.sha256" ]]; then
  sha256sum "$TARGET/.release-tree.sha256"
fi
printf 'CURRENT_BEFORE=%s\n' "$current"

rm -rf --one-file-system -- "$TARGET"

[[ ! -e "$TARGET" && ! -L "$TARGET" ]] || \
  die "BLOCKED_REMOVAL_INCOMPLETE" "$TARGET still exists"
[[ "$(readlink -f -- "$CURRENT_LINK")" == "$EXPECTED_CURRENT" ]] || \
  die "BLOCKED_CURRENT_MISMATCH" "current changed during cleanup"

find "$RELEASES_ROOT" -mindepth 1 -maxdepth 1 \
  ! -name "$TARGET_BASENAME" -printf '%f|%i|%s|%T@\n' | LC_ALL=C sort \
  > "$other_releases_after"
cmp -s "$other_releases_before" "$other_releases_after" || \
  die "BLOCKED_UNEXPECTED_RELEASE_CHANGE" "another release changed"

printf 'CURRENT_AFTER=%s\n' "$(readlink -f -- "$CURRENT_LINK")"
printf '[RESULT] ORPHAN_BUILDING_RELEASE_REMOVED\n'
