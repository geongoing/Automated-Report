"""Per-user folder settings and Windows folder selection."""
import json
from pathlib import Path
import threading
from folder_picker import choose_folder
from report import ReportError
import updater
from runtime import APP_VERSION, CONFIG, DATA, FROZEN, FOLDER_KEYS, HMI_TEMPLATE, write_json
PICK_LOCK = threading.Lock()


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
            "update": updater.update_status(APP_VERSION),
            "execution_mode": "exe" if FROZEN else "python"}


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
    write_json(CONFIG, config)
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
