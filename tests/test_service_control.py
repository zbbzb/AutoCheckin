"""Visual service control: manual stop marker, task guard and service endpoints."""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mumu_common as common
import mumu_service as service
import mumu_task_guard as guard

WORK = Path(tempfile.mkdtemp(prefix="autocheckin-test-"))
DATA = WORK / "data"
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = WORK / "mumu.json"
MARKER = DATA / "service.stop"
PID_FILE = DATA / "service.pid"
CONFIG.write_text(json.dumps({
    "enabled": True, "silent": True, "random_minutes": 15, "prepare_seconds": 180,
    "manager_path": "C:/MuMu/MuMuManager.exe", "vm_index": 2, "vm_name": "Test-Instance",
    "package": "com.delicloud.app.smartoffice", "organization": "测试组织",
    "location": {"longitude": 103.0, "latitude": 30.0},
    "slots": [{"id": f"slot-{i}", "label": f"时段{i}", "kind": "in" if i % 2 == 0 else "out",
               "time": t, "enabled": True}
              for i, t in enumerate(["09:00", "11:30", "13:30", "17:30", "19:00", "20:30"])]},
    ensure_ascii=False), encoding="utf-8")

_PATHS = ("DATABASE", "CONFIG", "STOP_MARKER", "SERVICE_LOCK", "TRAY_LOCK")


@contextlib.contextmanager
def sandboxed_paths():
    """Point the shared path globals at WORK for one test, then put them back.

    mumu_common owns these globals and mumu_service rebinds the ones it imported by
    name at import time. They cannot be patched once at module level: test_mumu and
    test_regressions import the same modules and must keep using the real paths.
    """
    saved = {name: getattr(common, name) for name in _PATHS}
    saved_service = {name: getattr(service, name) for name in _PATHS if hasattr(service, name)}
    common.DATABASE = DATA / "checkin.sqlite3"
    common.CONFIG = CONFIG
    common.STOP_MARKER = MARKER
    common.SERVICE_LOCK = PID_FILE
    common.TRAY_LOCK = DATA / "tray.pid"
    for name in saved_service:
        setattr(service, name, getattr(common, name))
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(common, name, value)
        for name, value in saved_service.items():
            setattr(service, name, value)


def tearDownModule():
    shutil.rmtree(WORK, ignore_errors=True)


class StopMarkerTests(unittest.TestCase):
    def setUp(self):
        self.paths = sandboxed_paths()
        self.paths.__enter__()
        common.clear_stopped()

    def tearDown(self):
        common.clear_stopped()
        self.paths.__exit__(None, None, None)

    def test_no_marker_means_the_service_may_start(self):
        self.assertFalse(common.stop_requested())

    def test_manual_stop_suppresses_starts_for_this_boot(self):
        common.mark_stopped("test")
        self.assertTrue(common.stop_requested())
        self.assertEqual(common.boot_id(),
                         json.loads(MARKER.read_text(encoding="utf-8"))["boot"])

    def test_marker_from_an_earlier_boot_is_ignored(self):
        """A reboot must bring the service back even though a stop was recorded."""
        MARKER.write_text(json.dumps({"boot": "some-previous-boot", "reason": "test"}),
                          encoding="utf-8")
        self.assertFalse(common.stop_requested())

    def test_clear_stopped_forgets_the_manual_stop(self):
        common.mark_stopped("test")
        common.clear_stopped()
        self.assertFalse(common.stop_requested())

    def test_corrupt_marker_fails_open(self):
        """A damaged marker must not silently disable the check-in service forever."""
        MARKER.write_text("{not json", encoding="utf-8")
        self.assertFalse(common.stop_requested())

    def test_pid_files_round_trip(self):
        self.assertIsNone(common.read_pid(PID_FILE))
        common.write_pid(PID_FILE, 4321)
        self.assertEqual(common.read_pid(PID_FILE), 4321)
        common.clear_pid(PID_FILE)
        self.assertIsNone(common.read_pid(PID_FILE))


class TaskGuardTests(unittest.TestCase):
    def test_manual_stop_keeps_the_watchdog_from_resurrecting_the_service(self):
        with patch.object(guard, "stop_requested", return_value=True), \
             patch.object(guard, "start_service") as start:
            guard.main()
        start.assert_not_called()

    def test_guard_starts_the_service_when_not_stopped(self):
        with patch.object(guard, "stop_requested", return_value=False), \
             patch.object(guard, "start_service", return_value=True) as start:
            guard.main()
        start.assert_called_once()


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.paths = sandboxed_paths()
        self.paths.__enter__()
        service.SHUTDOWN.clear()
        service.SHUTDOWN_KIND = None
        common.clear_stopped()

    def tearDown(self):
        service.SHUTDOWN.clear()
        service.SHUTDOWN_KIND = None
        common.clear_stopped()
        self.paths.__exit__(None, None, None)

    def test_stop_cancels_jobs_and_records_the_manual_stop(self):
        db = MagicMock()
        with patch.object(service, "database") as database:
            database.return_value.__enter__.return_value = db
            service.request_shutdown("stop")
        self.assertTrue(common.stop_requested())
        self.assertEqual(service.SHUTDOWN_KIND, "stop")
        self.assertTrue(service.SHUTDOWN.is_set())
        self.assertIn("status='cancelled'", db.execute.call_args.args[0])

    def test_restart_clears_the_marker_and_spawns_the_detached_helper(self):
        common.mark_stopped("earlier")
        with patch.object(service, "database") as database, \
             patch.object(service.subprocess, "Popen") as popen:
            database.return_value.__enter__.return_value = MagicMock()
            service.request_shutdown("restart")
        self.assertFalse(common.stop_requested())
        self.assertTrue(service.SHUTDOWN.is_set())
        self.assertEqual(service.SHUTDOWN_KIND, "restart")
        popen.assert_called_once()
        self.assertIn("mumu_restart.ps1", " ".join(str(a) for a in popen.call_args.args[0]))

    def test_health_reports_pid_and_live_switch(self):
        payload = service.health()
        self.assertEqual(payload["service"], "running")
        self.assertEqual(payload["pid"], os.getpid())
        self.assertIs(payload["enabled"], True)
        self.assertTrue(payload["token"])


class TrayTextTests(unittest.TestCase):
    """The tray's menu labels come from a JSON resource; empty labels are unusable."""

    def test_shipped_resource_has_every_label(self):
        import tray_helper
        ok, detail = tray_helper.text_ok()
        self.assertTrue(ok, detail)

    def test_launcher_rejects_a_resource_with_empty_labels(self):
        import tray_helper
        broken = WORK / "broken_text.json"
        broken.write_text(json.dumps({"menuOpen": "", "menuRestart": "x", "menuStop": "x"}),
                          encoding="utf-8")
        original = tray_helper.TEXT_RESOURCE
        tray_helper.TEXT_RESOURCE = broken
        try:
            ok, detail = tray_helper.text_ok()
        finally:
            tray_helper.TEXT_RESOURCE = original
        self.assertFalse(ok)
        self.assertIn("menuOpen", detail)

    def test_launcher_rejects_a_missing_resource(self):
        import tray_helper
        original = tray_helper.TEXT_RESOURCE
        tray_helper.TEXT_RESOURCE = WORK / "does-not-exist.json"
        try:
            ok, detail = tray_helper.text_ok()
        finally:
            tray_helper.TEXT_RESOURCE = original
        self.assertFalse(ok)
        self.assertIn("unreadable", detail)


if __name__ == "__main__":
    unittest.main()
