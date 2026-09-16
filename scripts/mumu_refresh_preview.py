"""Refresh the logged-in Deli workbench from its title. Never tap attendance.

Captures the refresh indicator and settled page for visual verification.
Requires the MuMu instance and Deli workbench to be open (1080 x 1920).
"""
import argparse
import json
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADB = ROOT / "android-sdk/platform-tools/adb.exe"
MANAGER = Path(r"D:\AAA-mytools\LIFE\MUMU\MuMuPlayer\nx_main\MuMuManager.exe")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-index", type=int, default=2)
    parser.add_argument("--count", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "logs" / datetime.now().strftime("mumu-refresh-%Y%m%d-%H%M%S")
    info = json.loads(subprocess.check_output(
        [str(MANAGER), "info", "-v", str(args.vm_index)], encoding="utf-8", timeout=15))
    if not info.get("is_android_started") or info.get("adb_host_ip") != "127.0.0.1":
        raise RuntimeError("Requested local MuMu instance is not running")
    serial = f"127.0.0.1:{int(info['adb_port'])}"
    subprocess.run([str(ADB), "connect", serial], check=True, capture_output=True, timeout=15)

    def adb(*command):
        return subprocess.check_output([str(ADB), "-s", serial, *command], timeout=20)

    def check_workbench():
        activities = adb("shell", "dumpsys", "activity", "activities").decode("utf-8", "replace")
        resumed = "\n".join(line for line in activities.splitlines() if "topResumedActivity" in line)
        if "com.delicloud.app.smartoffice/.ui.activity.MainActivity " not in resumed:
            raise RuntimeError("Deli MainActivity must be in the foreground; no gesture sent")
        picture = adb("exec-out", "screencap", "-p")
        if picture[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", picture[16:24]) != (1080, 1920):
            raise RuntimeError("Expected portrait 1080x1920; no gesture sent")
        return picture

    before = check_workbench()
    output.mkdir(parents=True, exist_ok=True)
    (output / "before.png").write_bytes(before)
    records = []
    for number in range(1, args.count + 1):
        check_workbench()
        start = time.monotonic()
        # The title receives pull-to-refresh. Swiping the attendance card only
        # scrolls it. Both ends stay above the circular attendance button.
        gesture = subprocess.Popen([str(ADB), "-s", serial, "shell", "input", "swipe",
                                    "420", "140", "420", "740", "900"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        frames = []
        try:
            for offset in (.7, 1.8, 3, 10):
                time.sleep(max(0, start + offset - time.monotonic()))
                name = f"refresh{number}-{offset:05.2f}s.png"
                (output / name).write_bytes(adb("exec-out", "screencap", "-p"))
                frames.append(name)
            stdout, stderr = gesture.communicate(timeout=10)
            if gesture.returncode:
                raise RuntimeError((stderr or stdout).decode("utf-8", "replace"))
        finally:
            if gesture.poll() is None:
                gesture.kill()
                gesture.wait()
        records.append({"number": number, "from": [420, 140], "to": [420, 740],
                        "duration_ms": 900, "frames": frames, "button_clicked": False,
                        "refresh_verification": "requires visual review of captured frames"})
        (output / "gestures.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"Title gesture {number} captured; inspect refresh indicator and settled frame", flush=True)
    print(output, flush=True)


if __name__ == "__main__":
    main()
