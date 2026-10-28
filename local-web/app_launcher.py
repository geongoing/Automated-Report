"""Windowless local server launcher; standard library only."""
import argparse
import ctypes
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

from runtime import ROOT, DATA, APP_VERSION, FROZEN, SERVER_INFO, prepare_workspace, same_path, same_profile
from maintenance import BoundedLog, process_alive

DEFAULT_PORT = 8765


def status(url):
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=0.8) as response:
            data = json.load(response)
        return data if same_profile(data) else None
    except (OSError, ValueError, TypeError):
        return False


def server_url(port):
    return f"http://127.0.0.1:{port}"


def candidate_ports(explicit=None):
    if explicit is not None:
        return [explicit]
    ports = []
    try:
        saved = json.loads(SERVER_INFO.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and same_path(saved.get("profile"), DATA) and type(saved.get("port")) is int and 0 < saved["port"] < 65536:
            ports.append(saved["port"])
    except (OSError, ValueError, TypeError):
        pass
    return list(dict.fromkeys(ports + list(range(DEFAULT_PORT, DEFAULT_PORT + 32))))


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def select_server(explicit=None):
    first_free = None
    for port in candidate_ports(explicit):
        if port_available(port):
            if first_free is None:
                first_free = port
            continue
        existing = status(server_url(port))
        if existing:
            return port, existing
    if first_free is None:
        raise RuntimeError("사용할 수 있는 실행 주소가 없습니다. 다른 프로그램의 사용 여부를 확인해 주세요.")
    return first_free, None


def version_tuple(value):
    try:
        return tuple(int(part) for part in value.split("."))
    except (ValueError, AttributeError):
        return ()


def stop_server(url):
    before = json.loads(SERVER_INFO.read_text(encoding="utf-8")) if SERVER_INFO.exists() else {}
    request = urllib.request.Request(url + "/api/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except urllib.error.HTTPError as e:
        if e.code == 409:
            from updater import UpdateDeferred
            raise UpdateDeferred("일지 생성 중입니다. 작업 완료 후 다시 실행해 주세요.") from e
        raise
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            marker = json.loads(SERVER_INFO.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        if not status(url):
            if not before or marker.get("pid") != before.get("pid"):
                return
            if not process_alive(before.get("pid")):
                # A terminated process can leave its marker behind. Never wait
                # forever or remove a replacement server's freshly written marker.
                if marker == before:
                    SERVER_INFO.unlink(missing_ok=True)
                return
        time.sleep(0.1)
    raise RuntimeError("기존 프로그램이 작업을 마치고 종료하기를 기다리고 있습니다. 잠시 후 다시 실행해 주세요.")


def start_server(port, installed_executable=None):
    environment = dict(os.environ)
    if FROZEN:
        command = [str(installed_executable or sys.executable), "--serve", "--port", str(port)]
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    else:
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            executable = Path(sys.executable)
        command = [str(executable), "-B", str(Path(__file__).resolve()), "--serve", "--port", str(port)]
    process = subprocess.Popen(command,
                               cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if status(server_url(port)):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.15)
    raise RuntimeError("서버 시작을 확인하지 못했습니다. 잠시 후 다시 실행해 주세요.")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-installed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-update", action="store_true", help="저장된 버전으로 실행")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    args = parser.parse_args()
    if args.port is not None and not 0 < args.port < 65536:
        raise ValueError("실행 주소의 포트 번호는 1~65535여야 합니다.")
    return args


def start_local(args):
    installed_executable = getattr(args, "installed_executable", None)
    desired_executable = installed_executable or sys.executable
    port, existing = select_server(args.port)
    url = server_url(port)
    if existing and (version_tuple(existing.get("version")) < version_tuple(APP_VERSION) or
                     version_tuple(existing.get("version")) == version_tuple(APP_VERSION) and FROZEN and
                     (existing.get("execution_mode") != "exe" or not same_path(existing.get("executable"), desired_executable))):
        stop_server(url)
        existing = None
    if not existing:
        prepare_workspace()
        for attempt in range(3):
            if start_server(port, installed_executable):
                break
            if args.port is not None or port_available(port) or attempt == 2:
                raise RuntimeError("서버를 시작하지 못했습니다. 오류 기록을 확인해 주세요.")
            port, existing = select_server()
            url = server_url(port)
            if existing:
                break
    with urllib.request.urlopen(url + "/api/settings", timeout=3) as response:
        config = json.load(response)
    if not config.get("hmi_available"):
        raise RuntimeError("프로그램에 포함된 HMI 양식을 확인하지 못했습니다.")
    return url


def main():
    args = parse_args()
    if args.stop:
        for port in candidate_ports(args.port):
            url = server_url(port)
            if status(url):
                stop_server(url)
                break
        return
    if args.serve:
        prepare_workspace()
        import server
        server.main(["--port", str(args.port or DEFAULT_PORT), "--no-browser"])
        return
    prepare_workspace()
    if FROZEN and not args.run_installed:
        from updater import launch
        url = launch(APP_VERSION, args, start_local)
    else:
        url = start_local(args)
    if not args.no_browser:
        webbrowser.open(url)


def entrypoint():
    if FROZEN:
        name = "server.log" if "--serve" in sys.argv else ("updater.log" if "--managed" in sys.argv or "--run-installed" in sys.argv else "launcher.log")
        sys.stdout = sys.stderr = BoundedLog(DATA / "logs" / name)
    try:
        main()
    except Exception as error:
        from updater import UpdateDeferred
        message = f"{error}\n\n오류 기록: {DATA / 'logs'}"
        if any(flag in sys.argv for flag in ("--serve", "--run-installed", "--managed")):
            if sys.stderr is not None:
                print(message, file=sys.stderr, flush=True)
        elif os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "송도지소 일지 작성", 0x10)
        raise SystemExit(2 if isinstance(error, UpdateDeferred) else 1)
