#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AVD GPS injection. Coordinates are WGS84, longitude first.

python scripts/set_location.py 121.490317 31.239066 [5554] [--verify]
--verify requires an app actively requesting GPS; it checks fresh system fixes.
"""
import argparse
import math
import os
import re
import subprocess
import time
from pathlib import Path

ADB = str(Path(__file__).resolve().parent.parent / "android-sdk/platform-tools/adb.exe")


def adb_command(serial, *args, timeout=30):
    result = subprocess.run([ADB, "-s", serial, *map(str, args)],
                            capture_output=True, timeout=timeout)
    output = result.stdout.decode("utf-8", errors="replace")
    error = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"ADB {serial}: {(error or output).strip()}")
    return output


def find_emulator_serial():
    """Only select the configured AVD. Never fall back to a connected phone."""
    serial = os.environ.get("ANDROID_SERIAL", "emulator-5554")
    if not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError("AVD 定位要求 ANDROID_SERIAL=emulator-<端口>，拒绝操作其他设备")
    result = subprocess.run([ADB, "devices"], capture_output=True, check=True,
                            text=True, timeout=15)
    if not any(line.split() == [serial, "device"] for line in result.stdout.splitlines()):
        raise RuntimeError(f"模拟器 {serial} 未连接或未就绪")
    return serial


def find_emulator_console_port():
    return int(find_emulator_serial().split("-")[1])


def validate_coordinates(lon, lat):
    lon, lat = float(lon), float(lat)
    if not (math.isfinite(lon) and -180 <= lon <= 180):
        raise ValueError("经度必须是 [-180, 180] 内的有限数值")
    if not (math.isfinite(lat) and -90 <= lat <= 90):
        raise ValueError("纬度必须是 [-90, 90] 内的有限数值")
    return lon, lat


def location_state(serial):
    """Read framework provider coordinates and age, without changing providers."""
    dump = adb_command(serial, "shell", "dumpsys", "location").replace("\r\n", "\n")
    uptime = float(adb_command(serial, "shell", "cat", "/proc/uptime").split()[0])
    providers = {}
    for match in re.finditer(r"^    (\w+) provider:\n(.*?)(?=^    \w+ provider:|^  \S|\Z)",
                             dump, re.MULTILINE | re.DOTALL):
        name, block = match.groups()
        fix = re.search(r"last location=Location\[\w+ ([\d.+-]+),([\d.+-]+).*?et=([^\s\]]+)", block)
        if not fix:
            providers[name] = None
            continue
        lat, lon, elapsed = fix.groups()
        units = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": .001}
        parts = re.findall(r"(\d+)(ms|d|h|m|s)", elapsed)
        timestamp = sum(int(value) * units[unit] for value, unit in parts)
        providers[name] = {"lat": float(lat), "lon": float(lon),
                           "age_seconds": max(0, uptime - timestamp) if parts else None}
    return providers


def wait_for_location(lon, lat, serial, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        state = location_state(serial)
        gps = state.get("gps")
        if (gps and abs(gps["lon"] - lon) < .00002 and abs(gps["lat"] - lat) < .00002
                and gps["age_seconds"] is not None and gps["age_seconds"] <= 5):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError(f"GPS 尚未回读到新鲜目标坐标（请确认定位开启且有应用请求 GPS）: {state}")
        time.sleep(1)


def geo_fix(lon, lat, port=None, *, serial=None, verify=False, quiet=False):
    lon, lat = validate_coordinates(lon, lat)
    if port is not None:
        if serial is not None and serial != f"emulator-{int(port)}":
            raise ValueError("serial 与 console 端口不一致")
        serial = f"emulator-{int(port)}"
    serial = serial or find_emulator_serial()
    if not re.fullmatch(r"emulator-\d+", serial):
        raise ValueError("geo fix 仅支持明确指定的 AVD 设备")
    if adb_command(serial, "shell", "cmd", "location", "is-location-enabled").strip() != "true":
        raise RuntimeError("Android 系统定位开关未开启")
    # ADB handles the greeting/authentication exchange, then returns the actual
    # command response. The old socket code read one response too early.
    response = adb_command(serial, "emu", "geo", "fix", lon, lat, 0, 8)
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines or lines[-1] != "OK" or any(line.startswith(("KO", "NO")) for line in lines):
        raise RuntimeError(f"模拟器拒绝定位命令: {response.strip()}")
    if verify:
        state = wait_for_location(lon, lat, serial)
        if not quiet:
            print(f"[OK] GPS 已回读验证: 经度={lon}, 纬度={lat}, {state['gps']}", flush=True)
        return state
    if not quiet:
        print(f"[OK] 模拟器已接受 GPS 坐标: 经度={lon}, 纬度={lat} ({serial}); 应用接收情况需回读验证", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lon", type=float)
    parser.add_argument("lat", type=float)
    parser.add_argument("port", type=int, nargs="?")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        geo_fix(args.lon, args.lat, args.port, verify=args.verify)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"[错误] {error}\n")


if __name__ == "__main__":
    main()

