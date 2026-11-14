#!/usr/bin/env python3
"""从 D 时间线及 A/E 素材清单生成 MP4、SRT 和报告；必须提供 ffmpeg 与 ffprobe。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickcut.timeline_preview import PreviewError, render_timeline_preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "媒体工具须位于 PATH，或通过 QUICKCUT_FFMPEG / QUICKCUT_FFPROBE 指定可执行文件。"))
    parser.add_argument("--timeline", required=True, type=Path, help="D 的 EditTimeline 0.1.0 JSON")
    parser.add_argument("--manifest", required=True, type=Path, help="AssetManifest JSON")
    parser.add_argument("--voice-package", required=True, type=Path, help="ready VoicePackage JSON")
    parser.add_argument("--project-root", required=True, type=Path, help="A/E 共享的素材根目录")
    parser.add_argument("--output", required=True, type=Path, help="不存在的 .mp4 输出路径，父目录须存在")
    parser.add_argument("--srt", type=Path, help="独立全局字幕路径，默认与 MP4 同名 .srt")
    parser.add_argument("--report", type=Path, help="报告路径，默认与 MP4 同名 .preview.json")
    args = parser.parse_args(argv)
    try:
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in (
            args.timeline, args.manifest, args.voice_package)]
        report = render_timeline_preview(*documents, args.project_root, args.output,
                                         srt_path=args.srt, report_path=args.report)
    except PreviewError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"预览失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
