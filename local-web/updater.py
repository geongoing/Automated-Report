"""Public GitHub release updates; all mutable files stay in the user profile."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from runtime import DATA, FROZEN, SERVER_INFO, same_path, same_profile, write_json
from maintenance import cleanup_versions, is_link

REPOSITORY = "geongoing/Automated-Report"
RELEASES = "https://github.com/" + REPOSITORY + "/releases"
MANIFEST_URL = RELEASES + "/latest/download/update.json"
ASSET = "SongdoDaily.exe"
VERSIONS = DATA / "app" / "versions"
STATE = DATA / "app-state.json"
UPDATE_STATUS = DATA / "update-status.json"
MAX_EXE = 80 * 1024 * 1024
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


class UpdateError(ValueError):
    pass


class UpdateDeferred(UpdateError):
    pass


def version(value):
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise UpdateError("정식 버전 번호가 올바르지 않습니다.")
    return tuple(map(int, value.split(".")))


def read_json(path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else ({} if default is None else default)
    except (OSError, ValueError):
        return {} if default is None else default


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(256 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def release_url(value):
    version(value)
    return RELEASES + "/tag/v" + value


def update_status(current):
    saved = read_json(UPDATE_STATUS)
    latest = saved.get("latest_version", "")
    try:
        available = version(latest) > version(current)
    except UpdateError:
        latest, available = "", False
    return {"current_version": current, "latest_version": latest,
            "available": available, "checked_at": saved.get("checked_at"),
            "message": saved.get("message", "EXE 실행 시 새 정식 버전을 자동으로 확인합니다."),
            "release_url": release_url(latest or current), "supported": FROZEN and os.name == "nt"}


def save_status(message, latest=None, *, checked=False):
    value = read_json(UPDATE_STATUS)
    value["message"] = message
    if latest is not None:
        value["latest_version"] = latest
    if checked:
        value["checked_at"] = int(time.time() * 1000)
    write_json(UPDATE_STATUS, value)


@contextmanager
def update_lock(timeout=65):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "updater.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise UpdateDeferred("다른 실행 창에서 업데이트를 처리하고 있습니다.")
                time.sleep(.15)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "github.com" or host.endswith(".githubusercontent.com")):
            raise UpdateError("허용되지 않은 업데이트 다운로드 주소입니다.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def open_download(url, timeout=4):
    request = urllib.request.Request(url, headers={"User-Agent": "SongdoDaily-Updater", "Cache-Control": "no-cache"})
    return urllib.request.build_opener(TrustedRedirect()).open(request, timeout=timeout)


def validate_manifest(data):
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("channel") != "stable" or data.get("platform") != "windows-x64":
        raise UpdateError("업데이트 정보의 형식이 올바르지 않습니다.")
    version(data.get("version"))
    if data.get("asset") != ASSET or not isinstance(data.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise UpdateError("업데이트 파일 검증 정보가 올바르지 않습니다.")
    if type(data.get("size")) is not int or not 0 < data["size"] <= MAX_EXE:
        raise UpdateError("업데이트 파일 크기가 올바르지 않습니다.")
    return {key: data[key] for key in ("schema", "channel", "platform", "version", "asset", "sha256", "size")}


def fetch_manifest():
    # The public download URL avoids the anonymous REST API's shared rate limit.
    with open_download(MANIFEST_URL) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise UpdateError("업데이트 정보가 너무 큽니다.")
    return validate_manifest(json.loads(raw))


def check_updates(current):
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with update_lock(timeout=.2):
            manifest = fetch_manifest()
            message = ("새 정식 버전이 있습니다. EXE를 다시 실행하면 적용합니다."
                       if version(manifest["version"]) > version(current) else "최신 정식 버전입니다.")
            if same_build(manifest, read_json(STATE).get("failed")):
                message = "이 버전은 실행 확인에 실패해 적용을 보류했습니다. 기존 버전으로 계속 사용할 수 있습니다."
            save_status(message, manifest["version"], checked=True)
    except UpdateDeferred:
        pass
    except Exception:
        save_status("업데이트 서버에 연결하지 못했습니다. 현재 버전으로 계속 사용할 수 있습니다.", checked=True)
    return update_status(current)


def validate_exe(path):
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise UpdateError("Windows 실행 파일이 아닙니다.")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= min(path.stat().st_size - 96, 1024 * 1024):
            raise UpdateError("실행 파일 헤더가 올바르지 않습니다.")
        source.seek(offset)
        pe = source.read(96)
        if pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664 or struct.unpack_from("<H", pe, 92)[0] != 2:
            raise UpdateError("Windows 64비트 창 모드 프로그램이 아닙니다.")


def record_path(value, sha):
    version(value)
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UpdateError("실행 파일 검증값이 올바르지 않습니다.")
    return VERSIONS / (value + "-" + sha[:16]) / ASSET


def valid_record(record):
    try:
        target = record_path(record["version"], record["sha256"])
        # MSIX can redirect the leaf file while its parent directory keeps its
        # logical path. Check the known parent boundary and reject file links;
        # do not require the redirected leaf's resolved string to share a prefix.
        if (not same_path(record["path"], target) or is_link(target) or
                not target.parent.resolve().is_relative_to(VERSIONS.resolve())):
            return False
        return target.is_file() and target.stat().st_size <= MAX_EXE and digest(target) == record["sha256"]
    except (OSError, ValueError, TypeError, KeyError):
        return False


def record(value, sha):
    return {"version": value, "sha256": sha, "path": str(record_path(value, sha))}


def bundled_record(current):
    source = Path(sys.executable).resolve()
    sha = digest(source)
    item = record(current, sha)
    target = Path(item["path"])
    if not same_path(source, target) and not valid_record(item):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        try:
            shutil.copyfile(source, temporary)
            if digest(temporary) != sha:
                raise UpdateError("프로그램 복사 내용을 확인하지 못했습니다.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return item


def download_release(manifest):
    item = record(manifest["version"], manifest["sha256"])
    if valid_record(item):
        return item
    target = Path(item["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    url = RELEASES + "/download/v" + manifest["version"] + "/" + ASSET
    deadline = time.monotonic() + 60
    total = 0
    try:
        with open_download(url, timeout=8) as response, temporary.open("wb") as output:
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("업데이트 다운로드 시간이 초과되었습니다.")
                block = response.read(256 * 1024)
                if not block:
                    break
                total += len(block)
                if total > manifest["size"]:
                    raise UpdateError("업데이트 파일 크기가 일치하지 않습니다.")
                output.write(block)
        if total != manifest["size"] or digest(temporary) != manifest["sha256"]:
            raise UpdateError("업데이트 파일 검증에 실패했습니다.")
        validate_exe(temporary)
        os.replace(temporary, target)
        return item
    finally:
        temporary.unlink(missing_ok=True)


def live_server():
    marker = read_json(SERVER_INFO)
    port = marker.get("port")
    if type(port) is not int or not 0 < port < 65536:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
            info = json.load(response)
        if same_profile(info):
            return {**info, "url": f"http://127.0.0.1:{port}"}
    except (OSError, ValueError):
        pass
    return None


def run_record(item, args, *, managed=False):
    if not valid_record(item):
        raise UpdateError("저장된 실행 파일이 손상되었거나 없습니다.")
    environment = dict(os.environ)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    executable = record_path(item["version"], item["sha256"])
    command = [str(executable), "--managed" if managed else "--run-installed", "--no-browser"]
    if args.port is not None:
        command += ["--port", str(args.port)]
    if args.skip_update:
        command.append("--skip-update")
    try:
        result = subprocess.run(command, cwd=executable.parent, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=125 if managed else 30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError("새 프로그램의 실행을 확인하지 못했습니다.") from e
    if result.returncode == 2:
        raise UpdateDeferred("일지 생성 중이므로 업데이트를 다음 실행으로 미뤘습니다.")
    info = live_server()
    if result.returncode or not info or (not managed and (info.get("version") != item["version"] or not same_path(info.get("executable"), executable))):
        raise UpdateError("새 프로그램의 시작 확인에 실패했습니다.")
    return info["url"]


def activate(item):
    state = read_json(STATE)
    previous = state.get("active")
    if not same_build(previous, item):
        failed = state.get("failed", {})
        if valid_record(previous) and not same_build(previous, failed):
            state["previous"] = previous
    state["active"] = item
    write_json(STATE, state)
    live = live_server()
    cleanup_versions(VERSIONS, [item.get("path") for key in ("active", "previous") if isinstance(item := state.get(key), dict)] +
                     [sys.executable, live.get("executable") if live else None])


def same_build(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and left.get("version") == right.get("version") and left.get("sha256") == right.get("sha256")


def managed_launch(current, args, start_local):
    own = bundled_record(current)
    args.installed_executable = own["path"]
    with update_lock():
        state = read_json(STATE)
        active = state.get("active")
        if valid_record(active) and version(active["version"]) > version(current):
            return run_record(active, args)
        candidate = None
        if not args.skip_update:
            try:
                manifest = fetch_manifest()
                latest = manifest["version"]
                save_status("최신 정식 버전입니다.", latest, checked=True)
                if version(latest) > version(current):
                    failed = state.get("failed", {})
                    if failed.get("version") == latest and failed.get("sha256") == manifest["sha256"]:
                        save_status("새 버전의 실행에 문제가 있어 검증된 기존 버전을 사용합니다.", latest)
                    else:
                        candidate = download_release(manifest)
            except Exception as e:
                print(f"Update check: {type(e).__name__}", flush=True)
                save_status("업데이트를 확인하지 못했습니다. 기존 버전으로 실행했습니다.", checked=True)
        if candidate:
            live = live_server()
            if live and live.get("busy"):
                save_status("일지 생성 중입니다. 업데이트는 다음 EXE 실행 시 적용합니다.", candidate["version"])
                return live["url"]
            try:
                url = run_record(candidate, args)
            except UpdateDeferred:
                save_status("일지 생성 중입니다. 업데이트는 다음 EXE 실행 시 적용합니다.", candidate["version"])
                live = live_server()
                if live:
                    return live["url"]
            except Exception as e:
                state = read_json(STATE)
                state["failed"] = {"version": candidate["version"], "sha256": candidate["sha256"]}
                write_json(STATE, state)
                print(f"Update rollback: {type(e).__name__}", flush=True)
                save_status("새 버전의 실행에 실패해 기존 버전으로 복구했습니다.", candidate["version"])
            else:
                activate(candidate)
                save_status("새 정식 버전으로 업데이트했습니다.", candidate["version"])
                return url
        try:
            url = start_local(args)
        except Exception:
            state = read_json(STATE)
            previous = state.get("previous")
            if not valid_record(previous) or previous == own:
                raise
            url = run_record(previous, args)
            state["failed"] = {"version": own["version"], "sha256": own["sha256"]}
            write_json(STATE, state)
            activate(previous)
            save_status("프로그램 시작에 실패해 이전 버전으로 복구했습니다.")
            return url
        activate(own)
        return url


def launch(current, args, start_local):
    """External EXEs delegate to an installed copy so its updater can evolve too."""
    DATA.mkdir(parents=True, exist_ok=True)
    if args.managed:
        return managed_launch(current, args, start_local)
    with update_lock():
        own = bundled_record(current)
        state = read_json(STATE)
        choices = [state.get(key) for key in ("active", "previous")
                   if valid_record(state.get(key)) and not same_build(state.get(key), state.get("failed"))]
        if not same_build(own, state.get("failed")) or not choices:
            choices.append(own)
        best = max(choices, key=lambda item: version(item["version"]))
        # An explicitly downloaded EXE with an equal version wins over an old build.
        if own in choices and version(own["version"]) == version(best["version"]):
            best = own
    try:
        if same_build(best, own):
            # This process already contains the selected updater. Avoid starting
            # an identical one-file controller (and extracting it) a second time.
            return managed_launch(current, args, start_local)
        return run_record(best, args, managed=True)
    except UpdateDeferred:
        live = live_server()
        if live:
            return live["url"]
        raise
    except Exception:
        fallback = [item for item in choices if item != best]
        if not fallback:
            raise
        fallback = max(fallback, key=lambda item: version(item["version"]))
        with update_lock():
            state = read_json(STATE)
            state["failed"] = {"version": best["version"], "sha256": best["sha256"]}
            write_json(STATE, state)
            url = run_record(fallback, args)
            activate(fallback)
            save_status("저장된 프로그램을 실행하지 못해 기본 버전으로 복구했습니다.")
            return url
