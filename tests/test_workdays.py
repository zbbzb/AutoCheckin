"""Workday sync: holidays, 调休 weekends, manual override and the weekday fallback."""
import contextlib
import json
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mumu_common as common  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="autocheckin-workday-test-"))


def sample_cfg(**extra):
    cfg = {"enabled": True, "silent": True, "random_minutes": 15, "prepare_seconds": 180,
           "manager_path": "C:/MuMu/MuMuManager.exe", "vm_index": 0, "vm_name": "T",
           "package": "com.example.targetapp", "organization": "测试组织工作台团队",
           "location": {"longitude": 116.4, "latitude": 39.9},
           "slots": [{"id": f"s{i}", "label": f"时段{i}", "kind": "in" if i % 2 == 0 else "out",
                      "time": t, "enabled": True}
                     for i, t in enumerate(("09:00", "11:30", "13:30", "17:30", "19:00", "20:30"))]}
    cfg.update(extra)
    return cfg


@contextlib.contextmanager
def sandboxed():
    saved = (common.WORKDAY_CACHE_DIR, dict(common._workday_cache))
    common.WORKDAY_CACHE_DIR = WORK
    common._workday_cache.clear()
    WORK.mkdir(parents=True, exist_ok=True)  # a previous test's teardown removed it
    try:
        yield WORK
    finally:
        common.WORKDAY_CACHE_DIR = saved[0]
        common._workday_cache.clear()
        common._workday_cache.update(saved[1])
        shutil.rmtree(WORK, ignore_errors=True)


def write_dataset(days):
    (WORK / "workdays-2026.json").write_text(
        json.dumps({"year": 2026, "days": days}), encoding="utf-8")


class IsWorkdayTests(unittest.TestCase):
    def test_weekday_fallback_without_any_data(self):
        with sandboxed():
            cfg = sample_cfg()
            self.assertTrue(common.is_workday(cfg, datetime(2026, 9, 17).date()))   # Thu
            self.assertFalse(common.is_workday(cfg, datetime(2026, 9, 19).date()))  # Sat

    def test_dataset_marks_tiaoxiu_and_holidays(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-09-20", "isOffDay": False},
                           {"name": "中秋", "date": "2026-09-25", "isOffDay": True}])
            cfg = sample_cfg()
            self.assertTrue(common.is_workday(cfg, datetime(2026, 9, 20).date()))   # Sunday WORKS
            self.assertFalse(common.is_workday(cfg, datetime(2026, 9, 25).date()))  # Friday off

    def test_override_beats_dataset_and_weekdays(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-09-20", "isOffDay": False}])
            (WORK / "workdays-override.json").write_text(json.dumps(
                {"work": ["2026-09-19"], "off": ["2026-09-20"]}), encoding="utf-8")
            cfg = sample_cfg()
            self.assertTrue(common.is_workday(cfg, datetime(2026, 9, 19).date()))   # Saturday works
            self.assertFalse(common.is_workday(cfg, datetime(2026, 9, 20).date()))  # dataset flipped

    def test_sync_disabled_ignores_dataset_but_keeps_override(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-09-20", "isOffDay": False},
                           {"name": "假", "date": "2026-09-18", "isOffDay": True}])
            (WORK / "workdays-override.json").write_text(json.dumps(
                {"work": ["2026-09-19"]}), encoding="utf-8")
            cfg = sample_cfg(workday_sync=False)
            self.assertFalse(common.is_workday(cfg, datetime(2026, 9, 20).date()))  # plain weekday
            self.assertTrue(common.is_workday(cfg, datetime(2026, 9, 18).date()))   # holiday ignored
            self.assertTrue(common.is_workday(cfg, datetime(2026, 9, 19).date()))   # override still wins

    def test_invalid_config_value_rejected(self):
        with self.assertRaises(ValueError):
            common.validate_config(sample_cfg(workday_sync="yes"))


class ScheduleTests(unittest.TestCase):
    def test_plan_creates_jobs_for_tiaoxiu_sunday_and_skips_holidays(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-09-20", "isOffDay": False},
                           {"name": "中秋", "date": "2026-09-25", "isOffDay": True}])
            cfg = sample_cfg()
            db_path = WORK / "t.sqlite3"
            with common.database(db_path) as db:
                common.plan(db, cfg, datetime(2026, 9, 17, 8, 0, tzinfo=common.TZ), random.Random(7))
                rows = list(db.execute("SELECT day, COUNT(*) c FROM jobs GROUP BY day"))
                counts = {day: c for day, c in rows}
            self.assertEqual(counts.get("2026-09-17"), 6)   # Thursday
            self.assertNotIn("2026-09-19", counts)          # Saturday off
            self.assertEqual(counts.get("2026-09-20"), 6)   # Sunday 调休 WORKS
            self.assertNotIn("2026-09-25", counts)          # Mid-Autumn off

    def test_due_job_fires_on_tiaoxiu_sunday(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-09-20", "isOffDay": False}])
            cfg = sample_cfg()
            db_path = WORK / "t.sqlite3"
            with common.database(db_path) as db:
                common.plan(db, cfg, datetime(2026, 9, 17, 8, 0, tzinfo=common.TZ), random.Random(7))
                row = dict(db.execute("SELECT * FROM jobs WHERE day='2026-09-20' AND slot_id='s0'").fetchone())
            planned = datetime.fromtimestamp(row["planned"], common.TZ)
            with common.database(db_path) as db:
                job = common.due_job(db, cfg, planned + __import__("datetime").timedelta(seconds=1))
            self.assertIsNotNone(job)
            self.assertEqual(job["day"], "2026-09-20")

    def test_stale_pending_jobs_on_holiday_are_deleted_not_missed(self):
        """Jobs planned before the dataset arrived must vanish, not expire as missed."""
        with sandboxed():
            cfg = sample_cfg()
            db_path = WORK / "t.sqlite3"
            # Wednesday + Friday planned with the plain weekday rule (no dataset yet)
            with common.database(db_path) as db:
                common.plan(db, cfg, datetime(2026, 9, 16, 8, 0, tzinfo=common.TZ), random.Random(7))
            # Now the calendar arrives: 9-18 (Friday) is a holiday
            write_dataset([{"name": "中秋", "date": "2026-09-18", "isOffDay": True}])
            with common.database(db_path) as db:
                common.plan(db, cfg, datetime(2026, 9, 16, 8, 0, tzinfo=common.TZ), random.Random(7))
                remaining = list(db.execute("SELECT DISTINCT day FROM jobs ORDER BY day"))
                missed = db.execute("SELECT COUNT(*) FROM jobs WHERE status='missed'").fetchone()[0]
            days = [d for (d,) in remaining]
            self.assertNotIn("2026-09-18", days)
            self.assertIn("2026-09-16", days)
            self.assertEqual(missed, 0)

    def test_due_job_silent_on_holiday(self):
        with sandboxed():
            write_dataset([{"name": "国庆", "date": "2026-10-01", "isOffDay": True}])
            cfg = sample_cfg()
            db_path = WORK / "t.sqlite3"
            with common.database(db_path) as db:
                common.plan(db, cfg, datetime(2026, 9, 28, 8, 0, tzinfo=common.TZ), random.Random(7))
                job = common.due_job(db, cfg, datetime(2026, 10, 1, 8, 50, tzinfo=common.TZ))
            self.assertIsNone(job)


class SummaryTests(unittest.TestCase):
    def test_summary_lists_extra_work_days(self):
        with sandboxed():
            write_dataset([{"name": "调休", "date": "2026-10-10", "isOffDay": False},
                           {"name": "国庆", "date": "2026-10-01", "isOffDay": True}])
            summary = common.workday_summary(2026)
            self.assertEqual(summary["extra_work"], ["2026-10-10"])
            self.assertEqual(summary["off"], ["2026-10-01"])


if __name__ == "__main__":
    unittest.main()
