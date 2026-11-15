"""Run C's offline regression and CLI integration checks without modifying fixtures."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-tests", action="store_true", help="Run the entire repository test suite")
    args = parser.parse_args()
    tests = [] if args.all_tests else [
        str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_planning*.py"))
    ]
    if not args.all_tests and not tests:
        parser.error("No C planning regression tests found")
    run("-m", "pytest", "-q", *tests)
    run("-m", "quickcut.planning", "selftest")
    with tempfile.TemporaryDirectory(prefix="quickcut-c-review-") as temporary:
        output = Path(temporary)
        for name, options in (
            ("rule", ["--mode", "rule"]),
            ("fixed", ["--mode", "fixed"]),
            ("insufficient", ["--case", "insufficient"]),
        ):
            case = output / name
            run("-m", "quickcut.planning", "run", *options, "--out-dir", str(case))
            run("-m", "quickcut.planning", "validate", str(case / "story_plan.json"),
                "--bundle", str(case / "bundle.json"))
        run("-m", "quickcut.planning", "inject", "--output", str(output / "injected_bundle.json"))
    print("C planning offline review passed; real-model and media acceptance remain separate.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
