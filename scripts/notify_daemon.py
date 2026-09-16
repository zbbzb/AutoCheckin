"""Group-robot notifier for finished check-in jobs (Feishu app bot / WeCom webhook).

Completely additive companion to the attendance software: it only READS the
existing scheduler database (WAL mode, safe for a concurrent reader), detects
jobs that reached a terminal state, and pushes a text summary plus the evidence
screenshot to a group chat.

Two interchangeable channels:

- feishu (primary): a self-built app bot. Pictures need an image_key, so the
  daemon keeps a tenant_access_token cache (2h), uploads the JPEG via
  /im/v1/images and sends text/image messages via /im/v1/messages. The target
  chat is auto-discovered from the bot's own chat list unless chat_id is set.
- wecom (fallback): a group-robot webhook; text is markdown and pictures are
  sent natively as base64+md5.

Nothing in the existing service, worker or database schema is modified. The
notifier keeps its own config, dedup state and pid lock, so a crash here can
never affect a check-in run.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mumu_common import (ROOT, TERMINAL, TZ, clear_pid, database,  # noqa: E402
                         process_running, read_pid, write_pid)

NOTIFIER_CONFIG = ROOT / "config/notify.json"
NOTIFIER_STATE = ROOT / "data/notify_state.json"
NOTIFIER_LOCK = ROOT / "data/notifier.pid"
NOTIFIER_LOG = ROOT / "logs/notifier.log"
DB_PATH = ROOT / "data/checkin.sqlite3"
ENV_PATH = ROOT / ".env"

MAX_ATTEMPTS = 10          # give up after this many send failures per event
MESSAGE_BYTES = 4000       # WeCom markdown limit 4096 bytes; Feishu text 150k, plenty
SUCCESS_WORDS = ("打卡成功", "签到成功", "签退成功")
IMAGE_STATUSES = {"success", "failed", "uncertain"}
CHANNELS = ("feishu", "wecom")
WEBHOOK_PATTERN = re.compile(r"^https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[0-9A-Za-z-]+$")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
TOKEN_EXPIRY_MARGIN = 300  # refresh the tenant token 5 minutes early
TOKEN_RETRY_CODES = {99991661, 99991663, 99991664}  # invalid / expired token

STATUS_LINE = {
    "success": "✅ 签到成功",
    "failed": "❌ 执行失败",
    "uncertain": "⚠️ 结果待核实",
    "missed": "⏭️ 时段已跳过",
    "cancelled": "⏹️ 已取消",
    "preview": "🔁 演练通过",
}


def log(message):
    """Append to logs/notifier.log; never raise (the daemon must keep running)."""
    try:
        NOTIFIER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NOTIFIER_LOG.open("a", encoding="utf-8") as stream:
            stamp = datetime.now(TZ).isoformat(timespec="seconds")
            stream.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def load_env(path=None):
    """Minimal KEY=VALUE reader for the project .env file (no third-party dep).

    Returns only the keys found in the file; callers merge real environment
    variables on top so `set NOTIFY_FEISHU_APP_ID=...` always wins.
    """
    values = {}
    try:
        text = (Path(path) if path else ENV_PATH).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def default_config():
    return {
        "channel": "feishu",
        "feishu": {"app_id": "", "app_secret": "", "chat_id": "", "phone": "",
                   "email": "", "receive_mode": "p2p"},
        "wecom": {"webhook": ""},
        "enabled": True,                     # master switch (tray menu toggles this)
        "poll_seconds": 8,
        "statuses": ["success", "failed", "uncertain", "missed", "cancelled"],
        "send_image": True,                 # attach the evidence screenshot when one exists
        "jpeg_quality": 80,
        "image_max_bytes": 2000000,         # WeCom limit 2MB; Feishu allows more, one cap is fine
        "max_age_hours": 24,                # skip events older than this (stale bursts)
    }


def load_config(apply_env=True):
    cfg = default_config()
    try:
        saved = json.loads(NOTIFIER_CONFIG.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            # Accept both the new nested shape and the old flat webhook.
            if isinstance(saved.get("feishu"), dict):
                cfg["feishu"].update(saved["feishu"])
            if isinstance(saved.get("wecom"), dict):
                cfg["wecom"].update(saved["wecom"])
            elif saved.get("webhook"):
                cfg["wecom"]["webhook"] = saved["webhook"]
            saved.pop("webhook", None)
            saved.pop("feishu", None)
            saved.pop("wecom", None)
            cfg.update(saved)
    except (OSError, ValueError):
        pass
    if cfg.get("channel") not in CHANNELS:
        cfg["channel"] = "feishu"
    if type(cfg.get("enabled")) is not bool:
        cfg["enabled"] = True
    feishu = cfg["feishu"]
    if feishu.get("receive_mode") not in ("p2p", "chat"):
        feishu["receive_mode"] = "p2p"
    for key in ("app_id", "app_secret", "chat_id", "phone", "email"):
        if not isinstance(feishu.get(key), str):
            feishu[key] = ""
    if not isinstance(cfg.get("poll_seconds"), int) or not 3 <= cfg["poll_seconds"] <= 60:
        cfg["poll_seconds"] = 8
    if not isinstance(cfg.get("statuses"), list):
        cfg["statuses"] = default_config()["statuses"]
    cfg["statuses"] = [s for s in cfg["statuses"] if s in TERMINAL]
    if not apply_env:
        return cfg
    # Credentials from the environment / .env file override config/notify.json,
    # so secrets can live outside the JSON config entirely.
    env = {**load_env(), **os.environ}
    if env.get("NOTIFY_CHANNEL") in CHANNELS:
        cfg["channel"] = env["NOTIFY_CHANNEL"]
    if env.get("NOTIFY_WECOM_WEBHOOK"):
        cfg["wecom"]["webhook"] = env["NOTIFY_WECOM_WEBHOOK"]
    for env_key, cfg_key in (("NOTIFY_FEISHU_APP_ID", "app_id"),
                             ("NOTIFY_FEISHU_APP_SECRET", "app_secret"),
                             ("NOTIFY_FEISHU_PHONE", "phone"),
                             ("NOTIFY_FEISHU_EMAIL", "email"),
                             ("NOTIFY_FEISHU_CHAT_ID", "chat_id")):
        if env.get(env_key):
            cfg["feishu"][cfg_key] = env[env_key]
    if env.get("NOTIFY_FEISHU_PHONE") or env.get("NOTIFY_FEISHU_EMAIL"):
        cfg["feishu"].setdefault("receive_mode", "p2p")
        if cfg["feishu"]["receive_mode"] not in ("p2p", "chat"):
            cfg["feishu"]["receive_mode"] = "p2p"
    if env.get("NOTIFY_FEISHU_CHAT_ID") and not env.get("NOTIFY_FEISHU_PHONE"):
        cfg["feishu"]["receive_mode"] = "chat"
    return cfg


def save_config(cfg):
    NOTIFIER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = NOTIFIER_CONFIG.with_suffix(".tmp")
    temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, NOTIFIER_CONFIG)


def set_enabled(value):
    """Flip the master switch WITHOUT ever persisting env-merged credentials.

    load_config() overlays .env values on top of the JSON; saving that merged
    result would copy the secrets from .env into config/notify.json. The switch
    therefore reloads the file with apply_env=False before writing.
    """
    cfg = load_config(apply_env=False)
    cfg["enabled"] = bool(value)
    save_config(cfg)
    return cfg["enabled"]


def channel_ready(cfg):
    if cfg["channel"] == "wecom":
        return bool(cfg["wecom"].get("webhook"))
    feishu = cfg["feishu"]
    return bool(feishu.get("app_id") and feishu.get("app_secret"))


def fmt_ts(value):
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(float(value), TZ).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


def compose_message(job, style="text"):
    """Plain text for Feishu, WeCom markdown when style='markdown'."""
    title = STATUS_LINE.get(job.get("status"), job.get("status") or "?")
    message = (job.get("message") or "").strip()
    if len(message) > 300:
        message = message[:300] + "…"
    label = job.get("label") or job.get("slot_id") or "?"
    day = job.get("day") or ""
    if style == "markdown":
        color = {"success": "info", "failed": "warning", "uncertain": "warning"}.get(job.get("status"), "comment")
        lines = [f'**<font color="{color}">{title}</font>**']
    else:
        lines = [title]
    lines += [f"时段：{label}（{day}）",
              f"计划：{fmt_ts(job.get('planned'))} · 完成：{fmt_ts(job.get('finished'))}"]
    if message:
        lines.append(message)
    content = "\n".join(lines)
    while len(content.encode("utf-8")) > MESSAGE_BYTES and len(content) > 10:
        content = content[: max(10, len(content) - 120)] + "…"
    return content


def pick_screenshot(job):
    """Choose the evidence frame for a finished job, safely.

    Success/uncertain: the first screenshot whose OCR sidecar contains the
    success banner (that is exactly the frame the worker used as evidence).
    Otherwise the worker's error frame, else the newest screenshot. Never
    returns anything outside logs/.
    """
    run_dir = job.get("run_dir")
    if not run_dir:
        return None
    try:
        folder = Path(run_dir).resolve()
    except OSError:
        return None
    if not folder.is_relative_to((ROOT / "logs").resolve()):
        return None
    pngs = sorted(folder.glob("*.png"))
    if not pngs:
        return None
    if job.get("status") in ("success", "uncertain"):
        for sidecar in sorted(folder.glob("*.json")):
            try:
                items = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(items, list):
                continue  # result.json / instance.json / location.json are dicts
            if any(word in (item.get("text") or "") for item in items
                   if isinstance(item, dict) for word in SUCCESS_WORDS):
                candidate = sidecar.with_suffix(".png")
                if candidate.exists():
                    return candidate
    errors = sorted(folder.glob("*-error.png"))
    if errors:
        return errors[-1]
    return pngs[-1]


def load_image_bytes(path, quality=80, max_bytes=2000000):
    """PNG -> size-capped JPEG bytes using the OpenCV already shipped with the project."""
    try:
        import cv2
        data = cv2.imread(str(path))
        if data is None:
            return None
        for _ in range(5):
            ok, encoded = cv2.imencode(".jpg", data, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            if not ok:
                return None
            raw = encoded.tobytes()
            if len(raw) <= max_bytes:
                return raw
            data = cv2.resize(data, (max(1, int(data.shape[1] * .75)),
                                     max(1, int(data.shape[0] * .75))), interpolation=cv2.INTER_AREA)
            quality = max(40, int(quality * .8))
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- HTTP helpers
def http_request(url, data=None, headers=None, timeout=12):
    """POST (or GET when data is None) and return (status, body_bytes)."""
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc).encode("utf-8", "replace")


# ---------------------------------------------------------------- Feishu sink
FEISHU_TOKEN = {"token": "", "expires": 0.0}


def feishu_call(cfg, method, path, payload=None, multipart=None, token=None, retry=True):
    """One signed call to the Feishu open API. Returns (ok, data_or_error).

    'payload' is JSON; 'multipart' is (body_bytes, content_type). The tenant
    token is cached and refreshed once on the token-expiry error codes.
    """
    feishu = cfg["feishu"]
    if token is None:
        token = feishu_token(cfg)
        if not token:
            return False, "无法获取 tenant_access_token（检查 app_id/app_secret）"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif multipart is not None:
        data, ctype = multipart
        headers["Content-Type"] = ctype
    if method == "GET" and data is None:
        status, body = http_request(f"{FEISHU_BASE}{path}", headers=headers, timeout=15)
    else:
        status, body = http_request(f"{FEISHU_BASE}{path}", data=data, headers=headers, timeout=20)
    if status is None:
        return False, f"网络错误: {body.decode('utf-8', 'replace')}"
    try:
        result = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False, f"HTTP {status} 响应异常"
    code = result.get("code", 0)
    if code == 0:
        return True, result.get("data") or {}
    if code in TOKEN_RETRY_CODES and retry:
        FEISHU_TOKEN["token"] = ""
        return feishu_call(cfg, method, path, payload=payload, multipart=multipart, retry=False)
    return False, f"code={code} {result.get('msg')}"


def feishu_token(cfg):
    feishu = cfg["feishu"]
    if FEISHU_TOKEN.get("token") and time.monotonic() < FEISHU_TOKEN["expires"]:
        return FEISHU_TOKEN["token"]
    status, body = http_request(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": feishu.get("app_id"), "app_secret": feishu.get("app_secret")}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, timeout=15)
    if status != 200:
        return ""
    try:
        result = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if result.get("code") != 0 or not result.get("tenant_access_token"):
        return ""
    FEISHU_TOKEN["token"] = result["tenant_access_token"]
    FEISHU_TOKEN["expires"] = time.monotonic() + max(60, int(result.get("expire", 7200))) - TOKEN_EXPIRY_MARGIN
    return FEISHU_TOKEN["token"]


def multipart_body(fields, files):
    boundary = "----autocheckin" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      f"{value}\r\n").encode("utf-8"))
    for name, (filename, data, ctype) in files.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8"))
        parts.append(data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def feishu_chat_id(cfg, state):
    """Configured chat_id, cached one, or the bot's only group; error when ambiguous."""
    if cfg["feishu"].get("chat_id"):
        return cfg["feishu"]["chat_id"], None
    cached = state.get("feishu_chat_id")
    if cached:
        return cached, None
    ok, data = feishu_call(cfg, "GET", "/im/v1/chats?page_size=20")
    if not ok:
        return None, f"获取机器人所在群列表失败: {data}"
    items = data.get("items") or []
    if not items:
        return None, "机器人还不在任何群里：请把应用机器人添加到目标群"
    if len(items) > 1:
        names = "、".join(i.get("name", "?") for i in items[:5])
        return None, f"机器人在多个群里（{names}），请在配置里显式填 feishu.chat_id"
    state["feishu_chat_id"] = items[0]["chat_id"]
    log(f"feishu chat auto-discovered: {items[0].get('name')} ({items[0]['chat_id']})")
    return items[0]["chat_id"], None


def feishu_open_id(cfg, state):
    """Resolve the configured phone/email to an open_id once, then cache it.

    A home PC has no public callback URL for bot events, so the lookup goes
    through /contact/v3/users/batch_get_id (permission: 通过手机号或邮箱获取用户 ID).
    """
    if state.get("feishu_open_id"):
        return state["feishu_open_id"], None
    feishu = cfg["feishu"]
    if feishu.get("phone"):
        mobiles = [feishu["phone"]]
        if re.fullmatch(r"1\d{10}", feishu["phone"]):
            mobiles.append("+86" + feishu["phone"])
        payload, key = {"mobiles": mobiles}, "mobile"
    elif feishu.get("email"):
        payload, key = {"emails": [feishu["email"]]}, "email"
    else:
        return None, "未配置接收人：请用 --set-phone 或 --set-email 设置你的飞书手机号/邮箱"
    ok, data = feishu_call(cfg, "POST", "/contact/v3/users/batch_get_id?user_id_type=open_id", payload=payload)
    if not ok:
        return None, f"获取用户 ID 失败: {data}（检查是否开通「通过手机号或邮箱获取用户 ID」权限）"
    for entry in (data.get("user_list") or data.get("users_list") or []):
        open_id = entry.get("user_id") or entry.get("open_id")
        if open_id:
            state["feishu_open_id"] = open_id
            log(f"feishu receiver resolved via {entry.get(key) or 'lookup'} -> {open_id}")
            return open_id, None
    return None, "手机号/邮箱没有匹配到飞书用户（确认是登录飞书的号码/邮箱，且你在应用可用范围内）"


def feishu_receiver(cfg, state):
    """(receive_id, receive_id_type, error) for the configured delivery mode."""
    feishu = cfg["feishu"]
    if feishu.get("receive_mode") == "chat" or feishu.get("chat_id"):
        chat_id, error = feishu_chat_id(cfg, state)
        if error:
            return None, None, error
        return chat_id, "chat_id", None
    open_id, error = feishu_open_id(cfg, state)
    if error:
        return None, None, error
    return open_id, "open_id", None


def feishu_send_message(cfg, receive_id, receive_id_type, msg_type, content):
    return feishu_call(cfg, "POST", f"/im/v1/messages?receive_id_type={receive_id_type}",
                       payload={"receive_id": receive_id, "msg_type": msg_type, "content": content})


# ---------------------------------------------------------------- WeCom sink
def wecom_send(cfg, payload, timeout=12):
    webhook = cfg["wecom"]["webhook"]
    request = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"网络错误: {exc}"
    if body.get("errcode") != 0:
        return False, f"errcode={body.get('errcode')} {body.get('errmsg')}"
    return True, ""


# ---------------------------------------------------------------- dispatch
def send_event(cfg, job, state=None, shot_override=None):
    """Send one event: summary text first, evidence image second.

    shot_override forces that exact image (used by --test --image); otherwise
    the evidence frame is picked from the run directory.

    Returns (delivered, warning). delivered=True means the summary reached the
    chat (an image failure alone is only a warning - the summary already told
    the user what happened, and the screenshot stays in the run directory).
    """
    if cfg["channel"] == "wecom":
        ok, error = wecom_send(cfg, {"msgtype": "markdown",
                                     "markdown": {"content": compose_message(job, "markdown")}})
        if not ok:
            return False, error
        warning = ""
        if cfg.get("send_image") and job.get("status") in IMAGE_STATUSES:
            shot = shot_override or pick_screenshot(job)
            if shot:
                raw = load_image_bytes(shot, cfg.get("jpeg_quality", 80), cfg.get("image_max_bytes", 2000000))
                if raw:
                    time.sleep(1)  # keep the summary above the image
                    ok2, error2 = wecom_send(cfg, {"msgtype": "image", "image": {
                        "base64": base64.b64encode(raw).decode("ascii"),
                        "md5": hashlib.md5(raw).hexdigest()}})
                    if not ok2:
                        warning = f"图片发送失败({shot.name}): {error2}"
                else:
                    warning = f"截图压缩失败: {shot.name}"
        return True, warning

    # feishu channel
    state = state if state is not None else {}
    receive_id, id_type, error = feishu_receiver(cfg, state)
    if not receive_id:
        return False, error
    ok, error = feishu_send_message(cfg, receive_id, id_type, "text",
                                    json.dumps({"text": compose_message(job, "text")}, ensure_ascii=False))
    if not ok:
        return False, f"发送摘要失败: {error}"
    warning = ""
    if cfg.get("send_image") and job.get("status") in IMAGE_STATUSES:
        shot = shot_override or pick_screenshot(job)
        if shot:
            raw = load_image_bytes(shot, cfg.get("jpeg_quality", 80), cfg.get("image_max_bytes", 2000000))
            if raw:
                ok2, data = feishu_call(cfg, "POST", "/im/v1/images",
                                        multipart=multipart_body(
                                            {"image_type": "message"},
                                            {"image": ("shot.jpg", raw, "image/jpeg")}))
                if not ok2:
                    warning = f"截图上传失败({shot.name}): {data}"
                else:
                    ok3, error3 = feishu_send_message(
                        cfg, receive_id, id_type, "image",
                        json.dumps({"image_key": data.get("image_key", "")}))
                    if not ok3:
                        warning = f"截图发送失败: {error3}"
            else:
                warning = f"截图压缩失败: {shot.name}"
    return True, warning


# ---------------------------------------------------------------- state & loop
def load_state():
    try:
        state = json.loads(NOTIFIER_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("events", [])    # [{"id":..,"status":..,"at":epoch}]
    state.setdefault("failures", {})  # "id|status" -> {"attempts":..,"error":..,"at":..}
    state.setdefault("bootstrapped", False)
    return state


def save_state(state):
    NOTIFIER_STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = NOTIFIER_STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, NOTIFIER_STATE)


def terminal_jobs(db_path):
    with database(path=db_path) as db:
        marks = ",".join("?" * len(TERMINAL))
        return [dict(row) for row in db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks})", tuple(TERMINAL))]


def bootstrap(state, db_path):
    """First run: silently mark every existing terminal job as handled."""
    for job in terminal_jobs(db_path):
        state["events"].append({"id": job["id"], "status": job["status"], "at": time.time()})
    state["bootstrapped"] = True
    save_state(state)
    log(f"baseline: {len(state['events'])} existing terminal jobs will not be notified")


def prune(state):
    horizon = time.time() - 30 * 86400
    state["events"] = [e for e in state["events"] if e.get("at", 0) > horizon][-1000:]


def poll_once(cfg, state, db_path=None):
    """Deliver every not-yet-handled terminal event. Returns a summary dict."""
    if not cfg.get("enabled", True):
        return {"disabled": 1}  # master switch off: send nothing, touch nothing
    summary = {"sent": 0, "skipped": 0, "stale": 0, "failed": 0, "given_up": 0}
    if not state["bootstrapped"]:
        bootstrap(state, db_path or DB_PATH)
    wanted = set(cfg["statuses"])
    done = {(e["id"], e["status"]) for e in state["events"]}
    max_age = max(1, int(cfg.get("max_age_hours", 24))) * 3600
    for job in terminal_jobs(db_path or DB_PATH):
        pair = (job["id"], job["status"])
        if pair in done:
            continue
        stamp = job.get("finished") or job.get("planned") or time.time()
        if stamp < time.time() - max_age:
            state["events"].append({"id": job["id"], "status": job["status"], "at": time.time()})
            summary["stale"] += 1
            continue
        if job["status"] not in wanted:
            state["events"].append({"id": job["id"], "status": job["status"], "at": time.time()})
            summary["skipped"] += 1
            continue
        key = f"{job['id']}|{job['status']}"
        attempts = state["failures"].get(key, {}).get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            state["failures"].pop(key, None)
            state["events"].append({"id": job["id"], "status": job["status"], "at": time.time()})
            summary["given_up"] += 1
            log(f"gave up on job {job['id']} ({job['status']}) after {attempts} attempts")
            continue
        delivered, warning = send_event(cfg, job, state)
        if delivered:
            state["events"].append({"id": job["id"], "status": job["status"], "at": time.time()})
            summary["sent"] += 1
            log(f"notified job {job['id']} ({job['status']})" + (f"; {warning}" if warning else ""))
        else:
            state["failures"][key] = {"attempts": attempts + 1, "error": warning, "at": time.time()}
            summary["failed"] += 1
            log(f"notify failed for job {job['id']} attempt {attempts + 1}: {warning}")
    prune(state)
    save_state(state)
    return summary


def run_daemon(cfg):
    other = read_pid(NOTIFIER_LOCK)
    if other and process_running(other):
        log(f"another notifier is running (pid {other}); exiting")
        return
    write_pid(NOTIFIER_LOCK, os.getpid())
    log(f"notifier started pid={os.getpid()} channel={cfg['channel']}")
    warned_config = False
    last_enabled = None
    try:
        while True:
            try:
                # Reload every cycle so the tray switch (and any config edit)
                # takes effect within one poll interval, without a restart.
                cfg = load_config()
                if not channel_ready(cfg):
                    if not warned_config:
                        log(f"{cfg['channel']} channel not configured; waiting (config/notify.json)")
                        warned_config = True
                    time.sleep(30)
                    continue
                warned_config = False
                enabled = bool(cfg.get("enabled", True))
                if enabled != last_enabled:
                    log(f"notify switch -> {'on' if enabled else 'off'}")
                    last_enabled = enabled
                if enabled:
                    poll_once(cfg, load_state())
            except Exception as exc:
                log(f"poll error: {exc!r}")
                time.sleep(min(60, cfg.get("poll_seconds", 8)))
            time.sleep(max(3, cfg.get("poll_seconds", 8)))
    finally:
        clear_pid(NOTIFIER_LOCK, expected=os.getpid())
        log("notifier stopped")


def main():
    parser = argparse.ArgumentParser(description="Group-robot notifier for AutoCheckin results")
    parser.add_argument("--set-feishu", nargs=2, metavar=("APP_ID", "APP_SECRET"),
                        help="save Feishu self-built app credentials and switch channel to feishu")
    parser.add_argument("--set-phone", metavar="PHONE",
                        help="your Feishu login phone; the bot will DM you directly (no group needed)")
    parser.add_argument("--set-email", metavar="EMAIL",
                        help="your Feishu login email; alternative receiver for direct messages")
    parser.add_argument("--set-chat", metavar="CHAT_ID",
                        help="switch to group mode and set the target Feishu chat_id explicitly")
    parser.add_argument("--set-webhook", metavar="URL",
                        help="save a WeCom group-robot webhook and switch channel to wecom")
    parser.add_argument("--set-enabled", metavar="ON|OFF", choices=("on", "off"),
                        help="flip the notification master switch (the tray menu uses this)")
    parser.add_argument("--test", action="store_true", help="send a test message (optionally with --image)")
    parser.add_argument("--image", metavar="PNG", help="image path used by --test")
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    args = parser.parse_args()

    if args.set_enabled:
        value = set_enabled(args.set_enabled == "on")
        print(f"OK: notifications {'enabled' if value else 'disabled'}")
        return 0

    if args.set_feishu:
        app_id, app_secret = (value.strip() for value in args.set_feishu)
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,}", app_id) or not app_secret:
            print("REFUSED: APP_ID/APP_SECRET look wrong")
            return 2
        # apply_env=False: never write .env credentials back into the JSON file.
        cfg = load_config(apply_env=False)
        cfg["channel"] = "feishu"
        cfg["feishu"].update({"app_id": app_id, "app_secret": app_secret})
        save_config(cfg)
        print(f"OK: feishu credentials saved to {NOTIFIER_CONFIG}")
        return 0

    if args.set_phone:
        value = args.set_phone.strip()
        if not re.fullmatch(r"\+?\d{5,16}", value):
            print("REFUSED: phone must be 5-16 digits, optionally starting with +")
            return 2
        cfg = load_config(apply_env=False)
        cfg["feishu"].update({"phone": value, "email": "", "receive_mode": "p2p"})
        save_config(cfg)
        state = load_state()
        state.pop("feishu_open_id", None)  # receiver changed; re-resolve on next send
        save_state(state)
        print(f"OK: receiver phone saved; the bot will DM you directly ({NOTIFIER_CONFIG})")
        return 0

    if args.set_email:
        value = args.set_email.strip()
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            print("REFUSED: that does not look like an email address")
            return 2
        cfg = load_config(apply_env=False)
        cfg["feishu"].update({"email": value, "phone": "", "receive_mode": "p2p"})
        save_config(cfg)
        state = load_state()
        state.pop("feishu_open_id", None)  # receiver changed; re-resolve on next send
        save_state(state)
        print(f"OK: receiver email saved; the bot will DM you directly ({NOTIFIER_CONFIG})")
        return 0

    if args.set_chat:
        cfg = load_config(apply_env=False)
        if not args.set_chat.startswith("oc_"):
            print("REFUSED: Feishu chat_id starts with oc_")
            return 2
        cfg["feishu"].update({"chat_id": args.set_chat, "receive_mode": "chat"})
        save_config(cfg)
        print(f"OK: group mode, chat_id saved to {NOTIFIER_CONFIG}")
        return 0

    if args.set_webhook:
        url = args.set_webhook.strip()
        if not WEBHOOK_PATTERN.match(url):
            print("REFUSED: URL must look like https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")
            return 2
        cfg = load_config(apply_env=False)
        cfg["channel"] = "wecom"
        cfg["wecom"]["webhook"] = url
        save_config(cfg)
        print(f"OK: wecom webhook saved, channel switched to wecom ({NOTIFIER_CONFIG})")
        return 0

    cfg = load_config()
    if args.test:
        if not channel_ready(cfg):
            print(f"REFUSED: configure the {cfg['channel']} channel first")
            return 2
        job = {"id": 0, "day": datetime.now(TZ).date().isoformat(), "slot_id": "test",
               "label": "通知器测试", "status": "success", "mode": "live",
               "message": "配置成功，这条消息来自 PC 通知器。", "planned": time.time(),
               "finished": time.time(), "run_dir": None}
        delivered, warning = send_event(cfg, job, load_state(),
                                        shot_override=Path(args.image) if args.image else None)
        print("TEST:", "OK" if delivered else f"FAILED ({warning})",
              ("| warning: " + warning) if (delivered and warning) else "")
        return 0 if delivered else 1

    if args.once:
        if not channel_ready(cfg):
            print(f"REFUSED: configure the {cfg['channel']} channel first")
            return 2
        print(json.dumps(poll_once(cfg, load_state()), ensure_ascii=False))
        return 0

    run_daemon(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
