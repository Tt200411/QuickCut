from __future__ import annotations

"""Opt-in downloader for user-provided or openly licensed source videos."""

from pathlib import Path

from .core import _require_binary


def download_video(url: str, output_dir: str | Path = "inputs", *, max_duration: int = 900) -> Path:
    """Download one video with yt-dlp, rejecting files longer than the limit.

    The caller owns the URL and the usage rights. No URL is hard-coded into the
    project, which keeps the demo reproducible and avoids silently copying
    copyrighted material.
    """
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Downloader requires yt-dlp; install with: pip install -e '.[download]'") from exc
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    def duration_guard(info: dict) -> None:
        duration = info.get("duration")
        if duration and duration > max_duration:
            raise yt_dlp.utils.DownloadError(f"Video is {duration:.0f}s; max_duration is {max_duration}s")

    template = str(output_dir / "%(id)s.%(ext)s")
    options = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "match_filter": duration_guard,
        "quiet": False,
        "ffmpeg_location": _require_binary("ffmpeg"),
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        requested = Path(downloader.prepare_filename(info))
    merged = requested.with_suffix(".mp4")
    return merged if merged.exists() else requested
