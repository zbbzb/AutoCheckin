import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import set_location as location
import checkin


class AutomationTests(unittest.TestCase):
    def test_invalid_coordinates_do_not_touch_adb(self):
        with patch.object(location, "adb_command") as adb:
            for lon, lat in [(181, 0), (0, 91), (float("nan"), 0), (0, float("inf"))]:
                with self.assertRaises(ValueError):
                    location.geo_fix(lon, lat, serial="emulator-5554")
            adb.assert_not_called()

    def test_phone_is_never_a_location_target(self):
        with patch.object(location, "adb_command") as adb:
            with self.assertRaises(ValueError):
                location.geo_fix(1, 2, serial="attached-phone")
            adb.assert_not_called()

    def test_offline_configured_avd_does_not_select_other_device(self):
        result = subprocess.CompletedProcess([], 0,
            "List of devices attached\nphone\tdevice\nemulator-5556\tdevice\nemulator-5554\toffline\n")
        with patch.dict(os.environ, {"ANDROID_SERIAL": "emulator-5554"}), \
                patch.object(location.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError):
                location.find_emulator_serial()

    def test_console_error_is_not_success(self):
        with patch.object(location, "adb_command", side_effect=["true\n", "KO: invalid command\n"]):
            with self.assertRaises(RuntimeError):
                location.geo_fix(1, 2, serial="emulator-5554")

    def test_provider_parser_handles_windows_newlines_and_ages(self):
        dump = ("Location Manager State:\r\n  Location Providers:\r\n    gps provider:\r\n"
                "      last location=Location[gps 31.239066,121.490317 hAcc=5.0 et=+1h2m3s400ms]\r\n"
                "    network provider:\r\n      last location=null\r\n  Event Log:\r\n")
        with patch.object(location, "adb_command", side_effect=[dump, "3724.4 0"]):
            state = location.location_state("emulator-5554")
        self.assertAlmostEqual(state["gps"]["age_seconds"], 1)
        self.assertEqual(state["gps"]["lat"], 31.239066)
        self.assertIsNone(state["network"])

    def test_matching_but_stale_fix_fails_verification(self):
        with patch.object(location, "location_state", return_value={
                "gps": {"lon": 1, "lat": 2, "age_seconds": 30}}):
            with self.assertRaises(RuntimeError):
                location.wait_for_location(1, 2, "emulator-5554", timeout=0)

    def test_no_device_command_cannot_fall_back_to_phone(self):
        with patch.object(checkin, "get_serial", return_value=""), \
                patch.object(checkin.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                checkin.adb("shell", "input", "tap", "1", "2")
            run.assert_not_called()

    def test_launch_crash_is_reported_even_if_launcher_succeeded(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(checkin, "adb", side_effect=["Events injected: 1", RuntimeError("no pid"), "crash trace"]), \
                patch.object(checkin.time, "sleep"):
            with self.assertRaises(RuntimeError):
                checkin.Runner(Path(directory)).run_actions([{"type": "launch"}], "com.test.app")
            self.assertEqual((Path(directory) / "launch-crash.txt").read_text(), "crash trace")

    def test_missing_ui_target_stops_actions(self):
        with patch.object(checkin, "dump_ui", return_value="<hierarchy/>"), \
                patch.object(checkin.time, "sleep"):
            with self.assertRaises(RuntimeError):
                checkin.Runner(Path(".")).tap_find(text="missing", retries=1)


if __name__ == "__main__":
    unittest.main()
