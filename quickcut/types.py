from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal


Provenance = Literal["source", "external", "generated"]


@dataclass
class Shot:
    shot_id: str
    start: float
    end: float
    keyframe: str | None = None
    transcript: str = ""
    embedding: list[float] = field(default_factory=list)
    backend: str = "fallback"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TimelineItem:
    item_id: str
    provenance: Provenance
    asset_id: str
    start: float
    end: float
    license_note: str = "Source video provided by user"
    score: float = 0.0
    transcript: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Timeline:
    version: str
    source_video: str
    query: str
    duration_budget: float
    index_id: str
    coverage: Literal["source", "abstain", "broll"]
    items: list[TimelineItem] = field(default_factory=list)
    message: str = ""
    preset: str = "clean"

    @property
    def duration(self) -> float:
        return sum(item.duration for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        data["items"] = [asdict(item) for item in self.items]
        return data


@dataclass
class IndexManifest:
    index_id: str
    source_video: str
    source_size: int
    source_mtime: float
    duration: float
    window: float
    stride: float
    created_at: str
    encoder: str
    shots: list[Shot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["shots"] = [asdict(shot) for shot in self.shots]
        return data
