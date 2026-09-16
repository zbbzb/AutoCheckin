"""Small bridge the tray (and the launchers) use instead of inline `python -c`.

Two bugs made the inline version unusable:

* Python encodes stdout using the legacy ANSI code page (GBK here), so anything
  printing a path containing the project directory raised
  ``OSError: [Errno 22] Invalid argument`` on flush. Every liveness probe then failed
  and the tray believed the service was permanently dead.
* Embedding Windows paths in a ``-c`` source string means backslashes are read as
  escape/continuation characters, producing SyntaxError.

This helper therefore prints ASCII flags only, and the caller passes an argument
rather than a code string. It also keeps pid liveness in one place instead of
duplicating the logic in PowerShell and VBScript.
"""
import json
import sys

from mumu_common import (ROOT, SERVICE_LOCK, TRAY_LOCK, clear_stopped, mark_stopped, read_pid,
                         running, stop_requested)

HELP = ("commands: status | service-pid | tray-pid | text-check | clear-stop | mark-stop | guard | port "
        "| notify-on | notify-off")

TEXT_RESOURCE = ROOT / "scripts/tray_text.json"
REQUIRED_TEXT = ("menuOpen", "menuRestart", "menuStop", "menuExit", "menuNotify",
                 "trayTooltipStarting", "trayTooltipRunning", "trayTooltipStopped",
                 "balloonStarted", "balloonStopped", "balloonStarting",
                 "balloonNotifyOn", "balloonNotifyOff")


def text_ok():
    """Verify the tray's localized wording loads and is not empty.

    A broken resource once produced a tray whose menu had no labels at all, so the
    launcher checks this before starting anything and refuses to create a dead tray.
    """
    try:
        labels = json.loads(TEXT_RESOURCE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"resource unreadable: {exc}"
    missing = [key for key in REQUIRED_TEXT if not str(labels.get(key) or "").strip()]
    if missing:
        return False, "empty labels: " + ", ".join(missing)
    if labels.get("menuRestart") == labels.get("menuStop"):
        return False, "restart and stop labels are identical"
    return True, "ok"


def service_state():
    """Return (alive, pid). A stale pid file is cleaned up by running()."""
    pid = running(SERVICE_LOCK)
    return bool(pid), pid


def main(argv):
    command = (argv[1] if len(argv) > 1 else "status").strip().lower()

    if command == "status":
        alive, pid = service_state()
        # ASCII only, so the legacy console code page can never mangle or crash it.
        print(json.dumps({"service": "running" if alive else "stopped",
                          "pid": pid or 0,
                          "tray_pid": read_pid(TRAY_LOCK) or 0,
                          "stop_marker": bool(stop_requested()),
                          "port": 18765}))
        return 0

    if command == "service-pid":
        alive, pid = service_state()
        print(pid if alive else 0)
        return 0

    if command == "tray-pid":
        pid = running(TRAY_LOCK)
        print(pid or 0)
        return 0

    if command == "text-check":
        ok, detail = text_ok()
        print("ok" if ok else "bad:" + detail)
        return 0 if ok else 1

    if command == "clear-stop":
        clear_stopped()
        print("cleared")
        return 0

    if command == "mark-stop":
        mark_stopped("launcher")
        print("marked")
        return 0

    if command in ("notify-on", "notify-off"):
        # The tray's Feishu-notification switch. notify_daemon.set_enabled never
        # persists .env-merged credentials into config/notify.json.
        import notify_daemon
        enabled = notify_daemon.set_enabled(command == "notify-on")
        print("ok" if enabled == (command == "notify-on") else "failed")
        return 0

    if command == "guard":
        # Hand control to the scheduled-task entry point so a manual start and the
        # per-minute watchdog take exactly the same path.
        sys.path.insert(0, str(ROOT / "scripts"))
        import mumu_task_guard
        mumu_task_guard.main()
        print("guard-done")
        return 0

    if command == "port":
        from mumu_service import PORT
        print(PORT)
        return 0

    sys.stderr.write(HELP + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
