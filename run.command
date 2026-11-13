#!/bin/bash
cd "$(dirname "$0")" || exit 1
PYTHON=/opt/homebrew/bin/python3.12
[ -x "$PYTHON" ] || PYTHON=/opt/homebrew/bin/python3
[ -x "$PYTHON" ] || PYTHON=python3
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv || exit 1
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt || exit 1
fi
exec .venv/bin/python app.py
