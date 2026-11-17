"""Job voice snapshots must survive changes to the shared default."""
import pytest

from quickcut.production_pipeline import Runtime, PipelineError, job_runtime
from quickcut.media import write_json


def test_job_uses_frozen_voice_for_preflight_and_synthesis(tmp_path):
    frozen = tmp_path / "voice-config.json"
    write_json(frozen, {"engine": "indextts2", "reference_audio": "captured.wav"})
    runtime = Runtime(voice_config="new-global-default.json")
    captured = job_runtime(tmp_path, {"voice_config": "voice-config.json"}, runtime)
    assert captured.voice_config == str(frozen)
    assert runtime.voice_config == "new-global-default.json"
    assert job_runtime(tmp_path, {}, runtime) is runtime  # Existing jobs remain readable.


def test_missing_captured_voice_cannot_fall_back_to_another_speaker(tmp_path):
    with pytest.raises(PipelineError) as failure:
        job_runtime(tmp_path, {"voice_config": "voice-config.json"}, Runtime())
    assert failure.value.code == "VOICE_CONFIG_MISSING"
    with pytest.raises(ValueError):
        job_runtime(tmp_path, {"voice_config": "../other.json"}, Runtime())
