"""Local HTTP endpoints and lifecycle; business services live in separate modules."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from app_settings import settings, save_settings, select_folder, output_folder, hmi_output_folder
from drafts import get_draft, save_draft
from report_files import catalog, auto_pair, source, hmi_template, hmi_profile, hmi_source_input
from report_storage import DuplicateReportError, existing_outputs, output_info, save_report_pair, recover_pending
from report import ReportError, generate_with_values, prepare
from hmi import render_hmi
from maintenance import BoundedLog
import updater
from runtime import ROOT, RESOURCES, DATA, CONFIG, APP_VERSION, FROZEN, SERVER_INFO, write_json, same_profile, prepare_workspace
sys.dont_write_bytecode = True
APP = RESOURCES
MAX_REQUEST = 32 * 1024 * 1024
JOB_LOCK = threading.Lock()
ACTIVE_JOBS = 0
SHUTTING_DOWN = False


def recover_on_startup():
    global ACTIVE_JOBS
    try:
        recover_pending()
    finally:
        with JOB_LOCK:
            ACTIVE_JOBS -= 1


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False
    # Windows permits duplicate listeners with SO_REUSEADDR. Claim this port
    # exclusively so a second launch can safely reopen the existing app.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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
                                    "busy": ACTIVE_JOBS > 0, "executable": str(Path(sys.executable).resolve()),
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
            try:
                parsed = urllib.parse.urlsplit(origin)
                return (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
                        and parsed.port == self.server.server_port and parsed.username is None
                        and not parsed.path and not parsed.query and not parsed.fragment)
            except ValueError:
                return False
        return True

    def do_POST(self):
        global ACTIVE_JOBS
        generating = self.path == "/api/generate" and self.local_request()
        if generating:
            with JOB_LOCK:
                if SHUTTING_DOWN:
                    return self.reply(409, {"error": "프로그램을 다시 시작하고 있습니다. 잠시 후 다시 실행해 주세요."})
                ACTIVE_JOBS += 1
        try:
            self.process_post()
        finally:
            if generating:
                with JOB_LOCK:
                    ACTIVE_JOBS -= 1

    def process_post(self):
        global SHUTTING_DOWN
        try:
            if not self.local_request():
                return self.reply(403, {"error": "로컬 웹 화면에서 실행해 주세요."})
            if self.path not in ("/api/settings", "/api/select-folder", "/api/update/check", "/api/inspect", "/api/preview", "/api/generate", "/api/hmi/profile", "/api/shutdown", "/api/draft"):
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
                with JOB_LOCK:
                    if ACTIVE_JOBS:
                        return self.reply(409, {"error": "일지 생성 중입니다. 작업이 끝난 뒤 종료하거나 업데이트해 주세요."})
                    SHUTTING_DOWN = True
                self.reply(200, {"message": "서버를 종료했습니다. 이 창을 닫아도 됩니다."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/api/settings":
                return self.reply(200, save_settings(body.get("raw_folder", body.get("report_folder")), body.get("final_folder"), body.get("hmi_folder")))
            if self.path == "/api/select-folder":
                return self.reply(200, select_folder(body))
            if self.path == "/api/update/check":
                return self.reply(200, updater.check_updates(APP_VERSION))
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
    global ACTIVE_JOBS
    prepare_workspace()
    # pythonw has no console streams. Keep request/error logs available on disk.
    if sys.stdout is None or sys.stderr is None:
        log = BoundedLog(DATA / "logs" / "server.log")
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
    recovery = None
    try:
        write_json(SERVER_INFO, {"profile": str(DATA), "workspace": str(ROOT), "port": httpd.server_port, "pid": os.getpid(), "version": APP_VERSION,
                                 "executable": str(Path(sys.executable).resolve())})
        print(f"Songdo daily report: {url}\nSettings: {CONFIG}\nPress Ctrl+C to stop.", flush=True)
        with JOB_LOCK:
            ACTIVE_JOBS += 1
        recovery = threading.Thread(target=recover_on_startup)
        recovery.start()
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if recovery:
            recovery.join()
        try:
            saved = json.loads(SERVER_INFO.read_text(encoding="utf-8"))
            if saved.get("pid") == os.getpid() and saved.get("port") == httpd.server_port:
                SERVER_INFO.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass


if __name__ == "__main__":
    main()
