#!/usr/bin/env bash
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-"$HOME/.local/share"}"
INSTALLED_DIR="$DATA_HOME/reposhelf"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

if [[ "$SCRIPT_DIR" == "$INSTALLED_DIR" ]]; then
  APP_DIR="$SCRIPT_DIR"
else
  APP_DIR="$INSTALLED_DIR"
fi

PURGE_DATA=false
case "${1:-}" in
  "")
    ;;
  --purge-data)
    PURGE_DATA=true
    ;;
  --help|-h)
    printf 'Usage: %s [--purge-data]\n' "$0"
    printf 'Remove the installed RepoShelf app and desktop launcher. Downloaded packages and settings are kept unless --purge-data is specified.\n'
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\nUsage: %s [--purge-data]\n' "$1" "$0" >&2
    exit 2
    ;;
esac

if [[ ! -d "$APP_DIR" ]]; then
  printf 'RepoShelf is not installed in %s.\n' "$APP_DIR" >&2
  exit 1
fi

LOCK_FILE="$APP_DIR/reposhelf.lock"
if [[ -e "$LOCK_FILE" ]]; then
  printf 'Waiting for RepoShelf to close…\n'
  for _ in {1..300}; do
    [[ -e "$LOCK_FILE" ]] || break
    sleep 0.1
  done
  if [[ -e "$LOCK_FILE" ]]; then
    printf 'RepoShelf is still running. Close it from the tray and try again.\n' >&2
    exit 1
  fi
fi

APPLICATIONS_DIR="$DATA_HOME/applications"
rm -f -- "$APPLICATIONS_DIR/reposhelf.desktop"
rm -rf -- "$APP_DIR/venv" "$APP_DIR/static" "$APP_DIR/assets"
rm -f -- \
  "$APP_DIR/desktop_app.py" \
  "$APP_DIR/server.py" \
  "$APP_DIR/requirements.txt" \
  "$APP_DIR/reposhelf.svg" \
  "$APP_DIR/uninstall-linux.sh"

if [[ "$PURGE_DATA" == true ]]; then
  rm -rf -- "$APP_DIR/data"
  printf 'Removed RepoShelf and its saved settings and downloads.\n'
else
  printf 'Removed RepoShelf. Saved settings and downloads remain in %s/data.\n' "$APP_DIR"
fi

rmdir -- "$APP_DIR" 2>/dev/null || true
