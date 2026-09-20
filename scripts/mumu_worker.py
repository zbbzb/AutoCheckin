"""One isolated MuMu run. All attendance decisions use fresh local screenshots."""
import argparse
import json
import os
import re
import struct
import subprocess
import threading
import time
from pathlib import Path

from mumu_common import ROOT, NO_WINDOW, database, is_workday, now, read_config, update_job


class Cancelled(RuntimeError):
    pass


def normalized(text):
    return re.sub(r"\s+", "", text)


def find_text(items, text, exact=False):
    text = normalized(text)
    for item in items:
        value = normalized(item["text"])
        if (value == text if exact else text in value) and item["score"] >= .65:
            return item
    return None


def validate_attendance(items, cfg, kind):
    if not find_text(items, "今日考勤") or not find_text(items, cfg["organization"][:7]):
        raise RuntimeError("未识别到目标组织的今日考勤页面")
    if not find_text(items, "已在打卡范围内"):
        raise RuntimeError("未确认已在打卡范围内，停止签到")
    label = "上班打卡" if kind == "in" else "下班打卡"
    button = find_text(items, label, exact=True)
    if not button:
        raise RuntimeError(f"未识别到「{label}」按钮，停止签到")
    x, y = button["center"]
    if not (250 < x < 830 and 900 < y < 1600):
        raise RuntimeError("签到按钮位置异常")
    return button


def success_visible(items):
    return any(find_text(items, text) for text in ("打卡成功", "签到成功", "签退成功"))


def find_close_cross(picture):
    """Find a diagonal cross only in the success page's upper-left toolbar."""
    import cv2
    import numpy as np
    data = cv2.imdecode(np.frombuffer(picture, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    crop = data[60:240, 20:190]
    edges = cv2.Canny(crop, 60, 160)
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=12, minLineLength=18, maxLineGap=6)
    if lines is None:
        return None
    # OpenCV 4.x returns (N,1,4); OpenCV 5 returns (N,4). reshape accepts both and
    # makes the row unpacking below independent of the installed major version.
    ascending, descending = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        dx, dy = int(x2-x1), int(y2-y1)
        if not dx or not .65 < abs(dy/dx) < 1.5 or max(abs(dx), abs(dy)) > 80:
            continue
        entry = ((int(x1+x2)/2, int(y1+y2)/2), max(abs(dx), abs(dy)))
        (ascending if dy/dx > 0 else descending).append(entry)
    for (a, length_a) in ascending:
        for (b, length_b) in descending:
            if abs(a[0]-b[0]) < 10 and abs(a[1]-b[1]) < 10 and .5 < length_a/length_b < 2:
                return (round((a[0]+b[0])/2+20), round((a[1]+b[1])/2+60))
    return None


class MuMuRun:
    def __init__(self, job, cfg):
        self.job, self.cfg = job, cfg
        self.output = ROOT / "logs" / f"mumu-{now():%Y%m%d-%H%M%S}-{job['id']}"
        self.output.mkdir(parents=True, exist_ok=True)
        self.serial = None
        self.owned = False
        self.clicked = False
        self.success = False
        self.hide_stop = threading.Event()
        self.hide_thread = None
        self.ocr = None
        self.shot_number = 0
        with database() as db:
            claimed = db.execute("UPDATE jobs SET run_dir=?,pid=?,status='running',started=? WHERE id=? AND status='starting'",
                                 (str(self.output), os.getpid(), time.time(), job["id"]))
            if claimed.rowcount != 1:
                raise Cancelled("本次流程已被停止")

    def log(self, message, phase=None):
        with (self.output / "run.log").open("a", encoding="utf-8") as f:
            f.write(f"[{now().isoformat(timespec='seconds')}] {message}\n")
        fields = {"message": message}
        if phase:
            fields["phase"] = phase
        update_job(self.job["id"], **fields)

    def command(self, args, timeout=20):
        result = subprocess.run(list(map(str, args)), capture_output=True, timeout=timeout,
                                creationflags=NO_WINDOW)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).decode("utf-8", "replace").strip()[:600])
        return result.stdout

    def manager(self, *args):
        raw = self.command([self.cfg["manager_path"], *args])
        result = json.loads(raw.decode("utf-8-sig"))
        if int(result.get("errcode", result.get("error_code", 0))) != 0:
            raise RuntimeError(f"MuMu: {result}")
        return result

    def control(self, *args):
        return self.manager("control", "-v", self.cfg["vm_index"], *args)

    def info(self):
        info = self.manager("info", "-v", self.cfg["vm_index"])
        if str(info.get("index")) != str(self.cfg["vm_index"]) or info.get("name") != self.cfg["vm_name"]:
            raise RuntimeError("MuMu 实例编号/名称不匹配，请检查设置")
        return info

    def adb(self, *args, timeout=20):
        if not self.serial or not re.fullmatch(r"127\.0\.0\.1:\d+", self.serial):
            raise RuntimeError("尚未连接指定的本机 MuMu 实例")
        return self.command([ROOT / "android-sdk/platform-tools/adb.exe", "-s", self.serial, *args], timeout)

    def check_cancelled(self):
        with database() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (self.job["id"],)).fetchone()
        if not row or row["status"] == "cancelled":
            raise Cancelled("本次运行已停止")
        cfg = read_config()
        if self.job["mode"] == "live":
            slot = next((s for s in cfg["slots"] if s["id"] == self.job["slot_id"]), None)
            if not cfg["enabled"] or not slot or not slot["enabled"]:
                raise Cancelled("自动签到或此时段已关闭")
            if cfg != self.cfg:
                raise Cancelled("设置已变更，本次运行取消；新设置用于后续时段")
            if not is_workday(cfg, now().date()) or time.time() >= self.job["window_end"]:
                raise RuntimeError("非工作日或已过签到时间窗口，停止提交")

    def pause(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while time.monotonic() < end:
            self.check_cancelled()
            time.sleep(min(.5, max(0, end-time.monotonic())))

    def wait_until(self, timestamp):
        # Re-read wall time across sleep/resume or a system clock correction.
        while time.time() < timestamp:
            self.pause(min(1, timestamp-time.time()))

    def hide_loop(self):
        while not self.hide_stop.is_set():
            try:
                self.control("hide_window")
            except Exception:
                pass  # May not have a window yet during boot.
            self.hide_stop.wait(.5)

    def adb_connect(self):
        """Connect the instance over adb, healing a wedged host-side server.

        2026-09-15 incident: an adb server that had run for eight days cached
        this serial as permanently offline, so every run timed out in start()
        while Android itself booted fine. `disconnect` drops the stale entry;
        if the device still reports offline, one server restart heals a deeper
        wedge. Both act on the host-side adb daemon only, never the emulator.
        """
        adb = ROOT / "android-sdk/platform-tools/adb.exe"
        try:
            self.command([adb, "disconnect", self.serial])
        except RuntimeError:
            pass  # "not connected" is fine; we only want the cache cleared.
        self.command([adb, "connect", self.serial])
        listing = self.command([adb, "devices"]).decode("utf-8", "replace")
        state = ""
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == self.serial:
                state = fields[1]
        if state == "offline":
            self.log(f"adb 对 {self.serial} 显示 offline，重启 adb server 后重连", "emulator")
            try:
                self.command([adb, "kill-server"])
            except RuntimeError:
                pass
            self.command([adb, "connect", self.serial])

    def start(self):
        self.log("正在启动目标 MuMu 实例", "emulator")
        self.check_cancelled()
        info = self.info()
        self.owned = True
        if self.cfg["silent"]:
            self.hide_thread = threading.Thread(target=self.hide_loop, daemon=True)
            self.hide_thread.start()
        if not info.get("is_process_started"):
            self.control("launch")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            self.check_cancelled()
            info = self.info()
            if info.get("is_android_started"):
                if info.get("adb_host_ip") != "127.0.0.1":
                    raise RuntimeError("实例 ADB 不是本机地址")
                self.serial = f"127.0.0.1:{int(info['adb_port'])}"
                self.adb_connect()
                try:
                    if self.adb("shell", "getprop", "sys.boot_completed").strip() == b"1":
                        (self.output / "instance.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
                        return
                except RuntimeError:
                    pass
            self.pause(2)
        raise RuntimeError("MuMu 启动超时")

    def screenshot(self, label):
        picture = self.adb("exec-out", "screencap", "-p")
        if picture[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", picture[16:24]) != (1080, 1920):
            raise RuntimeError("得力页面需要 1080×1920 竖屏，未发送点击")
        self.shot_number += 1
        path = self.output / f"{self.shot_number:02d}-{label}.png"
        path.write_bytes(picture)
        return picture, path

    def observe(self, label):
        activities = self.adb("shell", "dumpsys", "activity", "activities").decode("utf-8", "replace")
        resumed = "\n".join(line for line in activities.splitlines() if "topResumedActivity" in line)
        if self.cfg["package"] not in resumed:
            raise RuntimeError("得力e+ 没有处于模拟器前台")
        picture, path = self.screenshot(label)
        if self.ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        results, _ = self.ocr(picture)
        items = []
        for box, text, score in results or []:
            items.append({"text": text, "score": float(score),
                          "center": [round(sum(p[0] for p in box)/4), round(sum(p[1] for p in box)/4)],
                          "box": box})
        path.with_suffix(".json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return picture, items

    def tap(self, position):
        self.check_cancelled()
        self.adb("shell", "input", "tap", *position)

    def open_workbench(self):
        self.log("设置虚拟定位", "location")
        location = self.cfg["location"]
        response = self.control("tool", "location", "-lon", location["longitude"], "-lat", location["latitude"])
        (self.output / "location.json").write_text(json.dumps({**location, "response": response}, indent=2), encoding="utf-8")
        self.adb("shell", "cmd", "location", "set-location-enabled", "true")
        self.log("打开得力e+，等待目标组织工作台", "app")
        self.adb("shell", "am", "force-stop", self.cfg["package"])
        self.adb("shell", "monkey", "-p", self.cfg["package"], "-c", "android.intent.category.LAUNCHER", "1")
        self.pause(8)
        for attempt in range(8):
            try:
                _, items = self.observe("workbench")
            except RuntimeError:
                if attempt >= 7:
                    raise
                self.pause(3)
                continue
            if find_text(items, "今日考勤") and find_text(items, self.cfg["organization"][:7]):
                return
            if find_text(items, "密码登录") or find_text(items, "验证码登录"):
                raise RuntimeError("得力e+ 登录已失效，请手动登录后再运行")
            org = find_text(items, self.cfg["organization"][:7])
            if find_text(items, "切换") and org:
                self.tap(org["center"])
            elif find_text(items, "工作台"):
                self.tap(find_text(items, "工作台")["center"])
                self.pause(2)
                _, items = self.observe("selected-workbench")
                if find_text(items, "今日考勤") and find_text(items, self.cfg["organization"][:7]):
                    return
                self.tap((108, 140))  # Verified organization drawer, only on the workbench.
            else:
                self.pause(3)
            self.pause(3)
        raise RuntimeError("未能进入目标组织工作台")

    def execute(self):
        self.start()
        self.open_workbench()
        if self.job["mode"] == "live":
            self.log("准备完成，等待随机签到时间", "waiting")
            self.wait_until(self.job["planned"] - 45)
        for number in (1, 2):
            self.log(f"第 {number}/2 次刷新，完成后等待 10 秒", f"refresh{number}")
            _, items = self.observe(f"before-refresh{number}")
            if not find_text(items, "今日考勤") or not find_text(items, self.cfg["organization"][:7]):
                raise RuntimeError("刷新前工作台验证失败")
            self.adb("shell", "input", "swipe", "420", "140", "420", "740", "900")
            self.pause(10)
            self.observe(f"after-refresh{number}")
        _, items = self.observe("ready")
        if self.job["mode"] == "preview":
            # A preview accepts the currently displayed attendance direction.
            kind = "in" if find_text(items, "上班打卡", exact=True) else "out"
            validate_attendance(items, self.cfg, kind)
            self.log("演练通过：两次刷新完成、已在打卡范围内，未提交签到", "preview")
            return "preview"
        validate_attendance(items, self.cfg, self.job["kind"])
        if success_visible(items):
            raise RuntimeError("提交前已存在成功提示，无法确认本次结果")
        self.wait_until(self.job["planned"])
        # The page may change while waiting for the random second. Re-read it
        # immediately before submitting rather than clicking an old position.
        _, items = self.observe("before-submit")
        button = validate_attendance(items, self.cfg, self.job["kind"])
        if success_visible(items):
            raise RuntimeError("提交前已存在成功提示，无法确认本次结果")
        self.check_cancelled()
        # Persist intent BEFORE sending ADB. A crash after this point is uncertain,
        # never automatically retried, even when the transport reports an error.
        update_job(self.job["id"], clicked=time.time(), phase="submitting", message="正在提交签到")
        self.clicked = True
        self.adb("shell", "input", "tap", *button["center"])
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            time.sleep(2)  # Always inspect the result after a submitted tap, even if paused.
            picture, items = self.observe("result")
            if success_visible(items):
                self.success = True
                self.log("已识别签到成功，关闭成功页", "success")
                close = find_close_cross(picture)
                if close:
                    self.adb("shell", "input", "tap", *close)
                    time.sleep(2)
                    _, after = self.observe("success-closed")
                    if success_visible(after):
                        raise RuntimeError("签到已成功，但成功页叉号未关闭页面")
                    self.log("签到成功，成功页已关闭", "success")
                else:
                    raise RuntimeError("签到已成功，但未识别到左上角叉号")
                return "success"
        raise RuntimeError("已点击签到，但未识别到成功提示；结果待核实，不重复点击")

    def cleanup(self):
        errors = []
        # Keep the hiding loop alive until the emulator is shut down.
        if self.serial:
            try:
                self.adb("shell", "am", "force-stop", self.cfg["package"])
            except Exception as exc:
                errors.append(f"关闭得力失败: {exc}")
        if self.owned:
            try:
                self.control("shutdown")
                deadline = time.monotonic() + 45
                while self.info().get("is_process_started"):
                    if time.monotonic() > deadline:
                        raise RuntimeError("关闭 MuMu 超时")
                    time.sleep(1)
            except Exception as exc:
                errors.append(f"关闭 MuMu 失败: {exc}")
        self.hide_stop.set()
        if self.hide_thread:
            self.hide_thread.join(timeout=22)
        return errors


def run_job(job_id):
    # OS releases this lock even on process termination. It covers previews too.
    import msvcrt
    lock_path = ROOT / "data/worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            update_job(job_id, status="failed", message="另一个流程正在使用模拟器", finished=time.time())
            return
        worker = None
        status, message = "failed", "流程未启动"
        success_message = None
        try:
            with database() as db:
                job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job["status"] != "starting":
                status, message = job["status"], job["message"]
                return
            cfg = json.loads(job["config_json"])
            worker = MuMuRun(job, cfg)
            status = worker.execute()
            success_message = "演练通过，未提交签到" if status == "preview" else "签到成功，成功页已关闭"
            message = success_message
        except BaseException as exc:
            if worker and worker.success:
                # The submission succeeded and the success page was seen. Keep the
                # success wording; a failure in the closing steps is appended in the
                # finally block instead of replacing the result with a raw exception.
                status = "success"
                message = success_message or "签到成功"
            elif worker and worker.clicked:
                status = "uncertain"
                message = str(exc) or type(exc).__name__
            elif isinstance(exc, Cancelled):
                status = "cancelled"
                message = str(exc) or type(exc).__name__
            else:
                message = str(exc) or type(exc).__name__
            if worker:
                worker.log(message, status)
                try:
                    worker.screenshot("error")
                except Exception:
                    pass
        finally:
            if worker:
                errors = worker.cleanup()
                if errors:
                    message += "；" + "；".join(errors)
                    if status == "preview":
                        status = "failed"
                (worker.output / "result.json").write_text(json.dumps({
                    "status": status, "message": message, "clicked": worker.clicked,
                    "success_observed": worker.success, "cleanup_errors": errors,
                    "finished": now().isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
            update_job(job_id, status=status, phase="done", message=message, finished=time.time())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    run_job(parser.parse_args().job)
