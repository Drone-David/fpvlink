#!/usr/bin/env bash
# Deploy FPVLink to the Pi from git — and verify it actually landed.
#
# Written after a real incident: a hardware-validated fix (the standby-LUT
# gating) was committed only to a worktree branch, never merged to main. Later
# work then scp'd main's copy of the same file to the device, silently undoing
# it. Nothing caught this, because deploys were hand-picked scp's and there was
# no way to ask "does the Pi match git?".
#
# So this script refuses to deploy from an ambiguous state, copies exactly what
# git tracks, and verifies by checksum afterwards.
#
#   scripts/deploy.sh --check     compare device against HEAD, change nothing
#   scripts/deploy.sh             deploy, verify, restart what changed
#   scripts/deploy.sh --force     deploy despite the safety checks
#
# Host override:  FPVLINK_HOST=1.2.3.4 scripts/deploy.sh
set -euo pipefail

# Default to the mDNS name, not a DHCP address: leases move, and this script
# used to point at a stale one. setup/05-network.sh names the box 'fpvlink', so
# fpvlink.local follows it onto any network. If mDNS is unavailable, fall back
# to the service port's fixed address, which no router can change.
HOST="${FPVLINK_HOST:-fpvlink.local}"
FALLBACK_HOST="10.10.10.1"

# -W is milliseconds on macOS (this script runs from the Mac), not seconds.
if [[ -z "${FPVLINK_HOST:-}" ]] && ! ping -c1 -W 1500 "$HOST" >/dev/null 2>&1; then
  if ping -c1 -W 1500 "$FALLBACK_HOST" >/dev/null 2>&1; then
    echo "fpvlink.local did not resolve; using service port $FALLBACK_HOST" >&2
    HOST="$FALLBACK_HOST"
  fi
fi

REMOTE="root@${HOST}"
DEST="/opt/fpvlink"

# Files git tracks but that must NOT be pushed to the device:
#   system/config.json — device-local runtime state (LUT selection, capture
#     flags, output config). The one file where device and repo are MEANT to
#     differ; copying it over would clobber live settings.
#   scratch/ — research junk, and it contains a broken symlink that aborts rsync.
EXCLUDE_RE='^(system/config\.json|scratch/)'

MODE="deploy"; FORCE=0
for a in "$@"; do
  case "$a" in
    --check) MODE="check" ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

# ── Safety checks ────────────────────────────────────────────────────────────
# Each of these corresponds to something that actually went wrong.
problems=0

if [ -n "$(git status --porcelain)" ]; then
  ylw "! working tree is dirty — the device would get code that is in no commit"
  git status --short | sed 's/^/    /'
  problems=$((problems+1))
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if ! git diff --quiet "@{upstream}" HEAD 2>/dev/null; then
  if git rev-parse '@{upstream}' >/dev/null 2>&1; then
    ahead="$(git rev-list --count '@{upstream}'..HEAD)"
    [ "$ahead" -gt 0 ] && {
      ylw "! $branch is $ahead commit(s) ahead of its upstream — push before deploying,"
      ylw "  or the device runs code that exists only on this laptop"
      problems=$((problems+1))
    }
  else
    ylw "! $branch has no upstream — push it, or the device runs unpublished code"
    problems=$((problems+1))
  fi
fi

# THE one that caused the incident: commits stranded on other local branches.
stranded=""
for b in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v "^${branch}$"); do
  n="$(git rev-list --count "${branch}..${b}" 2>/dev/null || echo 0)"
  [ "$n" -gt 0 ] && stranded+="    $b ($n commit(s) not in $branch)\n$(git log --oneline "${branch}..${b}" | sed 's/^/      /')\n"
done
if [ -n "$stranded" ]; then
  ylw "! commits exist on other branches that are NOT in $branch:"
  printf "%b" "$stranded"
  ylw "  merge them first, or the device gets a version missing that work"
  problems=$((problems+1))
fi

if [ "$problems" -gt 0 ] && [ "$MODE" = "deploy" ] && [ "$FORCE" -eq 0 ]; then
  red "refusing to deploy with $problems issue(s) above — re-run with --force to override"
  exit 1
fi

# ── File list ────────────────────────────────────────────────────────────────
LIST="$(mktemp)"; trap 'rm -f "$LIST" "$LOCAL" "$REMOTE_SUMS"' EXIT
git ls-files | grep -vE "$EXCLUDE_RE" > "$LIST"
echo "tracked files to sync: $(wc -l < "$LIST" | tr -d ' ')  (excluding config.json, scratch/)"

if [ "$MODE" = "deploy" ]; then
  # Record the LUT source hash first: the compiled .so is a build artifact and
  # is NOT tracked, so if the source changes the plugin must be rebuilt on the
  # device or source and binary silently diverge.
  lut_before="$(ssh "$REMOTE" "sha256sum $DEST/capture/fpvlut3d.c 2>/dev/null | cut -d' ' -f1" || echo none)"
  changed_before="$(mktemp)"
  ssh "$REMOTE" "cd $DEST && sha256sum \$(cat) 2>/dev/null" < "$LIST" > "$changed_before" || true

  echo "syncing…"
  rsync -a --files-from="$LIST" ./ "$REMOTE:$DEST/"

  lut_after="$(ssh "$REMOTE" "sha256sum $DEST/capture/fpvlut3d.c 2>/dev/null | cut -d' ' -f1" || echo none)"
  if [ "$lut_before" != "$lut_after" ]; then
    ylw "fpvlut3d.c changed — rebuilding the plugin on the device"
    ssh "$REMOTE" "cd $DEST && bash setup/build-lut-plugin.sh" || red "plugin rebuild FAILED — the .so no longer matches its source"
  fi
fi

# ── Verify by checksum, never by assuming the copy worked ────────────────────
LOCAL="$(mktemp)"; REMOTE_SUMS="$(mktemp)"
while read -r f; do
  printf '%s  %s\n' "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$f"
done < "$LIST" | sort -k2 > "$LOCAL"

ssh "$REMOTE" "cd $DEST && while read -r f; do
  if [ -f \"\$f\" ]; then printf '%s  %s\n' \"\$(sha256sum \"\$f\" | cut -d' ' -f1)\" \"\$f\";
  else printf 'MISSING  %s\n' \"\$f\"; fi
done" < "$LIST" | sort -k2 > "$REMOTE_SUMS"

if diff -q "$LOCAL" "$REMOTE_SUMS" >/dev/null; then
  grn "OK: all $(wc -l < "$LOCAL" | tr -d ' ') tracked files on $HOST match $(git rev-parse --short HEAD)"
else
  red "MISMATCH between device and $(git rev-parse --short HEAD):"
  diff "$LOCAL" "$REMOTE_SUMS" | grep -E '^[<>]' | sed 's/^/    /' | head -20
  exit 1
fi

[ "$MODE" = "check" ] && exit 0

# ── Restart only what changed ────────────────────────────────────────────────
# Restarting the display pipeline is not free: repeated rapid restarts have left
# the mpp decoder unable to decode valid input, needing a reboot. So restart it
# only when its own code changed.
changed_after="$(mktemp)"
ssh "$REMOTE" "cd $DEST && sha256sum \$(cat) 2>/dev/null" < "$LIST" > "$changed_after" || true
touched() { ! diff <(grep -E "$1" "$changed_before" || true) <(grep -E "$1" "$changed_after" || true) >/dev/null 2>&1; }

restart=""
touched 'capture/(pipeline|fpvlut3d)' && restart+=" fpvlink-pipeline"
touched '(web/|capture/goggles2|capture/stream_output)' && restart+=" fpvlink"

if [ -n "$restart" ]; then
  echo "restarting:$restart"
  ssh "$REMOTE" "systemctl restart$restart"
  sleep 8
  ssh "$REMOTE" "systemctl is-active$restart"
else
  echo "no service-affecting files changed — nothing to restart"
fi
rm -f "$changed_before" "$changed_after"
