"""Entry point for the AutoCheckin scheduled task.

The task fires at logon and again every minute as a watchdog. This guard decides
whether that start should actually happen:

* if a manual stop was recorded for the current boot session, do nothing, so the
  watchdog cannot resurrect a service the user deliberately stopped;
* otherwise make sure the service is running;
* always make sure the tray companion is running.

Keeping this logic in one place means the scheduled task action never needs to know
about shutdown policy, and duplicate launches stay harmless. The tray needs supervising
because it lives in the user session, where a logon/logoff cycle, a session reset or a
crash removes it, and the Startup folder only runs once per logon.
"""
import subprocess
import sys
from pathlib import Path

from mumu_common import ROOT, SERVICE_LOCK, TRAY_LOCK, NO_WINDOW, running, stop_requested

SERVICE = ROOT / "scripts/mumu_service.py"
TRAY = ROOT / "scripts/mumu_tray.ps1"


def note(message):
    """The task runs under pythonw.exe, which has no stdout; never fail over logging."""
    try:
        print(message)
    except Exception:
        pass


def start_service():
    if running(SERVICE_LOCK):
        return False
    # Capture stderr: this runs under pythonw.exe where a failed spawn would be
    # completely silent, and "service silently not started" is the worst failure mode.
    log = ROOT / "logs/service-worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        subprocess.Popen([sys.executable, str(SERVICE)], cwd=ROOT,
                         stdout=stream, stderr=stream, creationflags=NO_WINDOW)
    return True


def start_tray():
    """Restart the tray companion when it is gone.

    It is started even after a manual service stop on purpose: the tray is the control
    surface for starting the service again, so it must outlive the service.
    """
    if running(TRAY_LOCK):
        return False
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.exists():
        powershell = "powershell.exe"
    subprocess.Popen([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", str(TRAY)], cwd=ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=NO_WINDOW)
    return True


def main():
    # The tray goes first: it is how a human recovers from a stopped service, so it
    # must not depend on the service being wanted.
    if start_tray():
        note("started: tray")
    if stop_requested():
        return  # service stopped by hand in this boot session
    if start_service():
        # Only speak when something changed, so the per-minute watchdog is quiet.
        note("started: service")


if __name__ == "__main__":
    main()
