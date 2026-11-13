"""Local IndexTTS2 inference with the official Python runtime."""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
from threading import Lock


_INFERENCE_LOCK = Lock()


class IndexTTS2Backend:
    """Load one model lazily per voice package and reuse it across its cues."""

    def __init__(self, config):
        model_dir = config.get("model_dir")
        if not isinstance(model_dir, str) or not model_dir:
            raise ValueError("IndexTTS2 requires model_dir in the voice config")
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.model_revision = config.get("model_revision")
        if not isinstance(self.model_revision, str) or not self.model_revision.strip():
            raise ValueError("IndexTTS2 requires model_revision for cache provenance")
        self.device = config.get("device", "cpu")
        if not isinstance(self.device, str) or not re.fullmatch(r"cpu|mps|cuda(?::\d+)?", self.device):
            raise ValueError("IndexTTS2 device must be cpu, mps, cuda, or cuda:N")
        cache = self.model_dir / "hf_cache"
        self.aux_paths = {
            "w2v_bert": cache / "w2v-bert-2.0",
            "semantic_codec": cache / "semantic_codec_model.safetensors",
            "campplus": cache / "campplus_cn_common.bin",
            "bigvgan": cache / "bigvgan",
        }
        required = [
            *(self.model_dir / name for name in (
                "config.yaml", "gpt.pth", "s2mel.pth", "bpe.model",
                "wav2vec2bert_stats.pt", "feat1.pt", "feat2.pt",
            )),
            self.aux_paths["w2v_bert"] / "config.json",
            self.aux_paths["w2v_bert"] / "preprocessor_config.json",
            self.aux_paths["w2v_bert"] / "model.safetensors",
            self.aux_paths["semantic_codec"],
            self.aux_paths["campplus"],
            self.aux_paths["bigvgan"] / "config.json",
            self.aux_paths["bigvgan"] / "bigvgan_generator.pt",
        ]
        missing = [str(path.relative_to(self.model_dir)) for path in required if not path.is_file()]
        if missing:
            raise ValueError("Missing local IndexTTS2 model files: " + ", ".join(missing))
        try:
            self.runtime_version = version("indextts")
        except PackageNotFoundError as exc:
            raise ValueError("Run QuickCut with the Python environment containing IndexTTS2") from exc
        self.model = None

    def cue_settings(self, config, language):
        if config.get("emotion"):
            raise ValueError("IndexTTS2 uses the reference audio for emotion in this configuration")
        reference = config.get("reference_audio")
        if not isinstance(reference, str) or not reference:
            raise ValueError("IndexTTS2 requires reference_audio in the voice config")
        reference = Path(reference).expanduser().resolve()
        if not reference.is_file() or reference.suffix.lower() != ".wav":
            raise ValueError("IndexTTS2 reference_audio must be an existing WAV file")
        seed = config.get("seed", 42)
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ValueError("IndexTTS2 seed must be an integer from 0 to 4294967295")
        profile = {
            "engine": "indextts2",
            "version": self.runtime_version,
            "model": "IndexTeam/IndexTTS-2",
            "model_revision": self.model_revision,
            "voice": "reference-audio",
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "language": language,
            "device": self.device,
            "seed": seed,
            "local": True,
            "emotion_supported": False,
        }
        return reference, profile

    def synthesize(self, text, output, reference, seed):
        import torch
        from indextts.infer_v2 import IndexTTS2

        device = torch.device(self.device)
        if device.type == "mps" and not torch.backends.mps.is_available():
            raise ValueError("IndexTTS2 MPS is unavailable in this Python environment")
        if device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("IndexTTS2 CUDA is unavailable in this Python environment")
        rng_devices = [] if device.type == "cpu" else [device]
        with _INFERENCE_LOCK, torch.random.fork_rng(devices=rng_devices, device_type=device.type):
            if self.model is None:
                self.model = IndexTTS2(
                    cfg_path=str(self.model_dir / "config.yaml"),
                    model_dir=str(self.model_dir),
                    device=self.device,
                    use_fp16=False,
                    use_cuda_kernel=False,
                    use_qwen_emo=False,
                    aux_paths={name: str(path) for name, path in self.aux_paths.items()},
                )
            torch.random.default_generator.manual_seed(seed)
            if device.type == "mps":
                torch.mps.manual_seed(seed)
            elif device.type == "cuda":
                with torch.cuda.device(device):
                    torch.cuda.manual_seed(seed)
            self.model.infer(spk_audio_prompt=str(reference), text=text, output_path=str(output))
        if not output.is_file():
            raise ValueError("IndexTTS2 returned no WAV file")
