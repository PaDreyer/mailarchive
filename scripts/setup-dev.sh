#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dev_venv="$project_root/.venv"
project_spec="$project_root"

if [[ "${1:-}" == "--with-appindicator" ]]; then
  project_spec="$project_root[linux]"
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--with-appindicator]" >&2
  exit 2
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "Tkinter is required. On Debian or Ubuntu, install it with: sudo apt install python3-tk" >&2
  exit 1
fi

python3 -m venv "$dev_venv"
"$dev_venv/bin/python" -m pip install --upgrade pip
"$dev_venv/bin/python" -m pip install -e "$project_spec"

echo "Development environment is ready."
echo "Activate it with: source .venv/bin/activate"
echo "The editable installation runs Python modules directly from: $project_root/src"
