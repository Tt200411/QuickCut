"""Wrapper boundary checks with a fake model; no neural runtime is imported."""
import array
from contextlib import nullcontext
import hashlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import wave

from quickcut.indextts2 import IndexTTS2Backend


def test_actual_wrapper_passes_original_reference_bytes_to_model_infer(tmp_path, monkeypatch):
    reference = tmp_path / "男声.wav"
    with wave.open(str(reference), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(array.array("h", [800] * 2400).tobytes())
    original = reference.read_bytes()
    initializations, calls, seeds = [], [], []

    class Model:
        def __init__(self, **kwargs):
            initializations.append(kwargs)

        def infer(self, **kwargs):
            calls.append({**kwargs, "reference_bytes": Path(kwargs["spk_audio_prompt"]).read_bytes()})
            Path(kwargs["output_path"]).write_bytes(b"fake generated output; no model executed")

    torch = ModuleType("torch")
    torch.device = lambda name: SimpleNamespace(type=name)
    torch.random = SimpleNamespace(fork_rng=lambda **kwargs: nullcontext(),
                                   default_generator=SimpleNamespace(manual_seed=seeds.append))
    package = ModuleType("indextts")
    package.__path__ = []
    inference = ModuleType("indextts.infer_v2")
    inference.IndexTTS2 = Model
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "indextts", package)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2", inference)

    backend = object.__new__(IndexTTS2Backend)
    backend.device = "cpu"
    backend.model_dir = tmp_path / "models"
    backend.aux_paths = {}
    backend.model = None
    backend.runtime_version = "test-runtime"
    backend.model_revision = "fixed-model-revision"
    selected, profile = backend.cue_settings({"reference_audio": str(reference), "seed": 42}, "zh-CN")
    for index, text in enumerate(("第一句", "第二句")):
        backend.synthesize(text, tmp_path / f"generated-{index}.wav", selected, profile["seed"])

    assert len(initializations) == 1
    assert seeds == [42, 42]
    assert [call["text"] for call in calls] == ["第一句", "第二句"]
    assert all(call["spk_audio_prompt"] == str(reference.resolve()) for call in calls)
    assert all(call["reference_bytes"] == original for call in calls)
    assert profile["reference_sha256"] == hashlib.sha256(original).hexdigest()
    assert reference.read_bytes() == original
