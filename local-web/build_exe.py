"""Build the Windows EXE. Requires the pinned build dependency only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

APP = Path(__file__).resolve().parent
ROOT = APP.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "일지작성.exe")
    args = parser.parse_args()
    metadata = json.loads((APP / "version.json").read_text(encoding="utf-8"))
    version = metadata["version"]
    version_tuple = tuple(map(int, version.split("."))) + (0,)
    cache = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "SongdoBuild" / version
    cache.mkdir(parents=True, exist_ok=True)
    version_info = cache / "version-info.txt"
    strings = {"CompanyName": "", "FileDescription": "운영일지 작성", "FileVersion": version,
               "InternalName": "SongdoDaily", "OriginalFilename": "일지작성.exe",
               "ProductName": "일지 작성", "ProductVersion": version}
    version_info.write_text(
        "VSVersionInfo(ffi=FixedFileInfo(filevers=" + repr(version_tuple) + ", prodvers=" + repr(version_tuple) +
        ", mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)), "
        "kids=[StringFileInfo([StringTable('041204B0', [" +
        ",".join("StringStruct(" + repr(key) + "," + repr(value) + ")" for key, value in strings.items()) +
        "])]), VarFileInfo([VarStruct('Translation',[1042,1200])])])", encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole", "--noconfirm", "--noupx",
               "--name", "일지작성", "--icon", str(APP / "assets" / "app-icon.ico"), "--version-file", str(version_info),
               "--distpath", str(cache / "dist"), "--workpath", str(cache / "work"), "--specpath", str(cache),
               "--paths", str(APP), "--add-data", str(APP / "static") + os.pathsep + "app/static",
               "--add-data", str(APP / "version.json") + os.pathsep + "app",
               "--add-data", str(ROOT / "송도_HMI.xlsx") + os.pathsep + "templates",
               str(APP / "launcher.pyw")]
    subprocess.run(command, cwd=ROOT, check=True)
    source = cache / "dist" / "일지작성.exe"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, args.output)
    print(f"Built v{version}: {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
