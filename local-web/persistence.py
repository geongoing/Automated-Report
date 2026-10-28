"""Durable state writes and three validated, distinct recovery snapshots."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

BACKUP_COUNT = 3
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_SNAPSHOT = re.compile(r"good-\d{20}-[0-9a-f]{16}\.json\Z")


class StateError(ValueError):
    pass


def state_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_bytes(path, data):
    """Flush a uniquely named sibling file before replacing one state file."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def write_json(path, value):
    with state_lock(path):
        atomic_bytes(path, json_bytes(value))


def backup_folder(path):
    path = Path(path)
    return path.parent / "backups" / path.stem


def snapshots(path):
    folder = backup_folder(path)
    return sorted((p for p in folder.glob("good-*.json") if _SNAPSHOT.fullmatch(p.name)), reverse=True)


def decode(raw, validate):
    try:
        return validate(json.loads(raw.decode("utf-8")))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise StateError("저장 파일의 내용을 확인할 수 없습니다.") from error


def preserve(path, raw):
    folder = backup_folder(path)
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    for saved in snapshots(path):
        if saved.name.endswith("-" + digest + ".json") and saved.read_bytes() == raw:
            return
    target = folder / f"good-{time.time_ns():020d}-{digest}.json"
    atomic_bytes(target, raw)


def trim_snapshots(path):
    kept = 0
    for saved in snapshots(path):
        raw = saved.read_bytes()
        if saved.name.endswith("-" + hashlib.sha256(raw).hexdigest()[:16] + ".json") and kept < BACKUP_COUNT:
            kept += 1
        else:
            saved.unlink()


def read_backed_json(path, validate, default):
    path = Path(path)
    with state_lock(path):
        raw = None
        if path.exists():
            raw = path.read_bytes()
            try:
                value = decode(raw, validate)
            except StateError:
                pass
            else:
                # Reading a healthy existing installation seeds its first backup.
                # Read-only media must still permit access to healthy settings.
                try:
                    preserve(path, raw)
                    trim_snapshots(path)
                except OSError:
                    print(f"백업 보관 실패: {path.name}", flush=True)
                return value
        for saved in snapshots(path):
            try:
                restored = saved.read_bytes()
                if not saved.name.endswith("-" + hashlib.sha256(restored).hexdigest()[:16] + ".json"):
                    continue
                value = decode(restored, validate)
            except (OSError, StateError):
                continue
            if raw is not None:
                atomic_bytes(backup_folder(path) / "damaged.json", raw)
            atomic_bytes(path, restored)
            print(f"정상 백업으로 복구했습니다: {path.name}", flush=True)
            return value
        if raw is None:
            if not snapshots(path):
                return default()
        raise StateError(f"{path.name}: 읽을 수 있는 정상 백업이 없습니다.")


def write_backed_json(path, value, validate):
    path = Path(path)
    with state_lock(path):
        raw = json_bytes(value)
        decode(raw, validate)
        if path.exists():
            old = path.read_bytes()
            try:
                decode(old, validate)
            except StateError:
                folder = backup_folder(path)
                folder.mkdir(parents=True, exist_ok=True)
                atomic_bytes(folder / "damaged.json", old)
            else:
                preserve(path, old)
        # A confirmed save already has a durable, validated recovery copy.
        preserve(path, raw)
        atomic_bytes(path, raw)
        trim_snapshots(path)
