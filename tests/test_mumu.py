import copy
import json
import random
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mumu_common as common
import mumu_worker as worker

FIXTURE = json.loads((Path(__file__).parent / "fixtures/workbench-ocr.json").read_text(encoding="utf-8"))

# Self-contained test config: the repo ships no real config/mumu.json (it holds
# private data), so tests must never read_config(). The organization matches the
# sanitized fixture text "测试组织工作台团...".
def test_config():
    return {
        "enabled": True, "silent": True, "random_minutes": 15, "prepare_seconds": 180,
        "manager_path": "C:/MuMu/MuMuManager.exe", "vm_index": 0, "vm_name": "Test-Instance",
        "package": "com.example.targetapp", "organization": "测试组织工作台团队",
        "location": {"longitude": 116.397128, "latitude": 39.916527},
        "slots": [{"id": i, "label": f"时段{i}", "kind": "in" if n % 2 == 0 else "out",
                   "time": t, "enabled": True}
                  for i, n, t in zip(("morning-in", "morning-out", "afternoon-in",
                                      "afternoon-out", "evening-in", "evening-out"),
                                     range(6),
                                     ("09:00", "11:30", "13:30", "17:30", "19:00", "20:30"))],
    }


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = test_config()
        self.cfg["enabled"] = True
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.current = datetime(2026, 9, 9, 0, 0, tzinfo=common.TZ)

    def tearDown(self):
        self.temp.cleanup()

    def test_eight_day_schedule_is_weekdays_only_with_six_correct_windows(self):
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current, random.Random(7))
            rows = db.execute("SELECT * FROM jobs").fetchall()
            self.assertEqual(len(rows), 36)
            for row in rows:
                stamp = datetime.fromtimestamp(row["planned"], common.TZ)
                self.assertLess(stamp.weekday(), 5)
                self.assertGreaterEqual(row["planned"], row["window_start"])
                self.assertLess(row["planned"], row["window_end"])
                self.assertEqual(row["window_end"]-row["window_start"], 900)
                slot = next(s for s in self.cfg["slots"] if s["id"] == row["slot_id"])
                boundary = datetime.fromtimestamp(row["window_end"] if slot["kind"] == "in" else row["window_start"], common.TZ)
                self.assertEqual(boundary.strftime("%H:%M"), slot["time"])

    def test_reopening_and_switch_edits_keep_random_times(self):
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current)
            before = list(db.execute("SELECT id,planned FROM jobs"))
        self.cfg["enabled"] = False
        self.cfg["slots"][0]["enabled"] = False
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current)
            self.assertEqual(before, list(db.execute("SELECT id,planned FROM jobs")))

    def test_completed_uncertain_and_cancelled_jobs_cannot_replay_after_edits(self):
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current)
            for state in ("success", "uncertain", "cancelled", "failed"):
                db.execute("UPDATE jobs SET status=? WHERE id=1", (state,))
                before = dict(db.execute("SELECT * FROM jobs WHERE id=1").fetchone())
                self.cfg["slots"][0]["time"] = "09:01"
                common.plan(db, self.cfg, self.current)
                self.assertEqual(before, dict(db.execute("SELECT * FROM jobs WHERE id=1").fetchone()))

    def test_disable_and_expired_window_prevent_execution(self):
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current)
            row = dict(db.execute("SELECT * FROM jobs WHERE id=1").fetchone())
            due = datetime.fromtimestamp(row["planned"], common.TZ)
            self.assertEqual(common.due_job(db, self.cfg, due)["id"], row["id"])
            self.cfg["enabled"] = False
            self.assertIsNone(common.due_job(db, self.cfg, due))
            self.cfg["enabled"] = True
            self.cfg["slots"][0]["enabled"] = False
            self.assertIsNone(common.due_job(db, self.cfg, due))
            self.cfg["slots"][0]["enabled"] = True
            expired = datetime.fromtimestamp(row["window_end"]+1, common.TZ)
            common.plan(db, self.cfg, expired)
            self.assertIsNone(common.due_job(db, self.cfg, expired))
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "missed")

    def test_weekend_never_runs_even_with_stale_database(self):
        with common.database(self.path) as db:
            common.plan(db, self.cfg, self.current)
            self.assertIsNone(common.due_job(db, self.cfg, self.current+timedelta(days=3,hours=9)))

    def test_invalid_settings_rejected_before_writing(self):
        for field, value in (("enabled", "yes"), ("random_minutes", 0), ("prepare_seconds", 1), ("vm_index", "all")):
            cfg = copy.deepcopy(self.cfg)
            cfg[field] = value
            with self.assertRaises(ValueError):
                common.validate_config(cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["slots"][0]["time"] = "00:05"
        with self.assertRaises(ValueError):
            common.validate_config(cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["slots"][1]["time"] = "08:50"
        with self.assertRaises(ValueError):
            common.validate_config(cfg)


class AttendanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = test_config()

    def test_real_ocr_fixture_matches_verified_workbench(self):
        button = worker.validate_attendance(FIXTURE, self.cfg, "out")
        self.assertAlmostEqual(button["center"][0], 540, delta=10)

    def test_wrong_direction_range_or_organization_never_authorizes_tap(self):
        with self.assertRaises(RuntimeError):
            worker.validate_attendance(FIXTURE, self.cfg, "in")
        out_of_range = [x for x in FIXTURE if "范围内" not in x["text"]]
        with self.assertRaises(RuntimeError):
            worker.validate_attendance(out_of_range, self.cfg, "out")
        cfg = copy.deepcopy(self.cfg)
        cfg["organization"] = "另一个完全不同的组织"
        with self.assertRaises(RuntimeError):
            worker.validate_attendance(FIXTURE, cfg, "out")

    def test_old_attendance_record_is_not_a_success_confirmation(self):
        self.assertFalse(worker.success_visible(FIXTURE))
        self.assertTrue(worker.success_visible([{"text":"打卡成功", "score":.99}]))

    def fake_runner(self, mode="preview"):
        run = worker.MuMuRun.__new__(worker.MuMuRun)
        run.cfg = self.cfg
        run.job = {"id":100,"mode":mode,"planned":time.time(),"kind":"out"}
        run.start = Mock()
        run.open_workbench = Mock()
        run.log = Mock()
        run.pause = Mock()
        run.check_cancelled = Mock()
        run.observe = Mock(return_value=(b"", FIXTURE))
        run.adb = Mock()
        run.clicked = False
        return run

    def test_preview_refreshes_twice_waits_ten_seconds_and_never_submits(self):
        run = self.fake_runner()
        self.assertEqual(run.execute(), "preview")
        self.assertEqual(run.pause.call_args_list, [unittest.mock.call(10), unittest.mock.call(10)])
        self.assertEqual(run.adb.call_count, 2)
        self.assertTrue(all(call.args[2] == "swipe" for call in run.adb.call_args_list))
        self.assertFalse(run.clicked)

    def test_shutdown_is_attempted_even_if_app_close_fails(self):
        run = self.fake_runner()
        run.serial = "127.0.0.1:16448"
        run.owned = True
        run.hide_stop = Mock()
        run.hide_thread = None
        run.adb.side_effect = RuntimeError("ADB lost")
        run.control = Mock()
        run.info = Mock(return_value={"is_process_started":False})
        self.assertEqual(len(run.cleanup()), 1)
        run.control.assert_called_once_with("shutdown")

    def test_uncertain_submission_is_not_retried(self):
        run = self.fake_runner("live")
        def command(*args):
            if args[2] == "tap":
                raise RuntimeError("Transport lost after sending tap")
        run.adb.side_effect = command
        with patch.object(worker, "update_job") as update:
            with self.assertRaises(RuntimeError):
                run.execute()
        self.assertTrue(run.clicked)
        self.assertEqual(sum(c.args[2] == "tap" for c in run.adb.call_args_list), 1)
        self.assertIn("clicked", update.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
