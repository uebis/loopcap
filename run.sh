#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Virtual environment not found. Create it with:"
  echo "  python3 -m venv --system-site-packages .venv"
  echo "  . .venv/bin/activate"
  echo "  python -m ensurepip --upgrade"
  exit 1
fi

PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON_BIN" -m fedora_licecap "$@"
