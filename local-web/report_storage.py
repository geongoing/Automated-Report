"""Duplicate approval and exclusive publication of a report/HMI pair."""
import datetime as dt
import hashlib
import json
import threading
import urllib.parse
import transactions
from app_settings import configured_folder, folder_config
from report import ReportError
from report_files import file_metadata, hmi_metadata
from runtime import same_path
SAVE_LOCK = threading.RLock()
RECOVERY_ERRORS = {}


def output_info(path, kind):
    return {"kind": kind, "name": path.name, "path": str(path), "folder": str(path.parent),
            "download_url": ("/download/hmi/" if kind == "hmi" else "/download/") + urllib.parse.quote(path.name)}


def recover_pending():
    with SAVE_LOCK:
        issues = transactions.recover_pending()
        RECOVERY_ERRORS.clear()
        RECOVERY_ERRORS.update({issue["id"]: issue["error"] for issue in issues})
        return issues


def recovery_status():
    with SAVE_LOCK:
        records = transactions.pending_records()
        for item in records:
            if item["id"] in RECOVERY_ERRORS:
                item["error"] = RECOVERY_ERRORS[item["id"]]
        return {"items": records}


class DuplicateReportError(ReportError):
    def __init__(self, existing):
        super().__init__("같은 기록일의 일지가 있습니다. 기존 파일을 확인한 뒤 재작성을 선택해 주세요.")
        self.existing = existing


def destination_folders():
    folders = folder_config()
    return {"report": configured_folder("final_folder", "과거 레포트", folders),
            "hmi": configured_folder("hmi_folder", "HMI 일지", folders)}


def existing_outputs(record_date, *, folders=None):
    date = dt.date.fromisoformat(record_date)
    folders = destination_folders() if folders is None else folders
    stems = {"report": (date + dt.timedelta(days=1)).strftime("%Y%m%d"),
             "hmi": f"송도지소_하수_{date:%Y%m%d}"}
    found = []
    for kind, folder in folders.items():
        for path in folder.glob("*.xlsx"):
            if path.name.startswith("~$"):
                continue
            stat = path.stat()
            if kind == "report":
                meta = file_metadata(str(path), stat.st_mtime_ns, stat.st_size)
                matches = meta["kind"] == "final" and meta["record_date"] == record_date
            else:
                matches = hmi_metadata(str(path), stat.st_mtime_ns, stat.st_size, record_date) == record_date
            suffix = path.stem.removeprefix(stems[kind] + "_")
            expected_name = path.stem == stems[kind] or (path.stem.startswith(stems[kind] + "_") and suffix.isdigit())
            if matches or expected_name:
                found.append({**output_info(path, kind), "modified_ns": stat.st_mtime_ns, "size": stat.st_size})
    found.sort(key=lambda item: (-item["modified_ns"], item["kind"], item["name"]))
    fingerprint = {"date": record_date, "folders": {key: str(path) for key, path in folders.items()}, "files": found}
    token = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if found else ""
    return {"record_date": record_date, "outputs": found, "token": token}


def save_report_pair(report_data, report_name, hmi_data, hmi_name, *, record_date, rewrite_token=None):
    """Publish one generation's two outputs; roll back only its new files on failure."""
    with SAVE_LOCK:
        folders = destination_folders()
        for issue in recover_pending():
            if issue["record_date"] == record_date and any(same_path(path, folder) for path in issue["folders"] for folder in folders.values()):
                raise ReportError("이 기록일의 중단된 저장을 복구하지 못했습니다. 저장 폴더 연결과 기존 파일을 확인한 뒤 다시 실행해 주세요.")
        existing = existing_outputs(record_date, folders=folders)
        if existing["outputs"] and rewrite_token != existing["token"]:
            raise DuplicateReportError(existing)
        report_path = transactions.unique_target(folders["report"], report_name)
        hmi_path = transactions.unique_target(folders["hmi"], hmi_name)
        return transactions.commit_pair(report_data, report_path, hmi_data, hmi_path, record_date)
