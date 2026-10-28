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

from runtime import ROOT, DATA, RESOURCES, FROZEN, SERVER_INFO, prepare_workspace, same_profile

APP_VERSION = json.loads((RESOURCES / "version.json").read_text(encoding="utf-8"))["version"]
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
        if isinstance(saved, dict) and saved.get("profile") == str(DATA) and type(saved.get("port")) is int and 0 < saved["port"] < 65536:
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
    request = urllib.request.Request(url + "/api/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
    deadline = time.monotonic() + 10
    while status(url) and time.monotonic() < deadline:
        time.sleep(0.1)


def start_server(port):
    log_folder = DATA / "logs"
    log_folder.mkdir(exist_ok=True)
    environment = dict(os.environ)
    if FROZEN:
        command = [sys.executable, "--serve", "--port", str(port)]
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    else:
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            executable = Path(sys.executable)
        command = [str(executable), "-B", str(Path(__file__).resolve()), "--serve", "--port", str(port)]
    with (log_folder / "server.log").open("ab") as log:
        process = subprocess.Popen(command,
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=APP_VERSION)
    args = parser.parse_args()
    if args.port is not None and not 0 < args.port < 65536:
        raise ValueError("실행 주소의 포트 번호는 1~65535여야 합니다.")
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
    port, existing = select_server(args.port)
    url = server_url(port)
    if existing and (version_tuple(existing.get("version")) < version_tuple(APP_VERSION) or
                     version_tuple(existing.get("version")) == version_tuple(APP_VERSION) and FROZEN and existing.get("execution_mode") != "exe"):
        stop_server(url)
        existing = None
    if not existing:
        prepare_workspace()
        for attempt in range(3):
            if start_server(port):
                break
            # Another folder may have selected this free port at the same time.
            if args.port is not None or port_available(port) or attempt == 2:
                raise RuntimeError("서버를 시작하지 못했습니다. 오류 기록을 확인해 주세요.")
            port, existing = select_server()
            url = server_url(port)
            if existing:
                break
        else:
            raise RuntimeError("사용할 수 있는 실행 주소를 찾지 못했습니다.")
    if not args.no_browser:
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = f"{error}\n\n오류 기록: {DATA / 'logs'}"
        if "--serve" in sys.argv:
            if sys.stderr is not None:
                print(message, file=sys.stderr, flush=True)
        elif os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "송도지소 일지 작성", 0x10)
        raise SystemExit(1)
