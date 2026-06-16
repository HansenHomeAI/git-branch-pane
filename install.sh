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

python_ok() {
  "$1" ${2:+"$2"} -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

if [ -z "${GBP_PYTHON_EXE:-}" ]; then
  if command -v python3 >/dev/null 2>&1 && python_ok python3 ""; then
    GBP_PYTHON_EXE=python3
    GBP_PYTHON_ARG=
  elif command -v python >/dev/null 2>&1 && python_ok python ""; then
    GBP_PYTHON_EXE=python
    GBP_PYTHON_ARG=
  elif command -v py >/dev/null 2>&1 && python_ok py "-3"; then
    GBP_PYTHON_EXE=py
    GBP_PYTHON_ARG=-3
  else
    echo "Missing required Python 3. Install Python 3.9+ or set GBP_PYTHON_EXE." >&2
    exit 1
  fi
fi
export GBP_PYTHON_EXE
export GBP_PYTHON_ARG

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
echo "Starting persistent Git Branch Pane for: $TARGET_REPO"
"$BIN_DIR/gbp" "$TARGET_REPO" --host "$HOST" --port "$PORT"
