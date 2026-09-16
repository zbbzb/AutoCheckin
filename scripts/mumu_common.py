"""Configuration, durable schedule and run journal for the local controller."""
import contextlib
import copy
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIG = ROOT / "config/mumu.json"
DATABASE = DATA / "checkin.sqlite3"
STOP_MARKER = DATA / "service.stop"
STOP_DONE = DATA / "service.stop.done"
SERVICE_LOCK = DATA / "service.pid"
TRAY_LOCK = DATA / "tray.pid"
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TERMINAL = {"success", "preview", "failed", "uncertain", "missed", "cancelled"}


def boot_id():
    """Identify the current Windows boot session.

    GetTickCount64 counts milliseconds since boot, so the derived boot instant is
    stable while the machine stays up and changes on the next boot. The manual stop
    marker is tied to it, so "stop the service" lasts until the next restart.
    """
    if os.name != "nt":
        return "non-windows"
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    tick = int(kernel.GetTickCount64())
    return str(int(time.time() - tick / 1000))


def stop_requested(marker_path=None):
    """True when the service was stopped by hand during this boot session."""
    path = Path(marker_path) if marker_path else STOP_MARKER
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return marker.get("boot") == boot_id()


def mark_stopped(reason, marker_path=None):
    """Suppress automatic starts until the next boot."""
    path = Path(marker_path) if marker_path else STOP_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "boot": boot_id(), "at": now().isoformat(timespec="seconds"), "reason": reason},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_stopped(marker_path=None):
    try:
        (Path(marker_path) if marker_path else STOP_MARKER).unlink()
    except FileNotFoundError:
        pass


def read_pid(path):
    """Read a pid written by a long-running process, or None when it is gone."""
    try:
        value = int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value or None


def write_pid(path, pid):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="utf-8")


def clear_pid(path, expected=None):
    """Remove a pid file, optionally only when it still holds the expected pid.

    The ownership check matters because a replacement process can write its own pid
    between our shutdown decision and our cleanup; unconditionally deleting the file
    would then erase the live instance's record.
    """
    path = Path(path)
    if expected is not None and read_pid(path) != int(expected):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def process_running(pid):
    """True only when a live process really owns that pid.

    Windows recycles pids, so "the pid exists" is not enough: a recycled pid would
    make a dead service look alive and permanently block restarts. The tasklist row
    is parsed for the exact pid so a substring match cannot produce a false positive.
    """
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                            capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode:
        return False
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == str(int(pid)):
            return True
    return False


def running(lock_path):
    """Self-healing liveness check for a pid file: drop the file once it is stale."""
    pid = read_pid(lock_path)
    if not pid:
        return None
    if process_running(pid):
        return pid
    clear_pid(lock_path)
    return None


def now():
    return datetime.now(TZ)


def validate_config(value):
    cfg = copy.deepcopy(value)
    for key in ("enabled", "silent"):
        if type(cfg.get(key)) is not bool:
            raise ValueError(f"{key} 必须是开关值")
    for key, low, high in (("random_minutes", 1, 30), ("prepare_seconds", 60, 600), ("vm_index", 0, 999)):
        if type(cfg.get(key)) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} 应在 {low}–{high} 之间")
    for key in ("manager_path", "vm_name", "package", "organization"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError(f"{key} 不能为空")
    if not re.fullmatch(r"[a-zA-Z0-9_.]+", cfg["package"]):
        raise ValueError("应用包名格式不正确")
    for key, limit in (("longitude", 180), ("latitude", 90)):
        val = cfg.get("location", {}).get(key)
        if type(val) not in (float, int) or not math.isfinite(val) or not -limit <= val <= limit:
            raise ValueError("定位经纬度格式不正确")
    if not isinstance(cfg.get("slots"), list) or len(cfg["slots"]) != 6:
        raise ValueError("需要保留六个时段，可以单独关闭")
    ids, intervals = set(), []
    for slot in cfg["slots"]:
        if not re.fullmatch(r"[a-z0-9-]{1,40}", slot.get("id", "")) or slot["id"] in ids:
            raise ValueError("时段编号无效或重复")
        ids.add(slot["id"])
        if slot.get("kind") not in ("in", "out") or type(slot.get("enabled")) is not bool:
            raise ValueError("时段类型或开关无效")
        if not isinstance(slot.get("label"), str) or not 1 <= len(slot["label"]) <= 30:
            raise ValueError("时段名称需为 1–30 字")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", slot.get("time", "")):
            raise ValueError("时间请使用 HH:MM 格式")
        hour, minute = map(int, slot["time"].split(":"))
        base = hour * 60 + minute
        start = base - cfg["random_minutes"] if slot["kind"] == "in" else base
        end = base if slot["kind"] == "in" else base + cfg["random_minutes"]
        if start < 0 or end >= 1440:
            raise ValueError("随机时间窗口不能跨越午夜")
        if slot["enabled"]:
            # One emulator cannot prepare two attendance events simultaneously.
            intervals.append((start * 60 - cfg["prepare_seconds"], end * 60, slot["label"]))
    intervals.sort()
    for left, right in zip(intervals, intervals[1:]):
        if left[1] >= right[0]:
            raise ValueError(f"{left[2]} 与 {right[2]} 的准备/签到时间重叠")
    return cfg


def read_config():
    return validate_config(json.loads(CONFIG.read_text(encoding="utf-8-sig")))


def save_config(cfg):
    cfg = validate_config(cfg)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_suffix(".tmp")
    temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CONFIG)
    return cfg


@contextlib.contextmanager
def database(path=DATABASE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, slot_id TEXT NOT NULL,
            label TEXT NOT NULL, kind TEXT NOT NULL, window_start REAL NOT NULL,
            window_end REAL NOT NULL, planned REAL NOT NULL, prepare_at REAL NOT NULL,
            signature TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            mode TEXT NOT NULL DEFAULT 'live', phase TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            started REAL, finished REAL, clicked REAL, pid INTEGER,
            run_dir TEXT, config_json TEXT, UNIQUE(day, slot_id))""")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def windows(day, slot, minutes):
    hour, minute = map(int, slot["time"].split(":"))
    base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    offset = timedelta(minutes=minutes)
    return (base - offset, base) if slot["kind"] == "in" else (base, base + offset)


def plan(db, cfg, current=None, rng=None):
    current = current or now()
    rng = rng or random.SystemRandom()
    for offset in range(8):
        day = current.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for slot in cfg["slots"]:
            start, end = windows(day, slot, cfg["random_minutes"])
            signature = json.dumps([slot["kind"], slot["time"], cfg["random_minutes"]])
            row = db.execute("SELECT * FROM jobs WHERE day=? AND slot_id=?", (day.isoformat(), slot["id"])).fetchone()
            if row and row["status"] != "pending":
                continue  # Never recreate an already attempted slot after edits/restart.
            if row and row["signature"] == signature:
                if row["prepare_at"] != row["planned"]-cfg["prepare_seconds"] or row["label"] != slot["label"]:
                    db.execute("UPDATE jobs SET prepare_at=planned-?,label=? WHERE id=?",
                               (cfg["prepare_seconds"], slot["label"], row["id"]))
                continue
            target = start.timestamp() + rng.randint(0, int((end-start).total_seconds()) - 5)
            values = (slot["label"], slot["kind"], start.timestamp(), end.timestamp(), target,
                      target - cfg["prepare_seconds"], signature)
            if row:
                db.execute("""UPDATE jobs SET label=?,kind=?,window_start=?,window_end=?,planned=?,
                           prepare_at=?,signature=? WHERE id=?""", (*values, row["id"]))
            else:
                db.execute("""INSERT INTO jobs(day,slot_id,label,kind,window_start,window_end,
                           planned,prepare_at,signature,created) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (day.isoformat(), slot["id"], *values, current.timestamp()))
    db.execute("UPDATE jobs SET status='missed',message='已过时间窗口，未补签',finished=? "
               "WHERE status='pending' AND window_end < ?", (current.timestamp(), current.timestamp()))


def due_job(db, cfg, current=None):
    current = current or now()
    if not cfg["enabled"] or current.weekday() >= 5:
        return None
    enabled = {s["id"] for s in cfg["slots"] if s["enabled"]}
    rows = db.execute("SELECT * FROM jobs WHERE status='pending' AND prepare_at<=? "
                      "AND window_end>? AND day=? ORDER BY planned", (current.timestamp(), current.timestamp(), current.date().isoformat()))
    return next((dict(r) for r in rows if r["slot_id"] in enabled), None)


def update_job(job_id, **fields):
    allowed = {"status", "phase", "message", "started", "finished", "clicked", "pid", "run_dir", "config_json"}
    if not fields or not set(fields) <= allowed:
        raise ValueError("Invalid job update")
    with database() as db:
        db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                   (*fields.values(), job_id))
