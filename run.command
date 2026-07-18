#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "尚未安装环境，正在执行 setup.command"
  exec ./setup.command
fi
source .venv/bin/activate
exec python app.py
