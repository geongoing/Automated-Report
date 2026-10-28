"""Recoverable, no-overwrite publication of one report/HMI file pair."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from maintenance import is_link
from persistence import atomic_bytes, sync_directory, write_json
from runtime import DATA

PENDING = DATA / "pending-writes"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEMP = re.compile(r"\.(?:journal\.json|report\.xlsx|hmi\.xlsx)\.[\w-]+\.tmp\Z")


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def unique_target(folder, name):
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("저장할 파일 이름이 올바르지 않습니다.")
    for attempt in range(10000):
        target = folder / (name if not attempt else f"{Path(name).stem}_{attempt + 1}.xlsx")
        if not target.exists() and not target.is_symlink():
            return target
    raise ValueError("같은 이름의 파일이 너무 많습니다.")


def stage_path(journal, item):
    return Path(item["folder"]) / f'.songdo-{journal["id"]}-{item["kind"]}.tmp'


def target_path(item):
    return Path(item["folder"]) / item["name"]


def payload(folder, item):
    path = folder / (item["kind"] + ".xlsx")
    if is_link(path) or path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
        raise ValueError("복구용 일지 파일의 검증에 실패했습니다.")
    return path.read_bytes()


def load_journal(folder):
    path = folder / "journal.json"
    if is_link(path):
        raise ValueError("연결된 복구 기록은 사용할 수 없습니다.")
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("schema") != 1 or journal.get("id") != folder.name or journal.get("state") not in ("pending", "complete", "aborted"):
        raise ValueError("복구 기록 형식이 올바르지 않습니다.")
    date = dt.date.fromisoformat(journal["record_date"])
    if date.isoformat() != journal["record_date"]:
        raise ValueError("복구 기록일이 올바르지 않습니다.")
    items = journal["outputs"]
    if not isinstance(items, list) or [item.get("kind") for item in items] != ["report", "hmi"]:
        raise ValueError("복구할 일지 쌍을 확인할 수 없습니다.")
    stems = [(date + dt.timedelta(days=1)).strftime("%Y%m%d"), f"송도지소_하수_{date:%Y%m%d}"]
    for item, stem in zip(items, stems):
        if (not isinstance(item.get("folder"), str) or not Path(item["folder"]).is_absolute() or
                not isinstance(item.get("name"), str) or not re.fullmatch(re.escape(stem) + r"(?:_\d+)?\.xlsx", item["name"]) or
                not isinstance(item.get("sha256"), str) or not _SHA.fullmatch(item["sha256"]) or
                type(item.get("size")) is not int or not 0 < item["size"] <= 30 * 1024 * 1024):
            raise ValueError("복구 대상 경로 또는 검증값이 올바르지 않습니다.")
    owned = journal.get("owned", [])
    if not isinstance(owned, list) or any(not isinstance(owner, dict) or owner.get("kind") not in ("report", "hmi") or
                                         type(owner.get("ino")) is not int or type(owner.get("dev")) is not int for owner in owned):
        raise ValueError("복구 파일의 생성 기록이 올바르지 않습니다.")
    return journal


def publish_output(folder, journal, item):
    """Publish only a fully flushed file; never replace an existing workbook."""
    destination, stage = target_path(item), stage_path(journal, item)
    data = payload(folder, item)
    if not destination.parent.is_dir():
        raise FileNotFoundError("저장 폴더에 다시 연결한 뒤 실행해 주세요.")
    if destination.exists() or destination.is_symlink():
        if is_link(destination) or file_digest(destination) != item["sha256"]:
            raise FileExistsError("복구 대상과 다른 기존 파일이 있어 보존했습니다.")
        return None  # Published before the interruption; do not write it again.
    if is_link(stage):
        raise ValueError("연결된 임시 파일은 수정하지 않습니다.")
    atomic_bytes(stage, data)
    stat = stage.stat()
    if stat.st_size != item["size"] or file_digest(stage) != item["sha256"]:
        raise ValueError("임시 저장 파일의 검증에 실패했습니다.")
    # Rename/link preserves the staged file's identity. Persist ownership BEFORE
    # publication, so failures immediately after rename can still roll it back.
    # A pre-publication crash is harmless: the destination does not yet exist.
    owner = {"kind": item["kind"], "ino": stat.st_ino, "dev": stat.st_dev}
    journal["owned"] = [entry for entry in journal.get("owned", []) if entry["kind"] != item["kind"]] + [owner]
    write_json(folder / "journal.json", journal)
    if os.name == "nt":
        os.rename(stage, destination)  # Windows rename refuses an existing target.
    else:
        os.link(stage, destination)    # POSIX rename would overwrite; link is exclusive.
        stage.unlink()
    sync_directory(destination.parent)
    return owner


def cleanup(folder, journal=None):
    if journal:
        for item in journal["outputs"]:
            stage = stage_path(journal, item)
            if not is_link(stage):
                stage.unlink(missing_ok=True)
            # An abrupt exit inside atomic_bytes can leave its sibling temporary
            # file behind. These names include this journal's unguessable UUID.
            pattern = re.compile(re.escape("." + stage.name + ".") + r"[\w-]+\.tmp\Z")
            for leftover in stage.parent.glob("." + stage.name + ".*.tmp"):
                if pattern.fullmatch(leftover.name) and leftover.is_file() and not is_link(leftover):
                    leftover.unlink()
    # Delete only direct, known files in this UUID directory; never recurse.
    if not _ID.fullmatch(folder.name) or is_link(folder) or folder.absolute().parent != PENDING.absolute():
        return
    files = list(folder.iterdir())
    if any(is_link(path) or not path.is_file() or (path.name not in {"journal.json", "report.xlsx", "hmi.xlsx"} and not _TEMP.fullmatch(path.name)) for path in files):
        return
    for path in files:
        path.unlink()
    folder.rmdir()
    sync_directory(PENDING)


def rollback(journal):
    owners = {item["kind"]: item for item in journal.get("owned", [])}
    for item in journal["outputs"]:
        owner, path = owners.get(item["kind"]), target_path(item)
        if not owner or not path.exists() or is_link(path):
            continue
        stat = path.stat()
        if (stat.st_ino, stat.st_dev) == (owner["ino"], owner["dev"]) and file_digest(path) == item["sha256"]:
            path.unlink()
            sync_directory(path.parent)
        else:
            print("중단 후 수정된 일지 파일은 보존했습니다.", flush=True)


def commit_pair(report_data, report_target, hmi_data, hmi_target, record_date):
    if is_link(PENDING):
        raise ValueError("연결된 복구 폴더에는 기록하지 않습니다.")
    PENDING.mkdir(parents=True, exist_ok=True)
    folder = PENDING / uuid.uuid4().hex
    folder.mkdir()
    journal = {"schema": 1, "id": folder.name, "state": "pending", "record_date": record_date, "outputs": [], "owned": []}
    try:
        for kind, data, target in [("report", report_data, report_target), ("hmi", hmi_data, hmi_target)]:
            atomic_bytes(folder / (kind + ".xlsx"), data)
            journal["outputs"].append({"kind": kind, "folder": str(target.parent), "name": target.name,
                                       "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        write_json(folder / "journal.json", journal)
        for item in journal["outputs"]:
            publish_output(folder, journal, item)
        journal["state"] = "complete"
        write_json(folder / "journal.json", journal)
    except Exception:
        # Ordinary errors roll back only the unchanged files created here.
        # A killed process cannot run this handler; its pending journal resumes.
        journal["state"] = "aborted"
        try:
            if len(journal["outputs"]) == 2:
                write_json(folder / "journal.json", journal)
            rollback(journal)
            cleanup(folder, journal)
        except (OSError, ValueError):
            print("중단된 저장 정리를 다음 실행으로 미뤘습니다.", flush=True)
        raise
    try:
        cleanup(folder, journal)
    except OSError:
        print("완료된 저장 기록 정리를 다음 실행으로 미뤘습니다.", flush=True)
    return report_target, hmi_target


def recover_pending():
    issues = []
    if not PENDING.is_dir() or is_link(PENDING):
        return issues
    for folder in sorted(PENDING.iterdir()):
        if not _ID.fullmatch(folder.name) or is_link(folder) or not folder.is_dir():
            continue
        journal = None
        try:
            if not (folder / "journal.json").exists():
                cleanup(folder)  # Preparation stopped before any output was touched.
                continue
            journal = load_journal(folder)
            if journal["state"] == "pending":
                # Verify BOTH payloads before touching either destination.
                for item in journal["outputs"]:
                    payload(folder, item)
                for item in journal["outputs"]:
                    publish_output(folder, journal, item)
                journal["state"] = "complete"
                write_json(folder / "journal.json", journal)
                print(f'중단된 최종 일지·HMI 저장을 복구했습니다: {journal["record_date"]}', flush=True)
            elif journal["state"] == "aborted":
                for item in journal["outputs"]:
                    payload(folder, item)
                rollback(journal)
            cleanup(folder, journal)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            issues.append({"record_date": journal.get("record_date") if journal else None,
                           "folders": [item["folder"] for item in journal["outputs"]] if journal else [],
                           "error": str(error)})
            print(f"중단된 저장 복구를 보류했습니다: {type(error).__name__}: {error}", flush=True)
    return issues
