#!/usr/bin/env bash
# Install ansreview on macOS into an isolated virtualenv.
#
# Works around a broken global Python (e.g. Homebrew site-packages with a stale
# __editable__*.pth or sitecustomize that breaks imports like `json`): every
# Python is probed in isolated mode (-I), and ansreview lives in its own venv,
# so nothing in the global site-packages is loaded.
#
# Usage (from the repo root, or anywhere):
#   ./scripts/install-macos.sh                 # install, link ~/.local/bin/ansreview
#   ./scripts/install-macos.sh --host http://gpu-box:11434   # also run `ansreview doctor`
#   ./scripts/install-macos.sh --fix-site      # also move broken .pth/sitecustomize aside
#   ./scripts/install-macos.sh --no-lint       # skip ansible-lint
#
# Compatible with the stock macOS bash 3.2.

set -euo pipefail

REPO_URL="git+https://github.com/jwklijnsma/-Ansreview.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${ANSREVIEW_VENV:-$HOME/.local/share/ansreview/venv}"
BIN_DIR="$HOME/.local/bin"
HOST=""
FIX_SITE=0
WITH_LINT=1
LINK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    --fix-site) FIX_SITE=1; shift ;;
    --no-lint) WITH_LINT=0; shift ;;
    --no-link) LINK=0; shift ;;
    --venv) VENV_DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. diagnose the default python3 ------------------------------------------
say "Checking your default python3"
if command -v python3 >/dev/null 2>&1; then
  DEFAULT_PY="$(command -v python3)"
  if python3 -c 'import json' >/dev/null 2>&1; then
    ok "$DEFAULT_PY imports the standard library normally"
  elif python3 -I -c 'import json' >/dev/null 2>&1; then
    warn "$DEFAULT_PY is broken by its startup files (.pth / sitecustomize), not by its install"
    SITE="$(python3 -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"
    if [ -n "$SITE" ] && [ -d "$SITE" ]; then
      SUSPECTS=""
      for f in "$SITE"/__editable__*.pth "$SITE"/sitecustomize.py "$SITE"/usercustomize.py; do
        [ -e "$f" ] && SUSPECTS="$SUSPECTS${f##*/}
"
      done
      if [ -n "$SUSPECTS" ]; then
        echo "     Suspect files in $SITE:"
        printf '%s' "$SUSPECTS" | while IFS= read -r f; do echo "       - $f"; done
        if [ "$FIX_SITE" = 1 ]; then
          BACKUP="$HOME/.local/share/ansreview/site-backup-$(date +%Y%m%d-%H%M%S)"
          mkdir -p "$BACKUP"
          printf '%s' "$SUSPECTS" | while IFS= read -r f; do
            [ -n "$f" ] && mv "$SITE/$f" "$BACKUP/" && echo "       moved $f"
          done
          ok "Moved them to $BACKUP (move back to undo)"
          if python3 -c 'import json' >/dev/null 2>&1; then ok "python3 works normally again"
          else warn "python3 still fails; the venv below works around it anyway"; fi
        else
          echo "     Re-run with --fix-site to move them to a backup folder (reversible)."
          echo "     Not required: ansreview's venv is isolated from them."
        fi
      fi
    fi
  else
    warn "$DEFAULT_PY cannot import json even in isolated mode; its install is damaged"
    echo "     Fix later with: brew reinstall python@$(python3 -I -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 3.x)"
  fi
else
  warn "no python3 on PATH"
fi

# --- 2. pick a healthy Python (3.10-3.14, prefer 3.13/3.12 for ansible-lint) ---
healthy() {
  "$1" -I -c 'import sys, json, venv, ensurepip, ssl, sqlite3; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)' >/dev/null 2>&1
}

find_python() {
  for v in 3.13 3.12 3.11 3.14 3.10; do
    for p in "python$v" "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v" \
             "/opt/homebrew/opt/python@$v/bin/python$v" "/usr/local/opt/python@$v/bin/python$v" \
             "/Library/Frameworks/Python.framework/Versions/$v/bin/python$v"; do
      if command -v "$p" >/dev/null 2>&1 && healthy "$(command -v "$p")"; then
        command -v "$p"; return 0
      fi
    done
  done
  if command -v python3 >/dev/null 2>&1 && healthy "$(command -v python3)"; then
    command -v python3; return 0
  fi
  return 1
}

say "Looking for a healthy Python 3.10-3.14"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    say "None found; installing python@3.13 with Homebrew"
    brew install python@3.13 || die "brew install python@3.13 failed"
    PY="$(find_python || true)"
  fi
  [ -n "$PY" ] || die "No working Python 3.10+ found. Install one: brew install python@3.13"
fi
ok "Using $PY ($("$PY" -I -c 'import sys;print(sys.version.split()[0])'))"

# --- 3. create the venv (isolated: global site-packages never loaded) ----------
say "Creating virtualenv in $VENV_DIR"
mkdir -p "$(dirname "$VENV_DIR")"
"$PY" -I -m venv --clear "$VENV_DIR" || die "venv creation failed"
VPY="$VENV_DIR/bin/python"
"$VPY" -c 'import json' >/dev/null 2>&1 || die "the new venv cannot import json; reinstall that Python"
ok "venv works"

say "Upgrading pip"
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel

# --- 4. install ansreview -------------------------------------------------------
if [ -f "$REPO_ROOT/pyproject.toml" ] && grep -q '^name = "ansreview"' "$REPO_ROOT/pyproject.toml"; then
  SRC="$REPO_ROOT"; MODE="-e"
  say "Installing ansreview from local checkout $REPO_ROOT (editable: git pull updates it)"
else
  SRC="$REPO_URL"; MODE=""
  say "Installing ansreview from $REPO_URL"
fi

install_pkg() {  # $1 = extras suffix, e.g. "[lint]" or ""
  if [ -n "$MODE" ]; then "$VPY" -m pip install --quiet -e "$SRC$1"
  else "$VPY" -m pip install --quiet "ansreview$1 @ $SRC"; fi
}

if [ "$WITH_LINT" = 1 ]; then
  if ! install_pkg "[lint]"; then
    warn "Installing with ansible-lint failed; installing ansreview without it"
    install_pkg "" || die "pip install failed"
    WITH_LINT=0
  fi
else
  install_pkg "" || die "pip install failed"
fi
ok "$("$VENV_DIR/bin/ansreview" --version)"
if [ "$WITH_LINT" = 1 ] && [ -x "$VENV_DIR/bin/ansible-lint" ]; then
  ok "ansible-lint $("$VENV_DIR/bin/ansible-lint" --version 2>/dev/null | head -1 | awk '{print $2}')"
fi

# --- 5. put it on PATH ----------------------------------------------------------
# ansreview calls ansible-lint by name, so link both.
if [ "$LINK" = 1 ]; then
  mkdir -p "$BIN_DIR"
  for tool in ansreview ansible-lint; do
    [ -x "$VENV_DIR/bin/$tool" ] && ln -sf "$VENV_DIR/bin/$tool" "$BIN_DIR/$tool"
  done
  ok "Linked into $BIN_DIR"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      RC="$HOME/.zshrc"
      if ! grep -qs 'HOME/.local/bin' "$RC"; then
        # shellcheck disable=SC2016  # literal $HOME/$PATH are meant for .zshrc
        printf '\n# ansreview\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
        ok "Added ~/.local/bin to PATH in ~/.zshrc (open a new terminal, or: source ~/.zshrc)"
      else
        warn "$BIN_DIR is in ~/.zshrc but not in this shell; run: source ~/.zshrc"
      fi ;;
  esac
fi

# --- 6. optional health check -------------------------------------------------
if [ -n "$HOST" ]; then
  say "Running ansreview doctor against $HOST"
  PATH="$VENV_DIR/bin:$PATH" "$VENV_DIR/bin/ansreview" doctor --host "$HOST" || true
fi

echo
ok "Done. Try:  ansreview doctor --host http://<ollama-host>:11434"
echo "     Update later with: git pull   (editable install)  or re-run this script."
