#!/bin/sh
# Run QuickCut with the isolated IndexTTS2 dependencies and local model assets.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
runtime_root=${QUICKCUT_INDEXTTS2_RUNTIME:-"$project_root/outputs/indextts2-runtime"}
runtime_python="$runtime_root/upstream/.venv/bin/python"

if [ ! -x "$runtime_python" ]; then
    printf '%s\n' "IndexTTS2 is not installed. Run: python3 $project_root/scripts/setup_indextts2.py" >&2
    exit 1
fi

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTORCH_ENABLE_MPS_FALLBACK=1
export PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}"
exec "$runtime_python" "$@"
