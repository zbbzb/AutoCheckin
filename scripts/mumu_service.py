"""Loopback-only dashboard and durable weekday scheduler, no third-party web stack."""
import argparse
import ctypes
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from mumu_common import (ROOT, SERVICE_LOCK, STOP_DONE, NO_WINDOW, clear_pid, clear_stopped,
                         database, due_job, mark_stopped, now, plan, read_config, running, save_config,
                         write_pid)

PORT = 18765
TOKEN = secrets.token_urlsafe(32)
GUARD = threading.RLock()
CHILDREN = {}
STARTED = time.time()
SHUTDOWN = threading.Event()
SHUTDOWN_KIND = None
SERVER = None
# Restarting in-process is impossible: the listening socket and the scheduled task
# both belong to this process. A detached helper waits for the port to free up and
# then starts the task again, which re-runs the guard.
def log_service(message):
    """Append to logs/service.log with a timestamp and flush, so a stuck shutdown is visible."""
    try:
        with (ROOT / "logs/service.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[{now().isoformat(timespec='seconds')}] {message}\n")
            stream.flush()
    except Exception:
        pass


RESTART_SCRIPT = ROOT / "scripts/mumu_restart.ps1"


def single_instance_server():
    """A server whose bind fails when the port is already taken.

    ThreadingHTTPServer inherits allow_reuse_address = 1 (SO_REUSEADDR). On Windows
    that lets a second process bind the same address, so a duplicate scheduled launch
    would silently run a second scheduler against the same database. Turning it off
    makes the bind raise OSError, which is the real single-instance guard.

    daemon_threads stays False on purpose: every accepted connection must be serviced
    to completion while we run. Shutdown instead happens from a background thread and
    the process itself is exited explicitly (see main), so lingering keep-alive
    threads can never hold a stopped service hostage.
    """
    class SingleInstanceServer(ThreadingHTTPServer):
        allow_reuse_address = False
    return SingleInstanceServer


def shutdown_watchdog(server, timeout=20):
    """Hard-stop the process if the graceful path cannot report completion in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if STOP_DONE.exists():
            log_service("graceful shutdown complete")
            os._exit(0)
        time.sleep(0.5)
    log_service(f"graceful shutdown did not finish in {timeout}s; forcing exit")
    os._exit(0)


def initiate_shutdown(server, kind):
    """Run the graceful shutdown and then guarantee that this process really exits.

    Cancelling a live check-in makes the service wait for the worker and the emulator,
    and an unclosed browser connection can leave a handler thread alive, so "return
    from main()" alone is not a reliable stop signal.
    """
    log_service(f"shutdown requested: {kind}")
    threading.Thread(target=shutdown_watchdog, args=(server,), daemon=True).start()
    if kind == "stop":
        wait_for_exit(150)
        log_service("running work finished")
    clear_pid(SERVICE_LOCK, expected=os.getpid())
    STOP_DONE.write_text(f"{kind} {os.getpid()}\n", encoding="utf-8")
    log_service("shutdown finished; exiting")
    os._exit(0)


def request_shutdown(kind):
    """Stop the service (kind='stop') or hand over to a restart helper ('restart')."""
    global SHUTDOWN_KIND
    SHUTDOWN_KIND = kind
    with database() as db:
        if kind == "stop":
            db.execute("UPDATE jobs SET status='cancelled',message='服务已停止，未提交的流程取消' "
                       "WHERE status IN ('starting','running')")
    if kind == "stop":
        mark_stopped("control-panel")
    else:
        # A restart is an explicit request to run again, so drop any earlier stop.
        clear_stopped()
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                          "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(RESTART_SCRIPT), "-Port", str(PORT)],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    SHUTDOWN.set()


def process_alive(pid):
    if not pid:
        return False
    if pid in CHILDREN:
        return CHILDREN[pid].poll() is None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def launch(db, job_id, cfg):
    claimed = db.execute("UPDATE jobs SET status='starting',started=?,config_json=?,message='正在准备' WHERE id=? AND status='pending'",
                         (time.time(), json.dumps(cfg, ensure_ascii=False), job_id))
    db.commit()  # Child must see the claim before it starts.
    if claimed.rowcount != 1:
        # Another scheduler instance claimed this job first (or it was stopped).
        # Never start a second worker for the same job.
        return
    try:
        log = ROOT / "logs/service-worker.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            child = subprocess.Popen([sys.executable, str(ROOT / "scripts/mumu_worker.py"), "--job", str(job_id)],
                                     cwd=ROOT, stdout=stream, stderr=stream, creationflags=NO_WINDOW)
        CHILDREN[child.pid] = child
        db.execute("UPDATE jobs SET pid=? WHERE id=?", (child.pid, job_id))
    except Exception as exc:
        db.execute("UPDATE jobs SET status='failed',finished=?,message=? WHERE id=?",
                   (time.time(), f"无法启动流程: {exc}", job_id))


def cleanup_orphan(row):
    cfg = json.loads(row["config_json"])
    # Target the recorded instance only. Never shut down all players.
    result = subprocess.run([cfg["manager_path"], "info", "-v", str(cfg["vm_index"])],
                            capture_output=True, timeout=20, creationflags=NO_WINDOW)
    info = json.loads(result.stdout.decode("utf-8-sig"))
    if info.get("name") != cfg["vm_name"] or str(info.get("index")) != str(cfg["vm_index"]):
        raise RuntimeError("实例身份已变，未关闭")
    if info.get("is_process_started"):
        if info.get("adb_host_ip") == "127.0.0.1" and info.get("adb_port"):
            subprocess.run([str(ROOT / "android-sdk/platform-tools/adb.exe"), "-s", f"127.0.0.1:{int(info['adb_port'])}",
                            "shell", "am", "force-stop", cfg["package"]], capture_output=True, timeout=20, creationflags=NO_WINDOW)
        result = subprocess.run([cfg["manager_path"], "control", "-v", str(cfg["vm_index"]), "shutdown"],
                                capture_output=True, timeout=20, creationflags=NO_WINDOW)
        if result.returncode:
            raise RuntimeError("关闭模拟器命令失败")


def tick():
    with GUARD, database() as db:
        cfg = read_config()
        active = db.execute("SELECT * FROM jobs WHERE status IN ('starting','running') OR (status='cancelled' AND finished IS NULL)").fetchall()
        busy = False
        for row in active:
            if process_alive(row["pid"]):
                busy = True
                continue
            if time.time() - (row["started"] or time.time()) < 30:
                busy = True  # Allow a freshly claimed process time to start.
                continue
            message = "进程意外中断；本时段不自动重试"
            try:
                cleanup_orphan(row)
            except Exception as exc:
                message += f"；清理失败: {exc}"
            db.execute("UPDATE jobs SET status=?,finished=?,message=? WHERE id=?",
                       ("uncertain" if row["clicked"] else "failed", time.time(), message, row["id"]))
        plan(db, cfg)
        if not busy:
            job = due_job(db, cfg)
            if job:
                launch(db, job["id"], cfg)


def scheduler():
    while True:
        try:
            tick()
        except Exception:
            with (ROOT / "logs/service.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n{now().isoformat()}\n{traceback.format_exc()}")
        time.sleep(1)


def request_shutdown(kind):
    """Stop the service (kind='stop') or hand over to a restart helper ('restart')."""
    global SHUTDOWN_KIND
    SHUTDOWN_KIND = kind
    with database() as db:
        if kind == "stop":
            db.execute("UPDATE jobs SET status='cancelled',message='服务已停止，未提交的流程取消' "
                       "WHERE status IN ('starting','running')")
    if kind == "stop":
        mark_stopped("control-panel")
    else:
        # A restart is an explicit request to run again, so drop any earlier stop.
        clear_stopped()
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive",
                          "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(RESTART_SCRIPT), "-Port", str(PORT)],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    SHUTDOWN.set()


def health():
    return {"service": "running", "pid": os.getpid(), "port": PORT,
            "uptime": round(time.time() - STARTED, 1),
            "enabled": read_config()["enabled"], "token": TOKEN, "version": 1}


def state():
    with GUARD, database() as db:
        cfg = read_config()
        today = now().date().isoformat()
        jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE day=? AND mode='live' ORDER BY planned", (today,))]
        history = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status NOT IN ('pending','starting','running') ORDER BY COALESCE(finished,planned) DESC LIMIT 30")]
        active = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status IN ('starting','running') OR (status='cancelled' AND finished IS NULL)")]
        enabled = {s["id"] for s in cfg["slots"] if s["enabled"]}
        upcoming = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status='pending' AND window_end>? ORDER BY planned", (time.time(),)) if r["slot_id"] in enabled]
        for job in jobs + history + active + upcoming:
            job.pop("config_json", None)
            job["log_url"] = f"/api/log?id={job['id']}" if job["run_dir"] else None
        return {"config": cfg, "now": now().isoformat(), "today": jobs, "history": history,
                "active": active, "next": upcoming[0] if upcoming and cfg["enabled"] else None,
                "weekend": now().weekday() >= 5, "token": TOKEN,
                "service": "running", "version": 1}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "仅允许本机访问"})
        url = urlparse(self.path)
        if url.path == "/api/state":
            return self.send(200, state())
        if url.path == "/api/health":
            return self.send(200, health())
        if url.path == "/api/log":
            from urllib.parse import parse_qs
            try:
                job_id = int(parse_qs(url.query)["id"][0])
                with database() as db:
                    row = db.execute("SELECT run_dir FROM jobs WHERE id=?", (job_id,)).fetchone()
                from pathlib import Path
                folder = Path(row["run_dir"]).resolve()
                if not folder.is_relative_to((ROOT / "logs").resolve()):
                    raise ValueError("Invalid path")
                return self.send(200, (folder / "run.log").read_bytes(), "text/plain; charset=utf-8")
            except (ValueError, TypeError, KeyError, OSError):
                return self.send(404, {"error": "暂无详细日志"})
        assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
        if url.path not in assets:
            return self.send(404, {"error": "页面不存在"})
        path = ROOT / "web" / assets[url.path]
        return self.send(200, path.read_bytes(), (mimetypes.guess_type(path)[0] or "application/octet-stream") + "; charset=utf-8")

    def do_POST(self):
        origin = self.headers.get("Origin")
        if (not self.valid_host() or origin not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")
                or not secrets.compare_digest(self.headers.get("X-Checkin-Token", ""), TOKEN)):
            return self.send(403, {"error": "页面凭据已失效，请刷新页面"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 32768 or self.headers.get_content_type() != "application/json":
                raise ValueError("请求格式不正确")
            payload = json.loads(self.rfile.read(length) or b"{}")
            with GUARD:
                if self.path == "/api/config":
                    cfg = save_config(payload)
                    with database() as db:
                        plan(db, cfg)
                elif self.path == "/api/toggle":
                    cfg = read_config()
                    if type(payload.get("enabled")) is not bool:
                        raise ValueError("开关格式不正确")
                    cfg["enabled"] = payload["enabled"]
                    save_config(cfg)
                elif self.path == "/api/preview":
                    with database() as db:
                        if db.execute("SELECT 1 FROM jobs WHERE status IN ('starting','running') OR (status='cancelled' AND finished IS NULL)").fetchone():
                            raise ValueError("已有流程正在执行，请等待结束")
                        cfg = read_config()
                        upcoming = db.execute("SELECT planned FROM jobs WHERE status='pending' AND planned BETWEEN ? AND ?",
                                              (time.time(), time.time()+600)).fetchone()
                        if cfg["enabled"] and upcoming:
                            raise ValueError("十分钟内有定时签到，请在签到结束后演练")
                        stamp = time.time()
                        cursor = db.execute("""INSERT INTO jobs(day,slot_id,label,kind,window_start,window_end,planned,
                                             prepare_at,signature,created,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                            (now().date().isoformat(), "preview-"+secrets.token_hex(6), "手动演练", "out",
                                             stamp, stamp+600, stamp, stamp, "preview", stamp, "preview"))
                        launch(db, cursor.lastrowid, cfg)
                elif self.path == "/api/stop":
                    with database() as db:
                        db.execute("UPDATE jobs SET status='cancelled',message='正在停止并关闭模拟器' WHERE status IN ('starting','running')")
                elif self.path == "/api/service":
                    action = payload.get("action")
                    if action not in ("stop", "restart"):
                        raise ValueError("服务操作不存在")
                    # The stop marker, the detached helper and the actual shutdown are
                    # handled in one place, so every caller behaves identically.
                    threading.Thread(target=request_shutdown, args=(action,), daemon=True).start()
                else:
                    return self.send(404, {"error": "操作不存在"})
            return self.send(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as exc:
            return self.send(400, {"error": str(exc)})
        except Exception as exc:
            return self.send(500, {"error": f"操作失败: {exc}"})


def worker_alive():
    for child in list(CHILDREN.values()):
        if child.poll() is None:
            return True
    return False


def wait_for_exit(timeout):
    """Let a running worker cancel and clean up its emulator before we disappear."""
    deadline = time.monotonic() + timeout
    time.sleep(1)  # give the cancel a moment to reach the worker
    while time.monotonic() < deadline:
        with database() as db:
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('starting','running') "
                                 "OR (status='cancelled' AND finished IS NULL)").fetchone()[0]
        if not pending and not worker_alive():
            return True
        time.sleep(1)
    return False


def main():
    global PORT, SERVER
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    PORT = args.port
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    STOP_DONE.unlink(missing_ok=True)
    # Bind before touching running jobs. Duplicate scheduled launches exit quietly.
    try:
        server = single_instance_server()(("127.0.0.1", PORT), Handler)
    except OSError:
        return
    SERVER = server
    write_pid(SERVICE_LOCK, os.getpid())
    log_service(f"service started pid={os.getpid()} port={PORT}")
    with database() as db:
        plan(db, read_config())
    threading.Thread(target=scheduler, daemon=True).start()
    # Requests are handled one at a time: the dashboard polls every 3s and each
    # response is small, so serialising them keeps the shutdown decision unambiguous.
    while not SHUTDOWN.is_set():
        server.handle_request()
    server.server_close()
    initiate_shutdown(server, SHUTDOWN_KIND or "stop")


if __name__ == "__main__":
    main()
