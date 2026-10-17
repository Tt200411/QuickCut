from __future__ import annotations

from quickcut.types import IndexManifest, Shot, Timeline, TimelineItem
from quickcut.ui import apply_edit, apply_reorder


def make_timeline() -> tuple[Timeline, IndexManifest]:
    timeline = Timeline(
        version="1.0", source_video="source.mp4", query="q", duration_budget=8,
        index_id="cached-index", coverage="source",
        items=[
            TimelineItem("item-000", "source", "shot-0000", 0, 3),
            TimelineItem("item-001", "source", "shot-0001", 3, 6),
        ],
    )
    manifest = IndexManifest(
        index_id="cached-index", source_video="source.mp4", source_size=1,
        source_mtime=0, duration=6, window=3, stride=3, created_at="",
        encoder="fallback", shots=[Shot("shot-0000", 0, 3), Shot("shot-0001", 3, 6)],
    )
    return timeline, manifest


def test_replace_uses_indexed_shot_interval_and_preserves_index():
    timeline, manifest = make_timeline()
    edited = apply_edit(timeline, manifest, "item-000", "shot-0001", 0, 3)
    assert edited.index_id == timeline.index_id
    assert edited.items[0].asset_id == "shot-0001"
    assert (edited.items[0].start, edited.items[0].end) == (3, 6)


def test_reorder_requires_all_items_and_preserves_index():
    timeline, manifest = make_timeline()
    edited = apply_reorder(timeline, "item-001, item-000")
    assert edited.index_id == timeline.index_id
    assert [item.item_id for item in edited.items] == ["item-001", "item-000"]

    try:
        apply_reorder(timeline, "item-001")
    except ValueError as exc:
        assert "each timeline item" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("partial reorder should fail")


def test_trim_routes_through_shared_updater():
    timeline, manifest = make_timeline()
    edited = apply_edit(timeline, manifest, "item-000", trim_start=0.5, trim_end=2.5)
    assert edited.index_id == timeline.index_id
    assert (edited.items[0].start, edited.items[0].end) == (0.5, 2.5)
