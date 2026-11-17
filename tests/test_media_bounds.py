"""Missing container stream durations must be measured from decoded media."""
import json

import pytest

from quickcut import media


def test_audio_tail_uses_decoded_samples_instead_of_padded_packet_duration(tmp_path, monkeypatch):
    frames = [{"best_effort_timestamp": 0, "nb_samples": 960},
              {"best_effort_timestamp": 20, "nb_samples": 120, "duration_time": "0.020"}]
    monkeypatch.setattr(media, "run_media", lambda command: json.dumps({"frames": frames}).encode())
    measured = media.decoded_stream_bounds("ffprobe", tmp_path / "audio.webm",
        {"index": 1, "time_base": "1/1000", "codec_type": "audio", "sample_rate": "48000"})
    assert measured["start_us"] == 0
    assert measured["end_us"] == 22_500


@pytest.mark.parametrize("frames", [
    [{"best_effort_timestamp": 0}, {"best_effort_timestamp": 40}],
    [{"best_effort_timestamp": 40}, {"best_effort_timestamp": 0, "duration_time": "0.04"}],
    [{"best_effort_timestamp": 0}, {"duration_time": "0.04"}],
])
def test_unmeasurable_decoded_tail_fails_closed(tmp_path, monkeypatch, frames):
    monkeypatch.setattr(media, "run_media", lambda command: json.dumps({"frames": frames}).encode())
    with pytest.raises(media.MediaError, match="Cannot measure decoded video"):
        media.decoded_stream_bounds("ffprobe", tmp_path / "unknown.webm",
            {"index": 0, "time_base": "1/1000", "codec_type": "video", "duration": "999"})
