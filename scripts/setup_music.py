"""Install the pinned, attribution-required music catalog from its official sources."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.parse
import urllib.request


def install(manifest, destination, *, verify_only=False):
    document = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if document.get("version") != 1 or not isinstance(document.get("tracks"), list) or not document["tracks"]:
        raise ValueError("Invalid music catalog")
    destination = Path(destination).resolve()
    if verify_only:
        installed = destination / "catalog.json"
        if not installed.is_file() or json.loads(installed.read_text("utf-8")) != document:
            raise ValueError("Installed catalog is missing or differs from the pinned manifest")
    else:
        destination.mkdir(parents=True, exist_ok=True)
    for track in document["tracks"]:
        filename = track["filename"]
        if not filename or Path(filename).name != filename or "\\" in filename:
            raise ValueError("Catalog filename escapes the installation directory")
        target = (destination / filename).resolve()
        if not target.is_relative_to(destination):
            raise ValueError("Music destination escapes the installation directory")
        if not target.exists():
            if verify_only:
                raise ValueError("Missing music: " + filename)
            location = urllib.parse.urlsplit(track["download_url"])
            if location.scheme != "https" or location.hostname not in {"www.scottbuckley.com.au", "scottbuckley.com.au"}:
                raise ValueError("Music downloads must use the pinned official HTTPS host")
            request = urllib.request.Request(track["download_url"], headers={"User-Agent": "QuickCut music setup"})
            with tempfile.TemporaryDirectory(prefix=".download-", dir=destination) as directory:
                temporary = Path(directory) / filename
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
                    final = urllib.parse.urlsplit(response.geturl())
                    if final.scheme != "https" or final.hostname not in {"www.scottbuckley.com.au", "scottbuckley.com.au"}:
                        raise ValueError("Music download redirected away from the official HTTPS host")
                    shutil.copyfileobj(response, stream)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != track["sha256"]:
                    raise ValueError("Music download hash mismatch: " + filename)
                temporary.rename(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != track["sha256"]:
            raise ValueError("Installed music hash mismatch: " + filename)
    if not verify_only:
        (destination / "catalog.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ready": True, "tracks": len(document["tracks"]), "attribution_required": True}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root / "outputs/music-library")
    parser.add_argument("--manifest", type=Path, default=root / "examples/music/catalog.json")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.manifest, args.root, verify_only=args.verify)))
