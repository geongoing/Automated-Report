"""Date-scoped manual-input persistence, independent of HTTP handlers."""
import datetime as dt
import threading
from hmi import STATUS_CELLS, DATE_CELLS, WATER_QUALITY_KEYS
from report import MANUAL, ReportError
from runtime import DATA
from persistence import read_backed_json, write_backed_json
DRAFTS = DATA / "drafts.json"
DRAFT_LOCK = threading.Lock()


def draft_date(value):
    if not isinstance(value, str):
        raise ReportError("임시 저장할 기록일을 확인해 주세요.")
    try:
        parsed = dt.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        raise ReportError("임시 저장할 기록일을 확인해 주세요.") from None


def validate_draft(data):
    date = draft_date(data.get("record_date"))
    values = data.get("values")
    if not isinstance(values, dict) or set(values) != {cell for cell, _, _ in MANUAL}:
        raise ReportError("임시 저장할 수치 항목이 올바르지 않습니다.")
    if any(not isinstance(value, str) or len(value) > 100 for value in values.values()):
        raise ReportError("임시 저장할 수치 입력이 너무 깁니다.")
    for key, size in [("day_workers", 8), ("night_workers", 2)]:
        names = data.get(key)
        if not isinstance(names, list) or len(names) != size or any(not isinstance(name, str) or len(name) > 30 for name in names):
            raise ReportError("임시 저장할 근무자 입력이 올바르지 않습니다.")
    fields = data.get("hmi_fields")
    if not isinstance(fields, dict) or set(fields) - set(STATUS_CELLS + DATE_CELLS):
        raise ReportError("임시 저장할 세정일자·가동상태가 올바르지 않습니다.")
    if any(not isinstance(value, str) or len(value) > 100 for value in fields.values()):
        raise ReportError("임시 저장할 세정일자·가동상태 입력이 너무 깁니다.")
    for cell, value in fields.items():
        if cell in DATE_CELLS and value:
            draft_date(value)
    folder = data.get("hmi_folder")
    quality = data.get('water_quality')
    if quality is not None and (not isinstance(quality, dict) or set(quality) != set(WATER_QUALITY_KEYS) or
                               any(not isinstance(value, str) or len(value) > 100 for value in quality.values())):
        raise ReportError('임시 저장할 pH·수온 입력이 올바르지 않습니다.')
    stamp = data.get("updated_at")
    if not isinstance(folder, str) or len(folder) > 4096 or type(stamp) is not int or not 0 < stamp < 2**53:
        raise ReportError("임시 저장 정보가 올바르지 않습니다.")
    return {**({'water_quality': dict(quality)} if quality is not None else {}),
            "record_date": date, "values": dict(values), "day_workers": list(data["day_workers"]),
            "night_workers": list(data["night_workers"]), "hmi_fields": dict(fields),
            "hmi_folder": folder, "updated_at": stamp}


def validate_drafts(data):
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("days"), dict):
        raise ValueError("임시 저장 기록 형식이 올바르지 않습니다.")
    for date, draft in data["days"].items():
        if validate_draft(draft)["record_date"] != date:
            raise ValueError("임시 저장 기록일이 일치하지 않습니다.")
    return data


def read_drafts():
    try:
        return read_backed_json(DRAFTS, validate_drafts, lambda: {"version": 1, "days": {}})
    except (OSError, ValueError):
        raise ReportError("임시 저장 기록을 읽을 수 없습니다.") from None


def get_draft(date):
    date = draft_date(date)
    with DRAFT_LOCK:
        saved = read_drafts()["days"].get(date)
        return {"draft": validate_draft(saved) if isinstance(saved, dict) else None}


def save_draft(body):
    draft = validate_draft(body)
    with DRAFT_LOCK:
        data = read_drafts()
        current = data["days"].get(draft["record_date"])
        if isinstance(current, dict) and current.get("updated_at", 0) >= draft["updated_at"]:
            return {"draft": current, "saved": False}
        data["days"][draft["record_date"]] = draft
        write_backed_json(DRAFTS, data, validate_drafts)
        return {"draft": draft, "saved": True}
