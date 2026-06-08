#!/usr/bin/env bash
# Publish a shared conversation as a PUBLIC link — WITHOUT exposing the agent.
#
# Renders the frozen share snapshot to a single self-contained .html
# (app.packaging.shared_chat) and uploads it as a SECRET GitHub gist. "Secret" means the gist is
# unlisted (not on your profile); its unguessable id is the credential, exactly like the share
# token. The agent/app is never reachable — only a read-only, frozen file on GitHub's CDN is.
#
# Usage:
#   scripts/publish_shared_chat.sh <token>            Publish; prints the public render URL.
#   scripts/publish_shared_chat.sh --revoke <token>   Delete the gist published for <token>.
#   scripts/publish_shared_chat.sh --dry-run <token>  Render + show what WOULD run; upload nothing.
#   scripts/publish_shared_chat.sh --help
#
# <token> is the 32-hex share token from the app's 🔗 Share dialog (the snapshot must already be
# shared, so the token exists in the workspace). Requires `gh` (authenticated): `gh auth status`.
#
# GitHub serves raw gist files as text/plain, so the rendered HTML is viewed through a static
# render proxy. We print the canonical raw URL host-swapped to gist.githack.com (primary) AND an
# htmlpreview.github.io fallback — both derived from the raw URL GitHub itself reports.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^#\s\{0,1\}//'
}

die() { echo "error: $1" >&2; exit "${2:-1}"; }

DRY_RUN=0
REVOKE=0
TOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --revoke)  REVOKE=1 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; TOKEN="${1:-$TOKEN}" ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)  TOKEN="$1" ;;
  esac
  shift
done

[ -n "$TOKEN" ] || { echo "error: a share token is required" >&2; usage >&2; exit 2; }
# Accept either a bare token OR a pasted ".../share/<token>" link (then drop any trailing /?#).
TOKEN="${TOKEN##*/share/}"
TOKEN="${TOKEN%%[/?#]*}"
printf '%s' "$TOKEN" | grep -Eq '^[0-9a-f]{32}$' \
  || die "'$TOKEN' is not a valid share token (expected the 32-hex token, or a copied /share/<token> link)" 2

# Remember which gist holds which token (for a clean --revoke), under the SAME workspace the app
# uses — resolved via the app's own settings so it matches a running app / any WORKSPACE_DIR.
WORKSPACE="$(PYTHONPATH="$PROJECT_ROOT" "$PYTHON" -c \
  'from app.config import get_settings; print(get_settings().resolved_workspace_dir)')"
MAP_DIR="$WORKSPACE/shares"
MAP_FILE="$MAP_DIR/$TOKEN.gist"

need_gh() { command -v gh >/dev/null 2>&1 || die "the GitHub CLI 'gh' is required (see https://cli.github.com); then run 'gh auth login'"; }

# ── revoke ────────────────────────────────────────────────────────────────────────────────────
if [ "$REVOKE" = 1 ]; then
  [ -f "$MAP_FILE" ] || die "no published gist recorded for token $TOKEN (nothing to revoke)"
  GIST_ID="$(cat "$MAP_FILE")"
  if [ "$DRY_RUN" = 1 ]; then
    echo "+ gh gist delete $GIST_ID"
    echo "(dry run — nothing deleted)"
    exit 0
  fi
  need_gh
  gh gist delete "$GIST_ID"
  rm -f "$MAP_FILE"
  echo "revoked: gist $GIST_ID deleted; the public link no longer works."
  exit 0
fi

# ── publish ───────────────────────────────────────────────────────────────────────────────────
# 1) Render the self-contained file (named chat.html so the gist file + URL are tidy).
TMP_DIR="$(mktemp -d -t llmd-share-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT
HTML="$TMP_DIR/chat.html"
PYTHONPATH="$PROJECT_ROOT" "$PYTHON" -m app.packaging.shared_chat "$TOKEN" -o "$HTML" >/dev/null \
  || die "could not render the snapshot — is '$TOKEN' a shared conversation in this workspace?"

if [ "$DRY_RUN" = 1 ]; then
  echo "+ gh gist create --desc \"llm-d shared chat $TOKEN\" $HTML   # secret (unlisted) by default"
  echo "(dry run — rendered $(wc -c < "$HTML" | tr -d ' ') bytes; nothing uploaded)"
  exit 0
fi

# 2) Upload as a SECRET gist (gh defaults to secret; --public would make it listed). chat.html is
#    the gist filename. gh prints the gist web URL; its last path segment is the gist id.
need_gh
GIST_URL="$(gh gist create --desc "llm-d shared chat $TOKEN" "$HTML")"
GIST_ID="${GIST_URL##*/}"

# 3) Canonical raw URL GitHub serves the file at → host-swap to a render proxy (+ a fallback).
RAW_URL="$(gh api "gists/$GIST_ID" --jq '[.files[].raw_url][0]')"
GITHACK_URL="${RAW_URL/gist.githubusercontent.com/gist.githack.com}"
HTMLPREVIEW_URL="https://htmlpreview.github.io/?$RAW_URL"

mkdir -p "$MAP_DIR"
printf '%s\n' "$GIST_ID" > "$MAP_FILE"

cat <<EOF
Published a read-only public link (secret gist $GIST_ID — unlisted, agent NOT exposed):

  $GITHACK_URL

If that proxy ever misbehaves, this fallback renders the same file:
  $HTMLPREVIEW_URL

Revoke any time with:
  scripts/publish_shared_chat.sh --revoke $TOKEN
EOF
