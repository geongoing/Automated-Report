"""Duplicate approval and exclusive publication of a report/HMI pair."""
import datetime as dt
import hashlib
import json
import threading
import urllib.parse
from app_settings import configured_folder, folder_config, output_folder
from report import ReportError
from report_files import file_metadata, hmi_metadata
SAVE_LOCK = threading.Lock()


def output_info(path, kind):
    return {"kind": kind, "name": path.name, "path": str(path),
            "download_url": ("/download/hmi/" if kind == "hmi" else "/download/") + urllib.parse.quote(path.name)}


def save_unique(data, name, *, folder=None):
    output = output_folder() if folder is None else folder
    if not output.is_dir():
        raise ReportError("저장할 폴더를 찾을 수 없습니다. 설정에서 경로를 확인해 주세요.")
    target = output / name
    stem = target.stem
    for attempt in range(10000):
        candidate = target if attempt == 0 else output / f"{stem}_{attempt + 1}.xlsx"
        try:
            with candidate.open("xb") as f:
                try:
                    f.write(data)
                except BaseException:
                    f.close()
                    candidate.unlink(missing_ok=True)
                    raise
            return candidate
        except FileExistsError:
            continue
    raise ReportError("같은 이름의 파일이 너무 많습니다. 완성일지 폴더를 정리해 주세요.")


class DuplicateReportError(ReportError):
    def __init__(self, existing):
        super().__init__("같은 기록일의 일지가 있습니다. 기존 파일을 확인한 뒤 재작성을 선택해 주세요.")
        self.existing = existing


def destination_folders():
    folders = folder_config()
    return {"report": configured_folder("final_folder", "완성일지", folders),
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
                matches = hmi_metadata(str(path), stat.st_mtime_ns, stat.st_size) == record_date
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
    created = []
    with SAVE_LOCK:
        folders = destination_folders()
        existing = existing_outputs(record_date, folders=folders)
        if existing["outputs"] and rewrite_token != existing["token"]:
            raise DuplicateReportError(existing)
        try:
            report_path = save_unique(report_data, report_name, folder=folders["report"])
            created.append(report_path)
            hmi_path = save_unique(hmi_data, hmi_name, folder=folders["hmi"])
            created.append(hmi_path)
            return report_path, hmi_path
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
