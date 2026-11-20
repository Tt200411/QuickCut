"""Offline catalog, provenance, selection, and narration-attenuation checks."""
import array
import copy
import json
import math
import wave

import pytest

from quickcut import music
from quickcut.media import digest


def tone_catalog(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "tone.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        samples = array.array("h")
        for index in range(48000):
            sample = round(6000 * math.sin(2 * math.pi * 200 * index / 48000))
            samples.extend([sample, sample])
        wav.writeframes(samples.tobytes())
    entry = {"id": "test-tone", "title": "Generated test tone", "artist": "Test fixture",
             "source_url": "https://example.test/generated-tone", "license": "CC0-1.0",
             "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
             "attribution": "Test tone generated for automated audio checks.",
             "filename": path.name, "sha256": digest(path).removeprefix("sha256:"),
             "tags": ["旅行", "travel"], "instrumental": True}
    catalog = root / "catalog.json"
    catalog.write_text(json.dumps({"version": 1, "tracks": [entry]}), encoding="utf-8")
    return catalog


def test_catalog_rejects_unknown_licenses_escaping_paths_and_changed_bytes(tmp_path):
    path = tone_catalog(tmp_path)
    original = json.loads(path.read_text("utf-8"))
    assert len(music.load_catalog(path)) == 1
    for field, value in (("license", "unknown"), ("filename", "../tone.wav"), ("sha256", "0" * 64)):
        changed = copy.deepcopy(original)
        changed["tracks"][0][field] = value
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError):
            music.load_catalog(path)


def test_selection_is_explainable_and_does_not_match_partial_english_words():
    tracks = [{"id": "quiet", "tags": ["art", "文艺"]}, {"id": "family", "tags": ["family"]}]
    track, evidence = music.select_music("family trip", tracks)
    assert track["id"] == "family" and evidence["matched_tags"] == ["family"]
    assert music.select_music("party", tracks)[1]["fallback_to_catalog_default"]
    assert music.select_music("文艺旅行", tracks)[1]["matched_tags"] == ["文艺"]


def test_music_envelope_fades_and_attenuates_before_actual_narration():
    speech = [(8_000_000, 10_000_000)]
    gain = lambda t: music.gain_at(t, 20_000_000, speech)
    assert gain(0) == gain(20_000_000) == 0
    assert 0 < gain(500_000) < gain(5_000_000)
    assert gain(9_000_000) == .06 < gain(7_000_000) == .25
    assert .06 < gain(7_900_000) < .25
    assert .06 < gain(10_200_000) < .25
    assert gain(19_500_000) < gain(15_000_000)


def test_installer_verification_requires_the_installed_catalog_and_original_bytes(tmp_path):
    from scripts.setup_music import install
    source = tone_catalog(tmp_path / "library")
    manifest = tmp_path / "pinned.json"
    manifest.write_bytes(source.read_bytes())
    assert install(manifest, source.parent, verify_only=True)["ready"]
    source.unlink()
    with pytest.raises(ValueError, match="Installed catalog"):
        install(manifest, source.parent, verify_only=True)
    source.write_bytes(manifest.read_bytes())
    (source.parent / "tone.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        install(manifest, source.parent, verify_only=True)


def test_installer_rejects_downloads_outside_the_official_host(tmp_path):
    from scripts.setup_music import install
    source = tone_catalog(tmp_path / "source")
    document = json.loads(source.read_text("utf-8"))
    document["tracks"][0]["download_url"] = "https://example.test/tone.wav"
    source.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="official HTTPS host"):
        install(source, tmp_path / "installed")
