#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$project_root"
exec "$project_root/.venv/bin/python" -m quickcut.workbench --open "$@"
