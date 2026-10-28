"""Bundled resources and per-user settings, independent of EXE location."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
RESOURCES = Path(sys._MEIPASS) / "app" if FROZEN else Path(__file__).resolve().parent
DATA = (Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local") / "SongdoDaily"
CONFIG = DATA / "settings.json"
SERVER_INFO = DATA / "server.json"
FOLDER_KEYS = ("raw_folder", "final_folder", "hmi_folder")
HMI_TEMPLATE = RESOURCES.parent / "templates" / "송도_HMI.xlsx" if FROZEN else ROOT / "송도_HMI.xlsx"


def write_json(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def same_profile(info):
    if not isinstance(info, dict) or info.get("app") != "SongdoLocal":
        return False
    if info.get("profile"):
        return Path(info["profile"]).resolve() == DATA.resolve()
    # Allow an upgrade to stop the old server from this exact EXE folder.
    return bool(info.get("workspace")) and Path(info["workspace"]).resolve() == ROOT


def prepare_workspace():
    """Create application state only; never create business folders or templates."""
    DATA.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        return
    legacy = ROOT / "local-web"
    previous = legacy / "settings.json"
    if not previous.is_file():
        return  # New PC: empty paths until the user saves existing folders.
    config = json.loads(previous.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("기존 폴더 설정을 읽을 수 없습니다.")
    fallback = config.get("report_folder", "")
    migrated = {key: config.get(key, fallback) for key in FOLDER_KEYS}
    old_drafts = legacy / "drafts.json"
    drafts = DATA / "drafts.json"
    if old_drafts.is_file() and not drafts.exists():
        try:
            with drafts.open("xb") as target:
                target.write(old_drafts.read_bytes())
        except FileExistsError:
            pass
    write_json(CONFIG, migrated)
