"""Group-robot notifier: screenshot picking, message shape, dedup, retry and sinks."""
import base64
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import notify_daemon as notifier  # noqa: E402
from mumu_common import database  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="autocheckin-notify-test-"))


@contextlib.contextmanager
def sandboxed(root=WORK):
    """Point the notifier's path globals at a temp root for one test, then restore."""
    saved = {name: getattr(notifier, name) for name in
             ("ROOT", "NOTIFIER_CONFIG", "NOTIFIER_STATE", "NOTIFIER_LOG", "DB_PATH", "ENV_PATH")}
    logs = root / "logs"
    notifier.ROOT = root  # pick_screenshot confines run_dir to ROOT/logs
    notifier.NOTIFIER_CONFIG = root / "notify.json"
    notifier.NOTIFIER_STATE = root / "notify_state.json"
    notifier.NOTIFIER_LOG = root / "notifier.log"
    notifier.DB_PATH = root / "checkin.sqlite3"
    notifier.ENV_PATH = root / ".env"
    try:
        yield root, logs
    finally:
        for name, value in saved.items():
            setattr(notifier, name, value)


def make_png(path, size=(64, 32)):
    import cv2
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.zeros((*size, 3), dtype=np.uint8))
    return path


def success_run(root_logs, name="mumu-shot"):
    run = root_logs / name
    make_png(run / "06-ready.png")
    make_png(run / "08-result.png")
    (run / "06-ready.json").write_text(json.dumps(
        [{"text": "已在打卡范围内", "score": .9, "center": [1, 1], "box": []}]), encoding="utf-8")
    (run / "08-result.json").write_text(json.dumps(
        [{"text": "打卡成功", "score": .9, "center": [1, 1], "box": []}]), encoding="utf-8")
    return run


def insert_job(db_path, job_id, status, *, mode="live", message="ok", run_dir=None,
               finished=None, label="下午下班"):
    with database(path=db_path) as db:
        db.execute("""INSERT INTO jobs(id,day,slot_id,label,kind,window_start,window_end,planned,
                     prepare_at,signature,created,status,mode,message,finished,run_dir)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (job_id, "2026-09-18", f"slot-{job_id}", label, "out", 1.0, 2.0, time.time(),
                    0.0, "sig", time.time(), status, mode, message, finished or time.time(), run_dir))


class EnvTests(unittest.TestCase):
    def test_env_file_overrides_config(self):
        with sandboxed() as (root, _):
            notifier.NOTIFIER_CONFIG.write_text(json.dumps(
                {"feishu": {"app_id": "cli_from_json", "app_secret": "json_sec", "phone": ""}}),
                encoding="utf-8")
            notifier.ENV_PATH.write_text(
                "# comment line\n"
                "NOTIFY_FEISHU_APP_ID=cli_from_env\n"
                'NOTIFY_FEISHU_APP_SECRET="quoted_secret"\n'
                "NOTIFY_FEISHU_PHONE=13800138000\n"      # fake number, never a real one
                "BROKEN_LINE_WITHOUT_EQUALS\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NOTIFY_FEISHU_APP_ID", None)
                cfg = notifier.load_config()
            self.assertEqual(cfg["feishu"]["app_id"], "cli_from_env")       # .env wins over json
            self.assertEqual(cfg["feishu"]["app_secret"], "quoted_secret")  # quotes stripped
            self.assertEqual(cfg["feishu"]["phone"], "13800138000")
            self.assertEqual(cfg["feishu"]["receive_mode"], "p2p")
            self.assertTrue(notifier.channel_ready(cfg))

    def test_real_environment_beats_env_file(self):
        with sandboxed() as (root, _):
            notifier.ENV_PATH.write_text("NOTIFY_FEISHU_APP_ID=cli_from_envfile\n", encoding="utf-8")
            with patch.dict(os.environ, {"NOTIFY_FEISHU_APP_ID": "cli_from_process_env"}):
                cfg = notifier.load_config()
            self.assertEqual(cfg["feishu"]["app_id"], "cli_from_process_env")

    def test_missing_env_file_is_harmless(self):
        fresh = Path(tempfile.mkdtemp(prefix="autocheckin-notify-noenv-"))
        with sandboxed(fresh):
            cfg = notifier.load_config()
            self.assertEqual(cfg["feishu"]["app_id"], "")
            self.assertFalse(notifier.channel_ready(cfg))


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with sandboxed() as (root, _):
            cfg = notifier.load_config()
            self.assertEqual(cfg["channel"], "feishu")
            self.assertEqual(cfg["feishu"]["app_id"], "")
            cfg["feishu"].update({"app_id": "cli_test", "app_secret": "secret"})
            notifier.save_config(cfg)
            self.assertEqual(notifier.load_config()["feishu"]["app_id"], "cli_test")
            bad = dict(notifier.default_config(), poll_seconds="x")
            notifier.save_config(bad)
            self.assertEqual(notifier.load_config()["poll_seconds"], 8)

    def test_legacy_flat_webhook_migrates(self):
        with sandboxed() as (root, _):
            notifier.NOTIFIER_CONFIG.write_text(json.dumps(
                {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"}), encoding="utf-8")
            cfg = notifier.load_config()
            self.assertEqual(cfg["wecom"]["webhook"], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
            self.assertEqual(cfg["channel"], "feishu")  # unchanged until --set-webhook


class MessageTests(unittest.TestCase):
    def test_text_contains_essentials(self):
        job = {"status": "success", "label": "下午下班", "slot_id": "afternoon-out",
               "day": "2026-09-18", "planned": 1780000000, "finished": 1780000100,
               "message": "签到成功，成功页已关闭"}
        text = notifier.compose_message(job, "text")
        self.assertIn("✅ 签到成功", text)
        self.assertIn("下午下班", text)
        self.assertNotIn("<font", text)

    def test_markdown_and_size_cap(self):
        job = {"status": "failed", "label": "x", "message": "爆" * 5000}
        text = notifier.compose_message(job, "markdown")
        self.assertIn("<font", text)
        self.assertLessEqual(len(text.encode("utf-8")), notifier.MESSAGE_BYTES)
        plain = notifier.compose_message(job, "text")
        self.assertLessEqual(len(plain.encode("utf-8")), notifier.MESSAGE_BYTES)


class EnabledSwitchTests(unittest.TestCase):
    """The tray master switch: default on, no secret leakage into the JSON."""

    def test_default_on_and_coercion(self):
        with sandboxed() as (root, _):
            cfg = notifier.load_config()
            self.assertIs(cfg["enabled"], True)
            notifier.NOTIFIER_CONFIG.write_text('{"enabled": "yes"}', encoding="utf-8")
            self.assertIs(notifier.load_config()["enabled"], True)

    def test_set_enabled_never_persists_env_credentials(self):
        fresh = Path(tempfile.mkdtemp(prefix="autocheckin-notify-switch-"))
        with sandboxed(fresh):
            notifier.ENV_PATH.write_text(
                "NOTIFY_FEISHU_APP_ID=cli_secret\nNOTIFY_FEISHU_APP_SECRET=hush\n", encoding="utf-8")
            notifier.set_enabled(False)
            raw = json.loads(notifier.NOTIFIER_CONFIG.read_text(encoding="utf-8"))
            self.assertIs(raw["enabled"], False)
            self.assertEqual(raw["feishu"]["app_id"], "")      # .env values stay in .env
            self.assertEqual(raw["feishu"]["app_secret"], "")
            merged = notifier.load_config()                     # runtime view still merged
            self.assertEqual(merged["feishu"]["app_id"], "cli_secret")
            self.assertFalse(merged["enabled"])
            notifier.set_enabled(True)
            self.assertIs(json.loads(notifier.NOTIFIER_CONFIG.read_text(encoding="utf-8"))["enabled"], True)

    def test_poll_once_short_circuits_when_disabled(self):
        fresh = Path(tempfile.mkdtemp(prefix="autocheckin-notify-off-"))
        with sandboxed(fresh):
            insert_job(fresh / "checkin.sqlite3", 601, "success")
            state = notifier.load_state()
            cfg = dict(notifier.default_config(), enabled=False)
            with patch.object(notifier, "send_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent"))):
                summary = notifier.poll_once(cfg, state, db_path=notifier.DB_PATH)
            self.assertEqual(summary, {"disabled": 1})
            self.assertFalse(state["bootstrapped"])             # untouched while off


class ScreenshotTests(unittest.TestCase):
    def test_success_frame_from_ocr_sidecar(self):
        with sandboxed() as (_, logs):
            run = success_run(logs)
            job = {"status": "success", "run_dir": str(run)}
            self.assertEqual(notifier.pick_screenshot(job).name, "08-result.png")

    def test_failed_prefers_error_then_newest(self):
        with sandboxed() as (_, logs):
            run = logs / "mumu-test-2"
            make_png(run / "03-before.png")
            make_png(run / "05-error.png")
            self.assertEqual(notifier.pick_screenshot(
                {"status": "failed", "run_dir": str(run)}).name, "05-error.png")
            bare = logs / "mumu-test-3"
            make_png(bare / "01-a.png")
            make_png(bare / "02-b.png")
            self.assertEqual(notifier.pick_screenshot(
                {"status": "failed", "run_dir": str(bare)}).name, "02-b.png")

    def test_escape_and_missing(self):
        outside = WORK / "outside"
        make_png(outside / "x.png")
        self.assertIsNone(notifier.pick_screenshot({"status": "success", "run_dir": str(outside)}))
        self.assertIsNone(notifier.pick_screenshot({"status": "success", "run_dir": None}))
        self.assertIsNone(notifier.pick_screenshot({"status": "success", "run_dir": "Z:\\nope"}))


class PollTests(unittest.TestCase):
    """Channel-agnostic pipeline: dedup, filtering, retry and give-up."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="autocheckin-notify-poll-"))
        patcher = sandboxed(self.dir)
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.sent = []
        self.cfg = dict(notifier.default_config(),
                        feishu={"app_id": "cli_x", "app_secret": "s", "chat_id": "oc_x"})

    def fake_send(self, cfg, job, state=None, shot_override=None):
        self.sent.append(job)
        return True, ""

    def test_bootstrap_dedup_and_skip(self):
        insert_job(self.dir / "checkin.sqlite3", 101, "success")          # pre-existing -> baseline
        state = notifier.load_state()
        with patch.object(notifier, "send_event", self.fake_send):
            notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual(self.sent, [])                                    # nothing on bootstrap
        insert_job(self.dir / "checkin.sqlite3", 102, "success")
        with patch.object(notifier, "send_event", self.fake_send):
            summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual(summary["sent"], 1)
        with patch.object(notifier, "send_event", self.fake_send):
            summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual(summary["sent"], 0)                               # dedup: never resend
        insert_job(self.dir / "checkin.sqlite3", 103, "preview", mode="preview")
        with patch.object(notifier, "send_event", self.fake_send):
            summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual(summary["skipped"], 1)                            # preview filtered by statuses

    def test_retry_then_give_up(self):
        db = self.dir / "checkin.sqlite3"
        insert_job(db, 201, "failed", message="boom")
        state = notifier.load_state()
        state["bootstrapped"] = True  # baseline would otherwise swallow this row
        with patch.object(notifier, "MAX_ATTEMPTS", 2):
            for _ in range(3):
                with patch.object(notifier, "send_event", lambda *a, **k: (False, "net down")):
                    summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["given_up"], 1)
        with patch.object(notifier, "send_event", self.fake_send):
            summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual(summary["sent"], 0)                               # given-up stays done

    def test_stale_events_marked_done(self):
        db = self.dir / "checkin.sqlite3"
        insert_job(db, 301, "missed", finished=time.time() - 5 * 86400)
        state = notifier.load_state()
        state["bootstrapped"] = True
        with patch.object(notifier, "send_event", self.fake_send):
            summary = notifier.poll_once(self.cfg, state, db_path=notifier.DB_PATH)
        self.assertEqual((summary["sent"], summary["stale"]), (0, 1))


class FeishuSinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="autocheckin-notify-feishu-"))
        patcher = sandboxed(self.dir)
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.cfg = dict(notifier.default_config(),
                        feishu={"app_id": "cli_x", "app_secret": "s", "chat_id": "",
                                "phone": "", "email": "", "receive_mode": "chat"})
        self.calls = []

    def route(self, url, data=None, headers=None, timeout=12):
        """Tiny fake Feishu open API: token, chat list, user lookup, upload, send."""
        method = "GET" if data is None else "POST"
        self.calls.append((method, url, data, dict(headers or {})))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "t-1", "expire": 7200}).encode()
        if "/im/v1/chats" in url:
            return 200, json.dumps({"code": 0, "data": {"items": self.chats}}).encode()
        if "/contact/v3/users/batch_get_id" in url:
            return 200, json.dumps({"code": 0, "data": {"user_list": self.users}}).encode()
        if url.endswith("/im/v1/images"):
            return 200, json.dumps({"code": 0, "data": {"image_key": "img_v2_key"}}).encode()
        if "/im/v1/messages" in url:
            return 200, json.dumps({"code": 0, "data": {"message_id": "om_1"}}).encode()
        return 404, b"{}"

    def setUp2(self, chats, users=None):
        self.chats = chats
        self.users = users or []
        notifier.FEISHU_TOKEN.update({"token": "", "expires": 0.0})

    def tearDown(self):
        notifier.FEISHU_TOKEN.update({"token": "", "expires": 0.0})

    def job(self, run):
        return {"id": 1, "day": "2026-09-18", "slot_id": "evening-out", "label": "晚上下班",
                "status": "success", "mode": "live", "message": "签到成功",
                "planned": time.time() - 60, "finished": time.time() - 10, "run_dir": str(run)}

    def test_full_flow_single_group(self):
        self.setUp2([{"chat_id": "oc_grp", "name": "打卡"}])
        run = success_run(self.dir / "logs")
        state = {}
        with patch.object(notifier, "http_request", self.route):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), state)
        self.assertTrue(delivered)
        self.assertEqual(warning, "")
        methods = [(m, url.split("open-apis")[-1].split("?")[0]) for m, url, _, _ in self.calls]
        # token, chat discovery, text message, image upload, image message
        self.assertEqual([m for m, _ in methods], ["POST", "GET", "POST", "POST", "POST"])
        self.assertEqual(state["feishu_chat_id"], "oc_grp")                # discovery cached
        text_payload = json.loads(self.calls[2][2])
        self.assertEqual(text_payload["msg_type"], "text")
        self.assertIn("签到成功", json.loads(text_payload["content"])["text"])
        upload_headers = self.calls[3][3]
        self.assertIn("multipart/form-data", upload_headers.get("Content-Type", ""))
        self.assertIn(b"image_type", self.calls[3][2])
        self.assertLessEqual(len(self.calls[3][2]), 2_500_000)
        image_payload = json.loads(self.calls[4][2])
        self.assertEqual(image_payload["msg_type"], "image")
        self.assertEqual(json.loads(image_payload["content"])["image_key"], "img_v2_key")

    def test_token_cached_and_reused(self):
        self.setUp2([{"chat_id": "oc_grp", "name": "打卡"}])
        run = success_run(self.dir / "logs")
        with patch.object(notifier, "http_request", self.route):
            notifier.send_event(self.cfg, self.job(run), {})
            notifier.send_event(self.cfg, self.job(run), {})
        token_calls = [c for c in self.calls if c[1].endswith("tenant_access_token/internal")]
        self.assertEqual(len(token_calls), 1)                              # second event reuses token

    def test_multiple_groups_requires_chat_id(self):
        self.setUp2([{"chat_id": "oc_a", "name": "A"}, {"chat_id": "oc_b", "name": "B"}])
        run = success_run(self.dir / "logs")
        with patch.object(notifier, "http_request", self.route):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), {})
        self.assertFalse(delivered)
        self.assertIn("chat_id", warning)

    def test_token_expiry_retried_once(self):
        self.setUp2([{"chat_id": "oc_grp", "name": "打卡"}])
        run = success_run(self.dir / "logs")
        state = {"feishu_chat_id": "oc_grp"}
        original = self.route
        expired = {"n": 0}

        def flaky(url, data=None, headers=None, timeout=12):
            if "/im/v1/messages" in url:
                expired["n"] += 1
                if expired["n"] == 1:
                    return 200, json.dumps({"code": 99991663, "msg": "token expired"}).encode()
            return original(url, data, headers, timeout)

        with patch.object(notifier, "http_request", flaky):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), state)
        self.assertTrue(delivered)                                         # retried with fresh token
        token_calls = [c for c in self.calls if c[1].endswith("tenant_access_token/internal")]
        self.assertEqual(len(token_calls), 2)

    def test_upload_failure_is_only_a_warning(self):
        self.setUp2([{"chat_id": "oc_grp", "name": "打卡"}])
        run = success_run(self.dir / "logs")

        def no_upload(url, data=None, headers=None, timeout=12):
            if url.endswith("/im/v1/images"):
                return 200, json.dumps({"code": 230002, "msg": "no permission"}).encode()
            return self.route(url, data, headers, timeout)

        with patch.object(notifier, "http_request", no_upload):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), {})
        self.assertTrue(delivered)                                         # text arrived -> event done
        self.assertIn("截图上传失败", warning)


class FeishuDmTests(unittest.TestCase):
    """Direct-message mode: resolve open_id by phone, no group involved."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="autocheckin-notify-dm-"))
        patcher = sandboxed(self.dir)
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        self.cfg = dict(notifier.default_config(),
                        feishu={"app_id": "cli_x", "app_secret": "s", "chat_id": "",
                                "phone": "13800138000", "email": "", "receive_mode": "p2p"})
        self.calls = []
        self.users = [{"user_id": "ou_me", "mobile": "13800138000"}]

    def route(self, url, data=None, headers=None, timeout=12):
        method = "GET" if data is None else "POST"
        self.calls.append((method, url, data))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return 200, json.dumps({"code": 0, "tenant_access_token": "t-1", "expire": 7200}).encode()
        if "/contact/v3/users/batch_get_id" in url:
            return 200, json.dumps({"code": 0, "data": {"user_list": self.users}}).encode()
        if url.endswith("/im/v1/images"):
            return 200, json.dumps({"code": 0, "data": {"image_key": "img_v2_key"}}).encode()
        if "/im/v1/messages" in url:
            return 200, json.dumps({"code": 0, "data": {"message_id": "om_1"}}).encode()
        return 404, b"{}"

    def job(self, run):
        return {"id": 1, "day": "2026-09-18", "slot_id": "evening-out", "label": "晚上下班",
                "status": "success", "mode": "live", "message": "签到成功",
                "planned": time.time() - 60, "finished": time.time() - 10, "run_dir": str(run)}

    def test_dm_flow_resolves_and_caches_open_id(self):
        notifier.FEISHU_TOKEN.update({"token": "", "expires": 0.0})
        run = success_run(self.dir / "logs")
        state = {}
        with patch.object(notifier, "http_request", self.route):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), state)
        self.assertTrue(delivered)
        self.assertEqual(warning, "")
        self.assertEqual(state["feishu_open_id"], "ou_me")
        lookup = json.loads(next(c[2] for c in self.calls if "batch_get_id" in c[1]))
        self.assertEqual(lookup["mobiles"], ["13800138000", "+8613800138000"])  # CN number tried both ways
        messages = [c for c in self.calls if "/im/v1/messages" in c[1]]
        self.assertEqual(len(messages), 2)                                 # text + image, both DMs
        self.assertIn("receive_id_type=open_id", messages[0][1])
        self.assertEqual(json.loads(messages[0][2])["receive_id"], "ou_me")
        # second send reuses the cached open_id: no new lookup
        with patch.object(notifier, "http_request", self.route):
            notifier.send_event(self.cfg, self.job(run), state)
        lookups = [c for c in self.calls if "batch_get_id" in c[1]]
        self.assertEqual(len(lookups), 1)

    def test_dm_phone_without_match(self):
        self.users = []  # number not found in the tenant
        notifier.FEISHU_TOKEN.update({"token": "", "expires": 0.0})
        run = success_run(self.dir / "logs")
        with patch.object(notifier, "http_request", self.route):
            delivered, warning = notifier.send_event(self.cfg, self.job(run), {})
        self.assertFalse(delivered)
        self.assertIn("没有匹配到飞书用户", warning)

    def test_dm_without_receiver_configured(self):
        notifier.FEISHU_TOKEN.update({"token": "", "expires": 0.0})
        cfg = dict(self.cfg, feishu=dict(self.cfg["feishu"], phone="", email=""))
        run = success_run(self.dir / "logs")
        with patch.object(notifier, "http_request", self.route):
            delivered, warning = notifier.send_event(cfg, self.job(run), {})
        self.assertFalse(delivered)
        self.assertIn("未配置接收人", warning)


class WecomSinkTests(unittest.TestCase):
    def test_payloads_markdown_and_base64_image(self):
        with sandboxed() as (root, _):
            run = success_run(root / "logs")
            cfg = dict(notifier.default_config(), channel="wecom",
                       wecom={"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"})
            captured = []

            def capture(webhook, payload, timeout=12):
                captured.append(payload)
                return True, ""

            job = {"id": 1, "day": "2026-09-18", "slot_id": "evening-out", "label": "晚上下班",
                   "status": "success", "mode": "live", "message": "签到成功",
                   "planned": time.time() - 60, "finished": time.time() - 10, "run_dir": str(run)}
            with patch.object(notifier, "wecom_send", capture), patch("notify_daemon.time.sleep"):
                delivered, warning = notifier.send_event(cfg, job)
            self.assertTrue(delivered)
            self.assertEqual([p["msgtype"] for p in captured], ["markdown", "image"])
            self.assertIn("签到成功", captured[0]["markdown"]["content"])
            raw = base64.b64decode(captured[1]["image"]["base64"])
            self.assertLessEqual(len(raw), cfg["image_max_bytes"])
            self.assertEqual(hashlib.md5(raw).hexdigest(), captured[1]["image"]["md5"])


if __name__ == "__main__":
    unittest.main()
