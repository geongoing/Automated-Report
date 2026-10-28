"""Local-only HTTP application. Python 3.10+, no package installation."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import hashlib
import socket
import os
import sys
import datetime as dt
from functools import lru_cache
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.parse
import urllib.request
import webbrowser

sys.dont_write_bytecode = True

from report import Book, MANUAL, CARRY, ReportError, generate_with_values, prepare
from hmi import render_hmi, hmi_record_date, editable_fields, STATUS_CELLS, DATE_CELLS
from folder_picker import choose_folder
from runtime import ROOT, RESOURCES, DATA, FROZEN, SERVER_INFO, HMI_TEMPLATE, FOLDER_KEYS, write_json, same_profile, prepare_workspace

APP = RESOURCES
APP_VERSION = json.loads((APP / "version.json").read_text(encoding="utf-8"))["version"]
CONFIG = DATA / "settings.json"
MAX_REQUEST = 32 * 1024 * 1024
SAVE_LOCK = threading.Lock()
PICK_LOCK = threading.Lock()
DRAFT_LOCK = threading.Lock()
DRAFTS = DATA / "drafts.json"


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
    stamp = data.get("updated_at")
    if not isinstance(folder, str) or len(folder) > 4096 or type(stamp) is not int or not 0 < stamp < 2**53:
        raise ReportError("임시 저장 정보가 올바르지 않습니다.")
    return {"record_date": date, "values": dict(values), "day_workers": list(data["day_workers"]),
            "night_workers": list(data["night_workers"]), "hmi_fields": dict(fields),
            "hmi_folder": folder, "updated_at": stamp}


def read_drafts():
    if not DRAFTS.exists():
        return {"version": 1, "days": {}}
    try:
        data = json.loads(DRAFTS.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("days"), dict):
            raise ValueError
        return data
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
        temporary = DRAFTS.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DRAFTS)
        return {"draft": draft, "saved": True}


def folder_config():
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
        if not isinstance(config, dict):
            raise ValueError
        result = {}
        for key in FOLDER_KEYS:
            value = config.get(key, "")
            if not isinstance(value, str):
                raise ValueError
            result[key] = Path(value) if value.strip() else None
        return result
    except (ValueError, TypeError, OSError):
        raise ReportError("폴더 설정을 읽지 못했습니다. 웹에서 폴더 경로를 다시 저장해 주세요.") from None


def report_folder():
    return configured_folder("raw_folder", "당일 레포트")


def final_folder():
    return configured_folder("final_folder", "완성일지")


def output_folder():
    return final_folder()


def hmi_output_folder():
    return configured_folder("hmi_folder", "HMI 일지")


def configured_folder(key, label, folders=None):
    path = (folder_config() if folders is None else folders)[key]
    if path is None:
        raise ReportError(f"{label} 폴더 경로를 설정해 주세요.")
    if not path.is_absolute() or not path.is_dir():
        raise ReportError(f"{label} 폴더를 찾을 수 없습니다. 설정에서 기존 폴더의 경로를 확인해 주세요.")
    return path


def settings():
    folders = folder_config()
    configured = all(path is not None and path.is_absolute() and path.is_dir() for path in folders.values())
    message = "" if configured else ("사용할 기존 폴더의 경로를 설정해 주세요." if any(path is None for path in folders.values()) else "설정한 폴더를 찾을 수 없습니다. 경로와 연결 상태를 확인해 주세요.")
    return {**{key: str(path) if path is not None else "" for key, path in folders.items()},
            "configured": configured, "configuration_message": message,
            "output_folder": str(folders["final_folder"]) if folders["final_folder"] else "", "hmi_template": str(HMI_TEMPLATE),
            "hmi_available": HMI_TEMPLATE.is_file(), "hmi_template_label": "프로그램 내장 양식" if FROZEN else str(HMI_TEMPLATE),
            "draft_scope": str(DATA), "version": APP_VERSION,
            "execution_mode": "exe" if FROZEN else "python"}


def hmi_template():
    if not HMI_TEMPLATE.is_file():
        raise ReportError(f"HMI 양식을 찾지 못했습니다. {HMI_TEMPLATE} 파일을 배치해 주세요.")
    return HMI_TEMPLATE.read_bytes()


def output_info(path, kind):
    return {"kind": kind, "name": path.name, "path": str(path),
            "download_url": ("/download/hmi/" if kind == "hmi" else "/download/") + urllib.parse.quote(path.name)}


def validate_folder(folder, label):
    if not isinstance(folder, str) or not folder.strip():
        raise ReportError(f"{label}의 전체 경로를 입력해 주세요.")
    path = Path(folder.strip().strip('"')).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ReportError(f"{label}: 존재하는 폴더의 전체 경로를 입력해 주세요. 예: C:\\일일레포트")
    try:
        next(path.iterdir(), None)
    except OSError:
        raise ReportError("이 폴더를 읽을 수 없습니다. 경로와 접근 권한을 확인해 주세요.") from None
    return str(path.resolve())


def save_settings(folder, previous_folder=None, previous_hmi_folder=None):
    config = {"raw_folder": validate_folder(folder, "당일 레포트 폴더"),
              "final_folder": validate_folder(folder if previous_folder is None else previous_folder, "완성일지 폴더"),
              "hmi_folder": validate_folder(previous_hmi_folder if previous_hmi_folder is not None else (folder if previous_folder is None else previous_folder), "HMI 일지 폴더")}
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG)
    return settings()


def select_folder(body):
    labels = {"raw_folder": "당일 레포트", "final_folder": "완성일지", "hmi_folder": "HMI 일지"}
    key = body.get("kind")
    if key not in labels or not isinstance(body.get("initial", ""), str):
        raise ReportError("선택할 폴더 항목을 확인해 주세요.")
    if not PICK_LOCK.acquire(blocking=False):
        raise ReportError("열려 있는 폴더 선택 창을 먼저 닫아 주세요.")
    try:
        try:
            selected = choose_folder(labels[key] + " 폴더 선택", body.get("initial", ""))
        except OSError as e:
            raise ReportError("폴더 선택 창을 열지 못했습니다. 경로를 직접 입력해 주세요.") from e
        return {"path": validate_folder(selected, labels[key]) if selected is not None else None}
    finally:
        PICK_LOCK.release()


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
def hmi_metadata(path, modified_ns, size):
    try:
        book = Book(Path(path).read_bytes(), Path(path).name, require_report=False)
        return hmi_record_date(book).isoformat()
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
            if hmi_metadata(str(path), stat.st_mtime_ns, stat.st_size) == expected:
                candidates.append((stat.st_mtime_ns, str(path), path))
    if not candidates:
        return None, None
    path = max(candidates)[2]
    return path, path.read_bytes()


def hmi_profile(record_date):
    path, data = previous_hmi(record_date)
    return {"record_date": record_date.isoformat(),
            "previous_date": (record_date - dt.timedelta(days=1)).isoformat(),
            "previous_name": path.name if path else None, "previous_path": str(path) if path else None,
            "seed_token": hashlib.sha256(data).hexdigest() if data is not None else "",
            "fields": editable_fields(data),
            "message": "전날 HMI의 세정일자·가동상태·작업내용을 불러왔습니다. 필요한 항목만 수정하세요." if path else "전날 HMI가 없습니다. 처음 작성하는 항목을 직접 입력하거나 HMI 폴더 경로를 확인해 주세요."}


def hmi_source_input(record_date, data):
    if not isinstance(data, dict):
        raise ReportError("HMI의 전날 이월값과 당일 근무자를 입력해 주세요.")
    _, previous = previous_hmi(record_date)
    token = hashlib.sha256(previous).hexdigest() if previous is not None else ""
    if data.get("seed_token") != token:
        raise ReportError("전날 HMI 파일이 변경되었습니다. HMI 이월값을 다시 불러온 후 확인해 주세요.")
    return previous, data


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False
    # Windows permits duplicate listeners with SO_REUSEADDR. Claim this port
    # exclusively so a second launch can safely reopen the existing app.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def catalog():
    raw_folder, previous_folder = report_folder(), final_folder()
    for folder, label in [(raw_folder, "당일 레포트"), (previous_folder, "완성일지")]:
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
            "message": "전날 최종 일지를 자동으로 연결했습니다." if previous else f"기록일 {expected}의 최종 일지를 찾지 못했습니다. 완성일지 폴더에 넣어 주세요."}


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


class Handler(BaseHTTPRequestHandler):
    server_version = "SongdoLocal/" + APP_VERSION

    def reply(self, status, data, content_type="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.local_request():
            return self.reply(403, {"error": "로컬 주소에서만 사용할 수 있습니다."})
        if path == "/api/status":
            return self.reply(200, {"app": "SongdoLocal", "workspace": str(ROOT), "profile": str(DATA), "running": True, "version": APP_VERSION,
                                    "execution_mode": "exe" if FROZEN else "python", "port": self.server.server_port})
        if path == "/api/settings":
            try:
                return self.reply(200, settings())
            except ReportError as e:
                return self.reply(400, {"error": str(e)})
        if path == "/api/draft":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                return self.reply(200, get_draft(query.get("date", [None])[0]))
            except ReportError as e:
                return self.reply(400, {"error": str(e)})
        if path == "/api/files":
            try:
                config = settings()
                files = catalog() if config["configured"] else []
                automatic = auto_pair(files) if config["configured"] else {"raw_id": None, "previous_id": None, "message": config["configuration_message"]}
                return self.reply(200, {"files": files, "automatic": automatic, **config})
            except (ReportError, OSError) as e:
                return self.reply(400, {"error": str(e)})
        if path.startswith("/download/"):
            is_hmi = path.startswith("/download/hmi/")
            name = urllib.parse.unquote(path[len("/download/hmi/") if is_hmi else len("/download/"):])
            if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".xlsx"):
                return self.reply(404, {"error": "파일을 찾을 수 없습니다."})
            try:
                target = (hmi_output_folder() if is_hmi else output_folder()) / name
            except ReportError as e:
                return self.reply(400, {"error": str(e)})
            if not target.is_file():
                return self.reply(404, {"error": "파일을 찾을 수 없습니다."})
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in static:
            name, content = static[path]
            return self.reply(200, (APP / "static" / name).read_bytes(), content)
        self.reply(404, {"error": "페이지를 찾을 수 없습니다."})

    def local_request(self):
        host = self.headers.get("Host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            return parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == self.server.server_port
        return True

    def do_POST(self):
        try:
            if not self.local_request():
                return self.reply(403, {"error": "로컬 웹 화면에서 실행해 주세요."})
            if self.path not in ("/api/settings", "/api/select-folder", "/api/inspect", "/api/preview", "/api/generate", "/api/hmi/profile", "/api/shutdown", "/api/draft"):
                return self.reply(404, {"error": "요청을 찾을 수 없습니다."})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST:
                raise ReportError("요청 크기가 올바르지 않습니다. 파일 크기를 확인해 주세요.")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ReportError("JSON 형식의 요청이 필요합니다.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ReportError("요청 형식이 올바르지 않습니다.")
            if self.path == "/api/draft":
                return self.reply(200, save_draft(body))
            if self.path == "/api/shutdown":
                self.reply(200, {"message": "서버를 종료했습니다. 이 창을 닫아도 됩니다."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/api/settings":
                return self.reply(200, save_settings(body.get("raw_folder", body.get("report_folder")), body.get("final_folder"), body.get("hmi_folder")))
            if self.path == "/api/select-folder":
                return self.reply(200, select_folder(body))
            if self.path == "/api/hmi/profile":
                return self.reply(200, hmi_profile(dt.date.fromisoformat(body.get("record_date", ""))))
            raw_data, raw_name = source(body.get("raw"))
            previous_data, previous_name = source(body.get("previous"))
            if self.path == "/api/inspect":
                summary, _, _ = prepare(raw_data, previous_data, raw_name, previous_name)
                summary["existing"] = existing_outputs(summary["record_date"])
                return self.reply(200, summary)
            inputs = body.get("inputs")
            if not isinstance(inputs, dict):
                raise ReportError("수기 입력값을 확인해 주세요.")
            if self.path == "/api/preview":
                summary, _, _ = prepare(raw_data, previous_data, raw_name, previous_name, inputs)
                summary["existing"] = existing_outputs(summary["record_date"])
                return self.reply(200, summary)
            data, summary, values = generate_with_values(raw_data, previous_data, raw_name, previous_name, inputs)
            if body.get("source_token") != summary["source_token"]:
                raise ReportError("계산 결과를 확인한 뒤 원본 파일이 변경되었습니다. 파일 목록을 새로고침하고 계산 결과를 다시 확인해 주세요.")
            # Both workbooks use this operation's fresh result in memory.
            # Never reopen a saved final report for a second generation step.
            previous, hmi_input = hmi_source_input(dt.date.fromisoformat(summary["record_date"]), body.get("hmi"))
            hmi_data, hmi_summary = render_hmi(values, hmi_template(), previous_data=previous, user_input=hmi_input)
            target, hmi_target = save_report_pair(data, summary["output_name"], hmi_data, hmi_summary["output_name"],
                                                  record_date=summary["record_date"], rewrite_token=body.get("rewrite_token"))
            summary["saved_name"] = target.name
            summary["saved_path"] = str(target)
            summary["download_url"] = "/download/" + urllib.parse.quote(target.name)
            summary["hmi"] = hmi_summary
            summary["outputs"] = [output_info(target, "report"), output_info(hmi_target, "hmi")]
            summary["existing"] = existing_outputs(summary["record_date"])
            self.reply(200, summary)
        except DuplicateReportError as e:
            self.reply(409, {"error": str(e), "code": "duplicate_report", "existing": e.existing})
        except (ReportError, PermissionError, FileNotFoundError) as e:
            self.reply(400, {"error": str(e)})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.reply(400, {"error": "요청 또는 파일 내용이 올바르지 않습니다."})
        except Exception as e:
            print(f"Processing error: {type(e).__name__}: {e}", flush=True)
            self.reply(500, {"error": f"파일 처리 중 오류가 발생했습니다. {DATA / 'logs' / 'server.log'} 기록을 확인해 주세요."})


def main(argv=None):
    prepare_workspace()
    # pythonw has no console streams. Keep request/error logs available on disk.
    if sys.stdout is None or sys.stderr is None:
        log_folder = DATA / "logs"
        log_folder.mkdir(exist_ok=True)
        log = (log_folder / "server.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        httpd = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with urllib.request.urlopen(url + "/api/status", timeout=2) as response:
                existing = json.load(response)
                ours = same_profile(existing)
            if ours:
                print(f"Already running: {url}", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return
        except (OSError, ValueError):
            pass
        print(f"Port {args.port} is already in use. Open http://127.0.0.1:{args.port} or use --port 8766.", flush=True)
        raise SystemExit(1)
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        write_json(SERVER_INFO, {"profile": str(DATA), "workspace": str(ROOT), "port": httpd.server_port, "pid": os.getpid(), "version": APP_VERSION})
        print(f"Songdo daily report: {url}\nSettings: {CONFIG}\nPress Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            saved = json.loads(SERVER_INFO.read_text(encoding="utf-8"))
            if saved.get("pid") == os.getpid() and saved.get("port") == httpd.server_port:
                SERVER_INFO.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass


if __name__ == "__main__":
    main()
