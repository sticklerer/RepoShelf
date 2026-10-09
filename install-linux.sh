#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/reposhelf"
APPLICATIONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/applications"
DESKTOP_FILE="$APPLICATIONS_DIR/reposhelf.desktop"

if ! command -v python3 >/dev/null 2>&1; then
  printf 'RepoShelf needs Python 3. Install Python 3 and try again.\n' >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$APPLICATIONS_DIR"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv" || {
    printf 'Could not create a Python environment. Install the Python venv and pip packages for your distribution, then run this installer again.\n' >&2
    exit 1
  }
fi

install -m 644 "$ROOT/server.py" "$APP_DIR/server.py"
install -m 644 "$ROOT/desktop_app.py" "$APP_DIR/desktop_app.py"
install -m 644 "$ROOT/requirements.txt" "$APP_DIR/requirements.txt"
install -m 755 "$ROOT/uninstall-linux.sh" "$APP_DIR/uninstall-linux.sh"
mkdir -p "$APP_DIR/assets"
install -m 644 "$ROOT/assets/reposhelf.svg" "$APP_DIR/assets/reposhelf.svg"
mkdir -p "$APP_DIR/static"
cp -R "$ROOT/static/." "$APP_DIR/static/"
if [[ -f "$ROOT/data/state.json" && ! -e "$APP_DIR/data/state.json" ]]; then
  mkdir -p "$APP_DIR/data"
  install -m 600 "$ROOT/data/state.json" "$APP_DIR/data/state.json"
fi

"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --requirement "$APP_DIR/requirements.txt"

cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=RepoShelf
Comment=Browse and download jailbreak repository packages
Exec="$APP_DIR/venv/bin/python" "$APP_DIR/desktop_app.py"
TryExec=$APP_DIR/venv/bin/python
Icon=$APP_DIR/assets/reposhelf.svg
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
chmod 644 "$DESKTOP_FILE"

printf 'RepoShelf is installed. Launch it from your Applications menu.\n'
