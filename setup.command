#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

choose_python() {
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return
    fi
  done
  return 1
}

PYTHON_BIN="$(choose_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "未找到 Python 3。macOS 建议执行：brew install python@3.12"
  read -r -p "按回车退出"
  exit 1
fi

echo "使用：$($PYTHON_BIN --version)（要求 Python 3.10+）"
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install --upgrade -e .
if [ -z "${GATESCOPE_BOOTSTRAP_PASSWORD:-}" ]; then
  gatescope-admin create-owner --username "${GATESCOPE_BOOTSTRAP_USERNAME:-owner}"
fi
python app.py
