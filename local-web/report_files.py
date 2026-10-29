"""Discover report sources and carry previous HMI fields by record date."""
import base64
import binascii
import datetime as dt
from functools import lru_cache
import hashlib
import re
from pathlib import Path
from app_settings import report_folder, final_folder, hmi_output_folder
from hmi import editable_fields, hmi_record_date
from report import Book, CARRY, MANUAL, ReportError
from runtime import HMI_TEMPLATE


def hmi_template():
    if not HMI_TEMPLATE.is_file():
        raise ReportError(f"HMI 양식을 찾지 못했습니다. {HMI_TEMPLATE} 파일을 배치해 주세요.")
    return HMI_TEMPLATE.read_bytes()


@lru_cache(maxsize=1024)
def file_metadata(path, modified_ns, size):
    try:
        book = Book(Path(path).read_bytes(), Path(path).name)
        # Historical completed reports may have a blank G32 on no-export days.
        required = [x[0] for x in MANUAL if x[0] != "G32"] + [x[0] for x in CARRY] + ["A32"]
        complete = all(book.cell(cell) is not None for cell in required)
        raw = "미가공" in Path(path).name or "원본" in Path(path).name or not complete
        return {"kind": "raw" if raw else "final", "record_date": book.date.isoformat(), "error": None}
    except (ReportError, OSError, ValueError) as e:
        return {"kind": "unknown", "record_date": None, "error": str(e)}


@lru_cache(maxsize=1024)
def hmi_metadata(path, modified_ns, size, expected_date=None):
    try:
        book = Book(Path(path).read_bytes(), Path(path).name, require_report=False)
        reference = dt.date.fromisoformat(expected_date) if expected_date is not None else None
        return hmi_record_date(book, reference_date=reference).isoformat()
    except (ReportError, OSError, ValueError):
        return None


def previous_hmi(record_date):
    folder = hmi_output_folder()
    if not folder.is_dir():
        raise ReportError("HMI 일지 폴더를 찾을 수 없습니다. 폴더 설정에서 경로를 확인해 주세요.")
    expected = (record_date - dt.timedelta(days=1)).isoformat()
    candidates = []
    for directory in (folder, folder / "완성일지"):
        if not directory.is_dir():
            continue
        for path in directory.glob('*.xlsx'):
            if path.name.startswith('~$'):
                continue
            stat = path.stat()
            if hmi_metadata(str(path), stat.st_mtime_ns, stat.st_size, expected) == expected:
                candidates.append((stat.st_mtime_ns, str(path), path))
    if not candidates:
        return None, None
    path = max(candidates)[2]
    return path, path.read_bytes()


def hmi_profile(record_date):
    path, data = previous_hmi(record_date)
    message = "전날 HMI의 세정일자·가동상태·작업내용을 불러왔습니다. 필요한 항목만 수정하세요." if path else "전날 HMI가 없습니다. 처음 작성하는 항목을 직접 입력하거나 HMI 폴더 경로를 확인해 주세요."
    if data is not None:
        try:
            hmi_record_date(Book(data, path.name, require_report=False))
        except ReportError:
            message += f" 연도 없는 제목의 월·일을 이월 대상일({record_date - dt.timedelta(days=1)})과 확인했습니다."
    return {"record_date": record_date.isoformat(),
            "previous_date": (record_date - dt.timedelta(days=1)).isoformat(),
            "previous_name": path.name if path else None, "previous_path": str(path) if path else None,
            "seed_token": hashlib.sha256(data).hexdigest() if data is not None else "",
            "fields": editable_fields(data),
            "message": message}


def hmi_source_input(record_date, data):
    if not isinstance(data, dict):
        raise ReportError("HMI의 전날 이월값과 당일 근무자를 입력해 주세요.")
    _, previous = previous_hmi(record_date)
    token = hashlib.sha256(previous).hexdigest() if previous is not None else ""
    if data.get("seed_token") != token:
        raise ReportError("전날 HMI 파일이 변경되었습니다. HMI 이월값을 다시 불러온 후 확인해 주세요.")
    return previous, data


def catalog():
    raw_folder, previous_folder = report_folder(), final_folder()
    for folder, label in [(raw_folder, "당일 레포트"), (previous_folder, "과거 레포트")]:
        if not folder.is_dir():
            raise ReportError(f"{label} 폴더를 찾을 수 없습니다. 폴더 설정에서 경로를 변경해 주세요.")
    same_folder = raw_folder.resolve() == previous_folder.resolve()
    files = []
    for role, folder in [("raw", raw_folder), ("final", previous_folder)]:
        if not folder.is_dir():
            continue
        for path in folder.glob("*.xlsx"):
            if path.name.startswith("~$"):
                continue
            stat = path.stat()
            meta = file_metadata(str(path), stat.st_mtime_ns, stat.st_size)
            if meta["kind"] == "unknown":
                continue
            if role == "raw":
                if same_folder and meta["kind"] != "raw":
                    continue
            elif meta["kind"] != "final":
                continue
            base = raw_folder if role == "raw" else previous_folder
            relative = path.relative_to(base).as_posix()
            files.append({**meta, "kind": role, "id": role + "/" + relative, "relative_path": relative,
                          "name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    return sorted(files, key=lambda x: (x["record_date"], x["modified"], x["id"]), reverse=True)


def auto_pair(files, raw_id=None):
    raw = next((f for f in files if f["kind"] == "raw" and (raw_id is None or f["id"] == raw_id)), None)
    if not raw:
        return {"raw_id": None, "previous_id": None, "message": "설정한 폴더에 당일 레포트가 없습니다."}
    expected = (dt.date.fromisoformat(raw["record_date"]) - dt.timedelta(days=1)).isoformat()
    previous = next((f for f in files if f["kind"] == "final" and f["record_date"] == expected), None)
    return {"raw_id": raw["id"], "previous_id": previous["id"] if previous else None,
            "message": "전날 최종 일지를 자동으로 연결했습니다." if previous else f"기록일 {expected}의 최종 일지를 찾지 못했습니다. 과거 레포트 폴더에 넣어 주세요."}


def duty_context(record_date):
    try:
        date = dt.date.fromisoformat(record_date)
        if date.isoformat() != record_date:
            raise ValueError
        file_date = date + dt.timedelta(days=1)
    except (TypeError, ValueError, OverflowError):
        raise ReportError("작성할 당직일을 YYYY-MM-DD 형식으로 선택해 주세요.") from None
    return {"record_date": date.isoformat(), "report_file_date": file_date.isoformat(),
            "expected_raw_name": file_date.strftime("%Y%m%d.xlsx")}


def raw_matches_duty(file, context):
    if file["kind"] != "raw" or file["record_date"] != context["record_date"]:
        return False
    prefix = re.match(r"^(20\d{6})(?!\d)", file["name"])
    return not prefix or prefix[1] == context["expected_raw_name"][:8]


def duty_pair(files, record_date):
    context = duty_context(record_date)
    candidates = [file for file in files if raw_matches_duty(file, context)]
    context["raw_ids"] = [file["id"] for file in candidates]
    raw = next(iter(candidates), None)
    if not raw:
        return {**context, "raw_id": None, "previous_id": None,
                "message": f'{record_date} 당직일의 당일 레포트가 없습니다. {context["expected_raw_name"]} 파일(내부 기록일 {record_date})을 확인해 주세요.'}
    return {**auto_pair(files, raw["id"]), **context}


def validate_duty_source(data, name, record_date):
    context = duty_context(record_date)
    book = Book(data, name)
    if not raw_matches_duty({"kind": "raw", "record_date": book.date.isoformat(), "name": name}, context):
        raise ReportError(f'선택한 레포트의 날짜가 작성할 당직일 {record_date}과 다릅니다. {context["expected_raw_name"]} 파일과 내부 기록일을 확인해 주세요.')


def source(spec):
    if not isinstance(spec, dict):
        raise ReportError("당일 레포트와 전날 최종 일지를 선택해 주세요.")
    if "id" in spec:
        available = {x["id"] for x in catalog()}
        if spec["id"] not in available:
            raise ReportError("선택한 파일이 폴더에 없습니다. 목록을 새로고침해 주세요.")
        role, relative = spec["id"].split("/", 1)
        p = (report_folder() if role == "raw" else final_folder()) / relative
        return p.read_bytes(), p.name
    name = str(spec.get("name", ""))
    if not name.lower().endswith(".xlsx"):
        raise ReportError(".xlsx 형식의 엑셀 파일을 선택해 주세요.")
    try:
        data = base64.b64decode(spec.get("data", ""), validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ReportError("업로드한 파일을 읽을 수 없습니다.") from None
    if not data or len(data) > 15 * 1024 * 1024:
        raise ReportError("파일은 15 MB 이하의 XLSX여야 합니다.")
    return data, Path(name).name
