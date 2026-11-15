#!/usr/bin/env python3
"""Export frozen A/D/E inputs as a relocatable editable Jianying package."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut.export import ExportError, export_project


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("timeline", "manifest", "voice-package", "project-root", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--name")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--allow-fixtures", action="store_true", help="仅测试；标记将保留，不宣称生产交付")
    parser.add_argument("--target-version", default="6.1.0")
    args = parser.parse_args(argv)
    try:
        docs = [json.loads(p.read_text(encoding="utf-8")) for p in (args.timeline, args.manifest, args.voice_package)]
        result = export_project(*docs, args.project_root, args.output, preview=args.preview,
                                allow_fixtures=args.allow_fixtures, target_version=args.target_version, name=args.name)
    except ExportError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "stage": "export", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
