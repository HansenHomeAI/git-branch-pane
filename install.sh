#!/usr/bin/env sh
set -eu

REPO_URL="${GBP_REPO_URL:-https://github.com/HansenHomeAI/git-branch-pane.git}"
REF="${GBP_REF:-main}"
APP_DIR="${GBP_APP_DIR:-$HOME/.local/share/git-branch-pane}"
SOURCE_DIR="${GBP_SOURCE_DIR:-$APP_DIR/source}"
BIN_DIR="${GBP_BIN_DIR:-$HOME/.local/bin}"
TARGET_REPO="${GBP_TARGET_REPO:-$(pwd)}"
HOST="${GBP_HOST:-127.0.0.1}"
PORT="${GBP_PORT:-8765}"

if [ "$#" -gt 0 ]; then
  TARGET_REPO="$1"
fi

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need git
need python3

mkdir -p "$APP_DIR" "$BIN_DIR"

if [ -d "$SOURCE_DIR/.git" ]; then
  git -C "$SOURCE_DIR" remote set-url origin "$REPO_URL" >/dev/null 2>&1 || true
  git -C "$SOURCE_DIR" fetch --depth 1 origin "$REF"
  git -C "$SOURCE_DIR" checkout -B "$REF" "origin/$REF"
else
  if [ -e "$SOURCE_DIR" ]; then
    mv "$SOURCE_DIR" "$SOURCE_DIR.backup.$(date +%s)"
  fi
  git clone --depth 1 --branch "$REF" "$REPO_URL" "$SOURCE_DIR"
fi

"$SOURCE_DIR/scripts/install-gbp"

if [ "${GBP_NO_RUN:-0}" = "1" ]; then
  exit 0
fi

echo
echo "Starting Git Branch Pane for: $TARGET_REPO"
exec "$BIN_DIR/gbp" "$TARGET_REPO" --host "$HOST" --port "$PORT"
