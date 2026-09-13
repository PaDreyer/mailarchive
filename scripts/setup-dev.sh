#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dev_venv="$project_root/.venv"

if [[ "$#" -ne 0 ]]; then
  echo "Usage: $0" >&2
  exit 2
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "Tkinter is required. On Debian or Ubuntu, install it with: sudo apt install python3-tk" >&2
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Python virtual-environment support is required. On Debian or Ubuntu, install it with: sudo apt install python3-venv" >&2
  exit 1
fi

python3 -m venv "$dev_venv"
"$dev_venv/bin/python" -m pip install --upgrade pip
"$dev_venv/bin/python" -m pip install -e "$project_root"

echo "Development environment is ready."
echo "Activate it with: source .venv/bin/activate"
echo "The editable installation runs Python modules directly from: $project_root/src"
