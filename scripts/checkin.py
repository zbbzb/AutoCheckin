#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoCheckin 自动化引擎：读取 config/checkin.json，在模拟器里执行定时固定操作。

支持的 action 类型:
  {"type": "launch",  "package": "com.xxx"}                  启动 App
  {"type": "wait",    "seconds": 5}                          等待
  {"type": "tap_text", "text": "签到", "index": 0}            点击包含指定文本的控件(uiautomator)
  {"type": "tap_id",   "id": "com.xxx:id/btn"}               点击指定 resource-id 控件
  {"type": "tap_xy",  "x": 540, "y": 1800}                   按屏幕坐标点击
  {"type": "swipe",   "x1":540,"y1":1500,"x2":540,"y2":500,"ms":300}  滑动
  {"type": "key",     "code": "back"}                        按键: back/home/enter
  {"type": "geo",     "lon": 116.39, "lat": 39.91}           重新设置虚拟定位
  {"type": "screenshot", "name": "after_checkin"}            截图存证
  {"type": "text",    "content": "hello"}                    输入文本(需先聚焦输入框)

用法: python checkin.py [配置文件路径，默认 config/checkin.json]
"""
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB = str(ROOT / "android-sdk" / "platform-tools" / "adb.exe")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from set_location import geo_fix, find_emulator_serial  # noqa: E402

KEY_CODES = {"back": 4, "home": 3, "enter": 66, "tab": 61, "del": 67}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


_serial_cache = None


def get_serial() -> str:
    """探测模拟器的 adb 序列号（emulator-xxxx），避免误操作真机。"""
    global _serial_cache
    if _serial_cache:
        return _serial_cache
    try:
        _serial_cache = find_emulator_serial()
    except RuntimeError:
        return ""
    return _serial_cache


def adb(*args, timeout=30, binary=False):
    """执行 adb 命令（定向到模拟器），返回 stdout。"""
    serial = get_serial()
    if not serial:
        raise RuntimeError("未找到指定模拟器，拒绝向其他 ADB 设备发送命令")
    cmd = [ADB, "-s", serial] + list(args)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode:
        error = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ADB 执行失败: {error}")
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", errors="replace")


def wait_device(max_wait=180):
    """等待模拟器完成开机。"""
    log("等待模拟器连接...")
    for _ in range(max_wait // 3):
        if get_serial():
            break
        time.sleep(3)
    else:
        raise RuntimeError("adb 未发现模拟器设备")
    for _ in range(max_wait // 2):
        out = adb("shell", "getprop", "sys.boot_completed").strip()
        if out == "1":
            log("模拟器已就绪")
            return
        time.sleep(2)
    raise RuntimeError("模拟器开机超时")


def dump_ui() -> str:
    """dump 当前界面并 pull 到本地，返回 XML 文本。"""
    adb("shell", "uiautomator", "dump", "/sdcard/uidump.xml", timeout=30)
    return adb("exec-out", "cat", "/sdcard/uidump.xml")


def parse_bounds(bounds: str):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_nodes(xml_text: str, text: str = None, res_id: str = None):
    """在 UI XML 中查找匹配节点，返回中心坐标列表。"""
    hits = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return hits
    for node in root.iter("node"):
        if text is not None:
            hay = (node.get("text") or "") + " " + (node.get("content-desc") or "")
            if text not in hay:
                continue
        if res_id is not None and node.get("resource-id") != res_id:
            continue
        center = parse_bounds(node.get("bounds", ""))
        if center:
            hits.append(center)
    return hits


class Runner:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.shot_idx = 0

    def screenshot(self, name: str):
        self.shot_idx += 1
        fname = f"{self.shot_idx:02d}_{name or 'shot'}.png"
        data = adb("exec-out", "screencap", "-p", timeout=60, binary=True)
        (self.run_dir / fname).write_bytes(data)
        log(f"截图 -> logs/{self.run_dir.name}/{fname}")

    def run_actions(self, actions, default_pkg=""):
        for i, act in enumerate(actions, 1):
            t = act.get("type")
            desc = {k: v for k, v in act.items() if k != "type"}
            log(f"动作 {i}/{len(actions)}: {t} {desc if t in ('tap_text','tap_id','launch') else ''}")
            if t == "launch":
                pkg = act.get("package", default_pkg)
                adb("shell", "monkey", "-p", pkg, "-c",
                    "android.intent.category.LAUNCHER", "1")
                time.sleep(act.get("wait", 5))
                try:
                    alive = adb("shell", "pidof", pkg).strip()
                except RuntimeError:
                    alive = ""
                if not alive:
                    crash = adb("logcat", "-b", "crash", "-d", "-t", "100")
                    (self.run_dir / "launch-crash.txt").write_text(crash, encoding="utf-8")
                    raise RuntimeError(f"{pkg} 启动后进程已退出，详见 launch-crash.txt")
            elif t == "start_activity":
                adb("shell", "am", "start", "-n", act["component"])
                time.sleep(act.get("wait", 3))
            elif t == "wait":
                time.sleep(float(act["seconds"]))
            elif t == "tap_text":
                self.tap_find(text=act["text"], index=act.get("index", 0))
            elif t == "tap_id":
                self.tap_find(res_id=act["id"], index=act.get("index", 0))
            elif t == "tap_xy":
                adb("shell", "input", "tap", str(act["x"]), str(act["y"]))
                time.sleep(act.get("wait", 1.5))
            elif t == "swipe":
                adb("shell", "input", "swipe", str(act["x1"]), str(act["y1"]),
                    str(act["x2"]), str(act["y2"]), str(act.get("ms", 300)))
                time.sleep(act.get("wait", 1))
            elif t == "key":
                adb("shell", "input", "keyevent", str(KEY_CODES.get(act["code"], act["code"])))
                time.sleep(act.get("wait", 1))
            elif t == "text":
                adb("shell", "input", "text", act["content"])
                time.sleep(act.get("wait", 1))
            elif t == "geo":
                geo_fix(act["lon"], act["lat"], serial=get_serial(), verify=act.get("verify", False))
            elif t == "screenshot":
                self.screenshot(act.get("name", ""))
            else:
                raise ValueError(f"未知动作类型: {t}")
            time.sleep(0.5)

    def tap_find(self, text=None, res_id=None, index=0, retries=3):
        for attempt in range(retries):
            hits = find_nodes(dump_ui(), text=text, res_id=res_id)
            if hits:
                if index >= len(hits):
                    index = 0
                x, y = hits[index]
                log(f"  找到目标(共{len(hits)}个)，点击 ({x},{y})")
                adb("shell", "input", "tap", str(x), str(y))
                time.sleep(1.5)
                return True
            log(f"  未找到目标，重试 {attempt + 1}/{retries}...")
            time.sleep(3)
        raise RuntimeError(f"未找到目标 text={text} id={res_id}，停止当前 App 流程")


def ensure_quic_block():
    """屏蔽 UDP 443 (QUIC)：模拟器的 -http-proxy 只转发 TCP，
    Google 系 App 走 QUIC 时会一直无响应，禁掉后强制回落 TCP 走代理。"""
    try:
        if adb("shell", "id", "-u").strip() != "0":
            adb("root", timeout=30)
            time.sleep(5)
        if "dpt:443" not in adb("shell", "iptables", "-L", "OUTPUT", "-n"):
            adb("shell", "iptables", "-A", "OUTPUT", "-p", "udp",
                "--dport", "443", "-j", "REJECT")
            log("已添加 QUIC(UDP 443) 屏蔽规则，强制走 TCP")
    except Exception as e:
        log(f"[警告] 设置 QUIC 屏蔽失败: {e}")


def ensure_running():
    """确保模拟器在运行，否则启动它。"""
    if get_serial():
        wait_device()
        return
    log("模拟器未运行，正在启动...")
    emulator = str(ROOT / "android-sdk" / "emulator" / "emulator.exe")
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(ROOT / "android-sdk")
    env["ANDROID_AVD_HOME"] = str(ROOT / "avds")
    serial = env.get("ANDROID_SERIAL", "emulator-5554")
    if not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError("自动启动仅支持 AVD 的 emulator-<端口> 序列号")
    command = [emulator, "-avd", "AutoCheckin", "-port", serial.split("-")[1],
               "-no-window", "-no-snapshot", "-gpu", "auto"]
    if env.get("EMU_PROXY"):
        command += ["-http-proxy", env["EMU_PROXY"]]
    with (ROOT / "logs" / "emulator-auto.log").open("ab") as emulator_log:
        subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=emulator_log, stderr=subprocess.STDOUT)
    wait_device()
    time.sleep(5)


def main():
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "checkin.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    run_dir = ROOT / "logs" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    ensure_running()
    # This network workaround changes the device's root state. Enable only for
    # configurations that explicitly require a TCP-only emulator proxy.
    if cfg.get("network", {}).get("block_quic", False):
        ensure_quic_block()

    loc = cfg.get("location")
    if loc:
        log(f"设置虚拟定位: ({loc['lon']}, {loc['lat']})")
        geo_fix(loc["lon"], loc["lat"], serial=get_serial(), verify=loc.get("verify", False))

    runner = Runner(run_dir)
    exit_code = 0
    for app in cfg.get("apps", []):
        pkg = app.get("package", "")
        log(f"===== 开始处理 App: {pkg or '(未指定)'} =====")
        if pkg:
            installed = f"package:{pkg}" in adb("shell", "pm", "list", "packages").splitlines()
            if not installed:
                log(f"[警告] {pkg} 未安装，跳过。请把 APK 放入 apps/ 并运行 install_app.bat")
                exit_code = 1
                continue
        try:
            runner.run_actions(app.get("actions", []), default_pkg=pkg)
        except Exception as e:
            log(f"[错误] 处理 {pkg} 失败: {e}")
            runner.screenshot("error")
            exit_code = 1
        # 回到桌面，避免影响下一个 App
        adb("shell", "input", "keyevent", "3")
        time.sleep(1)
    log(f"全部完成，截图与日志在: {run_dir}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
