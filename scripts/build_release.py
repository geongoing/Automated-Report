"""Build and package a stable Windows release from the current source version."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "local-web"))
from updater import ASSET, release_url, validate_exe, version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag")
    args = parser.parse_args()
    metadata = json.loads((ROOT / "local-web/version.json").read_text(encoding="utf-8"))
    current = metadata["version"]
    version(current)
    if args.tag and args.tag != "v" + current:
        raise SystemExit("Release tag must match local-web/version.json")
    if os.name != "nt":
        raise SystemExit("Build Windows releases on Windows")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    executable = output / ASSET
    subprocess.run([sys.executable, "-B", str(ROOT / "local-web/build_exe.py"), "--output", str(executable)], check=True)
    validate_exe(executable)
    manifest = {"schema": 1, "channel": "stable", "platform": "windows-x64", "version": current,
                "asset": ASSET, "size": executable.stat().st_size,
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "release_url": release_url(current)}
    (output / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = output / f"SongdoDaily-v{current}.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, "일지작성.exe")
    files = [executable, output / "update.json", bundle]
    checksums = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n" for path in files)
    (output / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(f"Packaged v{current}: {output}")


if __name__ == "__main__":
    main()
