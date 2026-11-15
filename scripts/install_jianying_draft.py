#!/usr/bin/env python3
"""Install a verified QuickCut package as a new draft while Jianying is closed."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut.export import ExportError, install_jianying_draft


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--draft-root", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--allow-fixtures", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install_jianying_draft(args.package, args.draft_root, name=args.name, allow_fixtures=args.allow_fixtures)
    except ExportError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "stage": "install", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
