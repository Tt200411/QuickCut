"""Vision deployment integrity tests with tiny assets; no packages/models fetched."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import setup_vision as setup


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    contents = {
        "open_clip_pytorch_model.bin": b"verified vision weights",
        "config.json": b"verified text config",
    }
    models = tuple((repo, revision, {name: (len(contents[name]), hashlib.sha256(contents[name]).hexdigest())})
                   for repo, revision, name in (
                       ("test/vision", "a" * 40, "open_clip_pytorch_model.bin"),
                       ("test/text", "b" * 40, "config.json")))
    monkeypatch.setattr(setup, "MODELS", models)
    monkeypatch.setattr(setup, "inspect_environment", lambda python: (
        {"python_version": setup.PYTHON_VERSION, "packages": dict(setup.PACKAGES)}, []))
    for repo, revision, files in models:
        for name in files:
            path = setup.model_snapshot(tmp_path, repo, revision) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents[name])
    setup.write_manifest(tmp_path, setup.deployment_report(tmp_path, setup.VISION_PYTHON))
    return tmp_path, contents


def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def test_check_is_read_only_and_does_not_load_or_download_models(deployment, monkeypatch, capsys):
    root, _ = deployment
    before = snapshot(root)
    monkeypatch.setattr(setup, "install_environment", lambda *args: pytest.fail("check must not install"))
    monkeypatch.setattr(setup, "download_assets", lambda *args: pytest.fail("check must not download"))
    assert setup.main(["--check", "--runtime-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["inference_run"] is False
    assert snapshot(root) == before


def test_check_detects_same_length_corruption_without_repair(deployment, capsys):
    root, _ = deployment
    target = setup.model_snapshot(root, *setup.MODELS[0][:2]) / "open_clip_pytorch_model.bin"
    target.write_bytes(b"x" * target.stat().st_size)
    before = snapshot(root)
    assert setup.main(["--check", "--runtime-root", str(root)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_ready"
    assert any("corrupt model file" in error for error in report["errors"])
    assert snapshot(root) == before


def test_check_missing_directory_never_creates_it(deployment, capsys):
    root, _ = deployment
    missing = root / "uninstalled"
    assert setup.main(["--check", "--runtime-root", str(missing)]) == 1
    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "not_ready"


def test_check_rejects_ready_manifest_with_tampered_paths(deployment):
    root, _ = deployment
    manifest = root / "deployment-manifest.json"
    saved = json.loads(manifest.read_text())
    saved["environment"]["QUICKCUT_CLIP_PRETRAINED"] = "/wrong/weights.bin"
    manifest.write_text(json.dumps(saved))
    report = setup.check_deployment(root, setup.VISION_PYTHON)
    assert report["status"] == "not_ready"
    assert any("pinned environment" in error for error in report["errors"])


def test_check_rejects_saved_revision_metadata_drift(deployment):
    root, _ = deployment
    path = root / "deployment-manifest.json"
    saved = json.loads(path.read_text())
    saved["models"][0]["revision"] = "unreviewed-revision"
    path.write_text(json.dumps(saved))
    report = setup.check_deployment(root, setup.VISION_PYTHON)
    assert report["status"] == "not_ready"
    assert any("pinned models" in error for error in report["errors"])


def test_complete_cache_reused_without_even_importing_hub(deployment, monkeypatch):
    root, _ = deployment
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    before = snapshot(root)
    setup.download_assets(root)
    assert snapshot(root) == before


def test_corrupt_hf_cache_forces_bounded_retry(deployment, monkeypatch):
    root, contents = deployment
    target = setup.model_snapshot(root, *setup.MODELS[0][:2]) / "open_clip_pytorch_model.bin"
    target.unlink()
    calls = []

    def download(**options):
        calls.append(dict(options))
        # A missing snapshot may still resolve to a corrupt completed HF cache.
        target.write_bytes(contents[options["filename"]] if options["force_download"] else b"corrupt cached download")
        return str(target)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=download))
    setup.download_assets(root)
    assert [call["force_download"] for call in calls] == [False, True]
    assert all(call["revision"] == setup.MODELS[0][1] for call in calls)
    setup.download_assets(root)
    assert len(calls) == 2


def test_failed_download_does_not_leave_ready_manifest(deployment, monkeypatch):
    root, _ = deployment
    target = setup.model_snapshot(root, *setup.MODELS[0][:2]) / "open_clip_pytorch_model.bin"
    target.write_bytes(b"bad")
    calls = []

    def download(**options):
        calls.append(options)
        return str(target)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=download))
    monkeypatch.setattr(setup, "install_environment", lambda skip: setup.VISION_PYTHON)
    monkeypatch.setattr(setup, "run", lambda *args, **kwargs: setup.download_assets(root))
    assert setup.main(["--runtime-root", str(root), "--skip-install"]) == 1
    assert len(calls) == 2
    manifest = json.loads((root / "deployment-manifest.json").read_text())
    assert manifest["status"] == "error"
    assert "Integrity check failed" in manifest["errors"][0]


def test_environment_install_failure_invalidates_old_ready_manifest(deployment, monkeypatch):
    root, _ = deployment

    def fail(skip):
        raise RuntimeError("dependency installation failed")

    monkeypatch.setattr(setup, "install_environment", fail)
    assert setup.main(["--runtime-root", str(root)]) == 1
    saved = json.loads((root / "deployment-manifest.json").read_text())
    assert saved["status"] == "error"
    assert saved["errors"] == ["dependency installation failed"]


def test_existing_different_python_is_preserved(tmp_path, monkeypatch):
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.write_text("existing runtime")
    monkeypatch.setattr(setup, "VISION_PYTHON", python)
    monkeypatch.setattr(setup, "run", lambda *args, **kwargs: json.dumps(["3.14.0", True]))
    monkeypatch.setattr(setup, "find_uv", lambda: pytest.fail("must not mutate incompatible environment"))
    with pytest.raises(RuntimeError, match="preserve it"):
        setup.install_environment(False)
    assert python.read_text() == "existing runtime"


def test_package_probe_reports_missing_and_drifted_versions(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.touch()
    installed = dict(setup.PACKAGES, torch="wrong", transformers=None)
    monkeypatch.setattr(setup, "run", lambda *args, **kwargs: json.dumps({
        "python_version": setup.PYTHON_VERSION, "packages": installed, "isolated": True}))
    _, errors = setup.inspect_environment(python)
    assert len(errors) == 2
    assert any("torch:" in error for error in errors)
    assert any("transformers:" in error for error in errors)
