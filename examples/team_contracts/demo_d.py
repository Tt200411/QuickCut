"""Run D with contract fixtures and validate the timeline it actually compiles."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quickcut.timeline import TimelineError, compile_timeline

from .validate import validate


FIXTURE = Path(__file__).with_name("bundle.json")
OUTPUT = {"width": 1920, "height": 1080, "fps": {"numerator": 30, "denominator": 1}, "sample_rate": 48_000}
CASES = ("normal", "longer", "retime", "overlong", "stale")


def load_case(case: str) -> dict:
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    docs = {doc["kind"]: doc for doc in bundle["documents"]}
    story, voice = docs["StoryPlan"], docs["VoicePackage"]
    if case == "stale":
        story["revision"] += 1
    if case in {"longer", "retime", "overlong"}:
        cue = next(cue for cue in voice["cues"] if cue["cue_id"] == "cue-02")
        asset = next(asset for asset in voice["assets"] if asset["asset_id"] == cue["asset_id"])
        duration = 9_500_000 if case == "overlong" else 8_800_000
        cue.update(raw_duration_us=duration, duration_us=duration)
        cue["local_subtitles"][0]["range_us"]["end"] = duration
        asset["duration_us"] = duration
        asset["revision"] += 1
        voice["revision"] += 1
        if case == "retime":
            # Leave a 0.5s source handle so D can extend the walking shot.
            story["beats"][1]["shots"][1]["suggested_duration_us"] = 5_500_000
            story["revision"] += 1
            voice["inputs"][0]["revision"] = story["revision"]
        elif case == "overlong":
            cue.update(fit_status="needs_retime", needs_retime=True)
    return bundle


def run_case(bundle: dict) -> dict:
    docs = {doc["kind"]: doc for doc in bundle["documents"]}
    timeline = compile_timeline(
        docs["Brief"], docs["AssetManifest"], docs["StoryPlan"], docs["VoicePackage"],
        output=OUTPUT,
    )
    # The fixture's timeline is replaced with D's computed output before validation.
    # Keep the placeholder export pointing at the computed timeline for the test.
    docs["EditTimeline"] = timeline
    for link in docs["ExportManifest"]["inputs"]:
        if link["kind"] == "EditTimeline":
            link.update(document_id=timeline["document_id"], revision=timeline["revision"])
        if link["kind"] == "VoicePackage":
            link["revision"] = docs["VoicePackage"]["revision"]
    bundle["documents"] = list(docs.values())
    validate(bundle)
    return timeline


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, default="normal")
    parser.add_argument("--output", type=Path, help="Timeline or structured failure JSON")
    parser.add_argument("--bundle-output", type=Path, help="Optional validated bundle for a successful case")
    args = parser.parse_args()
    output = args.output or Path("outputs/d-demo") / f"{args.case}.json"
    bundle = load_case(args.case)
    try:
        result = run_case(bundle)
    except TimelineError as error:
        project_id = next(doc["project_id"] for doc in bundle["documents"] if doc["kind"] == "Brief")
        write_json(output, error.to_dict(project_id))
        print(f"{error.code}: {error}; wrote {output}")
        return 1
    write_json(output, result)
    if args.bundle_output:
        write_json(args.bundle_output, bundle)
    print(f"PASS: compiled and validated {result['duration_us'] / 1_000_000:g}s; wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
