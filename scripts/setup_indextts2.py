"""Install the pinned local IndexTTS2 runtime and verify all required weights.

Run with Python 3.10+; model downloads run in the isolated Python 3.11 environment.
No speaker recording is selected or replaced by this installer.
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
RUNTIME_COMMIT = "d9e41aac89fd00b3d71497fddb287b7f24613712"
WEIGHT_REVISION = "740dcaff396282ffb241903d150ac011cd4b1ede"
UPSTREAM_URL = "https://github.com/index-tts/index-tts.git"
# Each entry is (repository, immutable revision, remote filename -> local filename).
MODEL_FILES = (
    ("IndexTeam/IndexTTS-2", WEIGHT_REVISION, {
        name: name for name in (
            "config.yaml", "gpt.pth", "s2mel.pth", "bpe.model",
            "wav2vec2bert_stats.pt", "feat1.pt", "feat2.pt",
        )
    }),
    ("facebook/w2v-bert-2.0", "da985ba0987f70aaeb84a80f2851cfac8c697a7b", {
        name: f"hf_cache/w2v-bert-2.0/{name}"
        for name in ("config.json", "preprocessor_config.json", "model.safetensors")
    }),
    ("amphion/MaskGCT", "265c6cef07625665d0c28d2faafb1415562379dc", {
        "semantic_codec/model.safetensors": "hf_cache/semantic_codec_model.safetensors",
    }),
    ("funasr/campplus", "e4b6ede7ce16997aff4ae69fbca1f0175e2afede", {
        "campplus_cn_common.bin": "hf_cache/campplus_cn_common.bin",
    }),
    ("nvidia/bigvgan_v2_22khz_80band_256x", "633ff708ed5b74903e86ff1298cf4a98e921c513", {
        name: f"hf_cache/bigvgan/{name}"
        for name in ("config.json", "bigvgan_generator.pt")
    }),
)


def run(*args, capture=False):
    result = subprocess.run(
        [str(arg) for arg in args], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def find_uv():
    candidate = shutil.which("uv")
    if not candidate:
        local = Path.home() / ".local/bin/uv"
        if local.is_file() and os.access(local, os.X_OK):
            candidate = str(local)
    if not candidate:
        raise RuntimeError("uv is required. Install uv, then run this command again.")
    return candidate


@contextmanager
def setup_lock(runtime_root):
    # The deployment target is macOS/Linux; keep repeated installer runs serialized.
    import fcntl

    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / ".setup.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another IndexTTS2 setup is using this runtime directory") from exc
        yield


def verify_runtime(upstream):
    if not (upstream / ".git").is_dir():
        raise RuntimeError(f"Existing runtime is not a Git checkout: {upstream}")
    commit = run("git", "-C", upstream, "rev-parse", "HEAD", capture=True)
    if commit != RUNTIME_COMMIT:
        raise RuntimeError(
            f"Runtime is at {commit}, expected {RUNTIME_COMMIT}; "
            "choose a new --runtime-root to preserve the existing installation"
        )
    dirty = run("git", "-C", upstream, "status", "--porcelain", "--untracked-files=no", capture=True)
    if dirty:
        raise RuntimeError("Runtime has tracked local changes; choose a new --runtime-root")


def install_runtime(runtime_root, skip_install):
    upstream = runtime_root / "upstream"
    if not upstream.exists():
        if skip_install:
            raise RuntimeError("--skip-install requires an existing pinned runtime")
        run("git", "clone", "--no-checkout", UPSTREAM_URL, upstream)
        run("git", "-C", upstream, "checkout", "--detach", RUNTIME_COMMIT)
    verify_runtime(upstream)
    python = upstream / ".venv/bin/python"
    if python.exists():
        installed = run(python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))", capture=True)
        if installed != "3.11":
            raise RuntimeError(f"Existing runtime uses Python {installed}; choose a new --runtime-root")
    if not skip_install:
        uv = find_uv()
        run(uv, "sync", "--locked", "--python", "3.11", "--project", upstream)
        run(uv, "pip", "install", "--python", python, "--no-deps", "-e", PROJECT_ROOT)
        run(uv, "pip", "install", "--python", python, "imageio-ffmpeg", "pillow")
    if not python.is_file():
        raise RuntimeError(f"Missing isolated Python: {python}")
    return python


def file_hashes(path):
    """Compute SHA-256 and Git's blob SHA-1 without reading a whole weight into RAM."""
    size = path.stat().st_size
    sha256 = hashlib.sha256()
    blob_sha1 = hashlib.sha1(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            sha256.update(block)
            blob_sha1.update(block)
    return size, sha256.hexdigest(), blob_sha1.hexdigest()


def verify_file(path, entry):
    if not path.is_file():
        return None
    size, sha256, blob_sha1 = file_hashes(path)
    if size != entry["size"]:
        return None
    expected_lfs = entry.get("sha256")
    if expected_lfs:
        if sha256 != expected_lfs:
            return None
    elif not entry.get("blob_id") or blob_sha1 != entry["blob_id"]:
        return None
    return sha256


def download_assets(runtime_root):
    from huggingface_hub import HfApi, hf_hub_download
    from importlib.metadata import version

    api = HfApi()
    entries = []
    for repo, revision, mapping in MODEL_FILES:
        info = api.model_info(repo, revision=revision, files_metadata=True)
        if info.sha != revision:
            raise RuntimeError(f"Unexpected revision for {repo}: {info.sha}")
        siblings = {item.rfilename: item for item in info.siblings}
        for remote, local in mapping.items():
            item = siblings.get(remote)
            if item is None or item.size is None:
                raise RuntimeError(f"Missing file metadata for {repo}/{remote}")
            entries.append({
                "repository": repo, "revision": revision, "remote_path": remote,
                "path": local, "size": item.size, "blob_id": item.blob_id,
                "sha256": item.lfs.sha256 if item.lfs else None,
            })
    total = sum(entry["size"] for entry in entries)
    model_dir = runtime_root / "checkpoints"
    model_dir.mkdir(parents=True, exist_ok=True)
    print(f"Pinned model assets: {len(entries)} files, {total:,} bytes ({total / 1e9:.2f} GB)", flush=True)
    # Validate every file, including auxiliaries in already-populated directories.
    missing = []
    for entry in entries:
        dest = model_dir / entry["path"]
        actual_sha256 = verify_file(dest, entry)
        if actual_sha256:
            entry["sha256"] = actual_sha256
            print(f"Verified: {entry['path']}", flush=True)
        else:
            missing.append(entry)
    needed = sum(entry["size"] for entry in missing)
    print(f"Assets to download or repair: {len(missing)} files, {needed:,} bytes", flush=True)
    if shutil.disk_usage(runtime_root).free < needed + 256 * 1024**2:
        raise RuntimeError(f"Not enough free disk space for {needed:,} bytes of missing assets")
    for entry in missing:
        dest = model_dir / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The codec's remote directory differs from the local inference layout.
        staging = dest.parent if "/" not in entry["remote_path"] else model_dir / "hf_cache/.downloads"
        print(f"Downloading: {entry['repository']}/{entry['remote_path']}", flush=True)
        download_options = {
            "repo_id": entry["repository"], "revision": entry["revision"],
            "filename": entry["remote_path"], "local_dir": str(staging),
            # A failed integrity check must not reuse a corrupt completed download.
            "force_download": dest.exists(),
        }
        fetched = Path(hf_hub_download(**download_options))
        actual_sha256 = verify_file(fetched, entry)
        if not actual_sha256:
            # The target can be absent while HF still has a completed, corrupt
            # staging file. Bypass its metadata/cache once, then fail boundedly.
            print(f"Integrity check failed; downloading fresh: {entry['remote_path']}", flush=True)
            download_options["force_download"] = True
            fetched = Path(hf_hub_download(**download_options))
            actual_sha256 = verify_file(fetched, entry)
            if not actual_sha256:
                raise RuntimeError(
                    f"Downloaded file failed integrity verification after a forced retry: {entry['remote_path']}"
                )
        if fetched != dest:
            fetched.replace(dest)
        entry["sha256"] = actual_sha256
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_repository": "IndexTeam/IndexTTS-2",
        "model_revision": WEIGHT_REVISION,
        "runtime_repository": UPSTREAM_URL,
        "runtime_commit": RUNTIME_COMMIT,
        "runtime_version": version("indextts"),
        "python_version": sys.version.split()[0],
        "total_model_bytes": total,
        "files": entries,
        "reference_audio": "User-supplied; not downloaded or replaced by setup",
        "inference_verification": "not_run",
    }
    path = runtime_root / "deployment-manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(f"Verified deployment manifest: {path}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=PROJECT_ROOT / "outputs/indextts2-runtime")
    parser.add_argument("--skip-install", action="store_true", help="Reuse a pinned Python 3.11 runtime; verify/download model files only")
    parser.add_argument("--assets-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve()
    try:
        if args.assets_only:
            download_assets(runtime_root)
        else:
            with setup_lock(runtime_root):
                python = install_runtime(runtime_root, args.skip_install)
                run(python, Path(__file__).resolve(), "--runtime-root", runtime_root, "--assets-only")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"IndexTTS2 setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
