"""Installer integrity checks using tiny assets, without network/model downloads."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import setup_indextts2 as setup


def fake_codec_hub(monkeypatch, *, corrupt_on_force=False):
    payload = b"verified codec weights"
    remote = "semantic_codec/model.safetensors"
    destination = "hf_cache/semantic_codec_model.safetensors"
    sibling = SimpleNamespace(
        rfilename=remote, size=len(payload), blob_id=None,
        lfs=SimpleNamespace(sha256=hashlib.sha256(payload).hexdigest()),
    )
    calls = []

    def download(**options):
        calls.append(dict(options))
        path = Path(options["local_dir"]) / options["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or options["force_download"]:
            path.write_bytes(b"still corrupt" if corrupt_on_force else payload)
        return str(path)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda: SimpleNamespace(model_info=lambda *args, **kwargs: SimpleNamespace(
            sha="test-revision", siblings=[sibling],
        )),
        hf_hub_download=download,
    ))
    monkeypatch.setattr(metadata, "version", lambda name: "2.0.0")
    monkeypatch.setattr(setup, "MODEL_FILES", (
        ("test/codec", "test-revision", {remote: destination}),
    ))
    return payload, remote, destination, calls


def test_missing_codec_target_retries_corrupt_completed_staging(tmp_path, monkeypatch):
    payload, remote, destination, calls = fake_codec_hub(monkeypatch)
    stale = tmp_path / "checkpoints/hf_cache/.downloads" / remote
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale corrupt cache")

    setup.download_assets(tmp_path)

    assert [call["force_download"] for call in calls] == [False, True]
    assert (tmp_path / "checkpoints" / destination).read_bytes() == payload
    manifest = json.loads((tmp_path / "deployment-manifest.json").read_text())
    assert manifest["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    setup.download_assets(tmp_path)
    assert len(calls) == 2  # A verified installation is reusable without downloads.


def test_forced_retry_is_bounded_and_does_not_publish_bad_weights(tmp_path, monkeypatch):
    _, remote, destination, calls = fake_codec_hub(monkeypatch, corrupt_on_force=True)
    stale = tmp_path / "checkpoints/hf_cache/.downloads" / remote
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale corrupt cache")

    with pytest.raises(RuntimeError, match="after a forced retry"):
        setup.download_assets(tmp_path)

    assert [call["force_download"] for call in calls] == [False, True]
    assert not (tmp_path / "checkpoints" / destination).exists()
    assert not (tmp_path / "deployment-manifest.json").exists()


def test_corrupt_deployed_codec_is_replaced_then_reused(tmp_path, monkeypatch):
    payload, _, destination, calls = fake_codec_hub(monkeypatch)
    target = tmp_path / "checkpoints" / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupted installed weights")

    setup.download_assets(tmp_path)

    assert [call["force_download"] for call in calls] == [True]
    assert target.read_bytes() == payload
    setup.download_assets(tmp_path)
    assert len(calls) == 1
