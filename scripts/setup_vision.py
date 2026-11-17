"""Set up the isolated, pinned B vision runtime; --check is offline and read-only.

No model inference is performed. The deployment manifest describes installation
readiness, separately from an actual CandidateSet produced by the model.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.11.15"
VISION_PYTHON = PROJECT_ROOT / ".venv-vision/bin/python"
# Runtime dependencies from the validated isolated environment, including
# transitive packages. Test-only dependencies are deliberately omitted.
PACKAGES = dict(item.split("==") for item in """
certifi==2026.10.23 charset-normalizer==3.5.2 filelock==4.2.10
fsspec==2026.9.0 ftfy==6.3.1 hf-xet==1.8.0 huggingface-hub==0.36.2
idna==3.20 jinja2==3.1.6 markupsafe==3.0.4 mpmath==1.3.0
networkx==3.6.1 numpy==2.4.6 open-clip-torch==2.32.0 packaging==26.3
pillow==13.0.0 pyyaml==6.0.3 regex==2026.11.15 requests==2.34.2
safetensors==0.8.0 sentencepiece==0.2.3 sympy==1.14.0 timm==1.0.31
tokenizers==0.22.2 torch==2.8.0 torchvision==0.23.0 tqdm==4.70.1
transformers==4.57.6 typing-extensions==4.16.0 urllib3==2.8.0 wcwidth==0.9.4
""".split())

# Immutable HF revisions and the SHA-256 of every allowed file. The main model's
# hash is also its upstream LFS object ID. No redundant text-tower weights are
# needed: the complete CLIP checkpoint already includes that tower.
MODELS = (
    ("visheratin/nllb-clip-base-oc", "f16398ba58d1d5cff27450ce2fe971b087969444", {
        "open_clip_config.json": (548, "e13a98be8b81cb8319527e4839c4b60446a251f7f4e7e53b597c0c25d10bd344"),
        "open_clip_pytorch_model.bin": (2007675592, "55f4101efcb112643c0b62e650dd891dbf7f0b944a587bfd2e2c8e27663f098f"),
    }),
    ("facebook/nllb-200-distilled-600M", "f8d333a098d19b4fd9a8b18f94170487ad3f821d", {
        "config.json": (846, "f9b4081d3d108e9d06532ea8d92b932c04ed3a15ff2f87e6982f8e14db51fbc5"),
        "sentencepiece.bpe.model": (4852054, "14bb8dfb35c0ffdea7bc01e56cea38b9e3d5efcdcb9c251d6b40538e1aab555a"),
        "special_tokens_map.json": (3548, "992bd4ed610d644d6823081937bcc91bb8878dd556cea4ae5327f2480361330e"),
        "tokenizer.json": (17331176, "e316b82de11d0f951f370943b3c438311629547285129b0b81dadabd01bca665"),
        "tokenizer_config.json": (564, "d1aa8c3697d3e35674f97b5b7e9c99d22b010f528e80140257d97316be90d044"),
    }),
)


def run(*args, capture=False):
    result = subprocess.run([str(arg) for arg in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return result.stdout.strip() if capture else None


def find_uv():
    candidate = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError("Install uv first, then run setup_vision.py again")
    return candidate


@contextmanager
def setup_lock(runtime_root):
    import fcntl

    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / ".setup.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another vision setup is using this directory") from exc
        yield


def inspect_environment(python):
    """Use metadata only: no torch import, model load, cache writes, or network."""
    if not python.is_file():
        return {}, [f"Missing isolated Python: {python}"]
    probe = """
from importlib.metadata import version, PackageNotFoundError
import json, sys
packages = {}
for name in json.loads(sys.argv[1]):
    try:
        packages[name] = version(name)
    except PackageNotFoundError:
        packages[name] = None
print(json.dumps({'python_version': sys.version.split()[0], 'packages': packages,
                  'isolated': sys.prefix != sys.base_prefix}))
"""
    try:
        result = json.loads(run(python, "-I", "-B", "-c", probe, json.dumps(list(PACKAGES)), capture=True))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return {}, [f"Cannot inspect isolated Python: {exc}"]
    errors = []
    if not result.get("isolated"):
        errors.append("Vision Python must be an isolated virtual environment")
    if result.get("python_version") != PYTHON_VERSION:
        errors.append(f"Python must be {PYTHON_VERSION}, found {result.get('python_version')}")
    for name, expected in PACKAGES.items():
        actual = result.get("packages", {}).get(name)
        if actual != expected:
            errors.append(f"{name}: expected {expected}, found {actual}")
    return result, errors


def install_environment(skip_install):
    python = VISION_PYTHON
    if python.exists():
        probe = json.loads(run(python, "-I", "-B", "-c",
                               "import json,sys; print(json.dumps([sys.version.split()[0], sys.prefix != sys.base_prefix]))",
                               capture=True))
        if probe != [PYTHON_VERSION, True]:
            raise RuntimeError(f"Existing .venv-vision is not isolated Python {PYTHON_VERSION}; preserve it and inspect manually")
    elif skip_install:
        raise RuntimeError("--skip-install requires an existing .venv-vision")
    else:
        run(find_uv(), "venv", "--python", PYTHON_VERSION, python.parent.parent)
    _, errors = inspect_environment(python)
    if errors and not skip_install:
        run(find_uv(), "pip", "install", "--python", python,
            *(f"{name}=={version}" for name, version in PACKAGES.items()))
        _, errors = inspect_environment(python)
    if errors:
        raise RuntimeError("; ".join(errors))
    return python


def model_snapshot(runtime_root, repo, revision):
    return runtime_root / "hf-cache" / ("models--" + repo.replace("/", "--")) / "snapshots" / revision


def verify_file(path, expected):
    size, expected_hash = expected
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest() == expected_hash
    except OSError:
        return False


def download_assets(runtime_root):
    """Reuse verified snapshots; ask HF only for missing or corrupt files."""
    missing = []
    for repo, revision, files in MODELS:
        snapshot = model_snapshot(runtime_root, repo, revision)
        for filename, expected in files.items():
            if not verify_file(snapshot / filename, expected):
                missing.append((repo, revision, filename, expected))
    print(f"Model files to download or repair: {len(missing)}", flush=True)
    if not missing:
        return
    needed = sum(expected[0] for _, _, _, expected in missing)
    if shutil.disk_usage(runtime_root).free < needed + 256 * 1024**2:
        raise RuntimeError(f"Insufficient disk space for {needed:,} bytes of model files")
    # Import only after the verified-cache fast path; offline reuse never calls HF.
    from huggingface_hub import hf_hub_download

    for repo, revision, filename, expected in missing:
        target = model_snapshot(runtime_root, repo, revision) / filename
        options = dict(repo_id=repo, revision=revision, filename=filename,
                       cache_dir=str(runtime_root / "hf-cache"), force_download=target.exists())
        for attempt in range(2):
            downloaded = Path(hf_hub_download(**options))
            if verify_file(downloaded, expected) and verify_file(target, expected):
                break
            options["force_download"] = True
        else:
            raise RuntimeError(f"Integrity check failed after forced retry: {repo}/{filename}")


def deployment_report(runtime_root, python):
    inspected, errors = inspect_environment(python)
    models, all_files = [], []
    for repo, revision, expected_files in MODELS:
        snapshot = model_snapshot(runtime_root, repo, revision)
        files = {}
        for filename, expected in expected_files.items():
            path = snapshot / filename
            files[filename] = {"path": str(path), "bytes": expected[0], "sha256": expected[1]}
            all_files.append(files[filename])
            if not verify_file(path, expected):
                errors.append(f"Missing or corrupt model file: {path}")
        models.append({"repo": repo, "revision": revision, "allow": list(expected_files),
                       "snapshot": str(snapshot), "files": files})
    environment = {
        "QUICKCUT_ENCODER": "openclip", "QUICKCUT_CLIP_MODEL": "nllb-clip-base",
        "QUICKCUT_CLIP_PRETRAINED": str(model_snapshot(runtime_root, *MODELS[0][:2]) / "open_clip_pytorch_model.bin"),
        "QUICKCUT_CLIP_MODEL_REVISION": MODELS[0][1],
        "QUICKCUT_CLIP_TEXT_MODEL": str(model_snapshot(runtime_root, *MODELS[1][:2])),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }
    return {
        "schema_version": 1, "status": "not_ready" if errors else "ready",
        "created_at": datetime.now(timezone.utc).isoformat(), "python": str(python),
        "python_version": inspected.get("python_version"), "packages": inspected.get("packages", {}),
        "files": all_files, "models": models, "environment": environment,
        "total_model_bytes": sum(item["bytes"] for item in all_files), "errors": errors,
        "inference_run": False,
        "validation": "Pinned environment and every model file checked; model inference is not performed by setup.",
    }


def write_manifest(runtime_root, document):
    path = runtime_root / "deployment-manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_deployment(runtime_root, python):
    report = deployment_report(runtime_root, python)
    try:
        existing = json.loads((runtime_root / "deployment-manifest.json").read_text(encoding="utf-8"))
        if existing.get("status") != "ready":
            report["errors"].append("Saved deployment manifest is not ready")
        for key in ("python", "files", "models", "environment"):
            if existing.get(key) != report[key]:
                report["errors"].append(f"Saved deployment manifest does not match pinned {key}")
    except (OSError, ValueError, AttributeError) as exc:
        report["errors"].append(f"Missing or invalid deployment manifest: {exc}")
    report["status"] = "not_ready" if report["errors"] else "ready"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=PROJECT_ROOT / "outputs/vision-runtime")
    parser.add_argument("--check", action="store_true", help="Offline, read-only package/hash/manifest verification; no model load")
    parser.add_argument("--skip-install", action="store_true", help="Keep the existing environment; verify/download pinned model files")
    parser.add_argument("--assets-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.runtime_root.expanduser().resolve()
    try:
        if args.check:
            report = check_deployment(root, VISION_PYTHON)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "ready" else 1
        if args.assets_only:
            download_assets(root)
            return 0
        with setup_lock(root):
            # Invalidate a prior ready marker before touching dependencies/assets.
            write_manifest(root, {"status": "installing", "inference_run": False})
            try:
                python = install_environment(args.skip_install)
                run(python, "-B", Path(__file__).resolve(), "--runtime-root", root, "--assets-only")
                report = deployment_report(root, python)
                if report["status"] != "ready":
                    raise RuntimeError("; ".join(report["errors"]))
                write_manifest(root, report)
                print(f"Verified deployment manifest: {root / 'deployment-manifest.json'}")
            except Exception as exc:
                write_manifest(root, {"status": "error", "errors": [str(exc)], "inference_run": False})
                raise
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Vision setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
