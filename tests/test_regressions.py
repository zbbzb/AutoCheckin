"""Regressions for the two production faults found on 2026-09-10.

1. find_close_cross unpacked cv2.HoughLinesP rows as (N,1,4), which OpenCV 5 changed
   to (N,4). Every successful check-in raised TypeError while closing the success page,
   and the job was still recorded as "success" with the exception as its message.
2. ThreadingHTTPServer.allow_reuse_address let a second scheduled launch bind the same
   loopback port on Windows, so two schedulers ran against one database.
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cv2
import numpy as np
import mumu_worker as worker
import mumu_service as service


def success_page_with_cross():
    """A synthetic success page carrying the same diagonal cross the real one has."""
    picture = np.full((1920, 1080, 3), 245, dtype=np.uint8)
    cv2.line(picture, (93, 125), (124, 152), (70, 70, 70), 4)
    cv2.line(picture, (124, 125), (93, 152), (70, 70, 70), 4)
    ok, encoded = cv2.imencode(".png", picture)
    assert ok, "failed to encode the fixture"
    return encoded.tobytes()


class CloseCrossTests(unittest.TestCase):
    def test_close_cross_is_found_regardless_of_houghlinesp_shape(self):
        """The worker must not depend on the installed OpenCV major version."""
        picture = success_page_with_cross()
        # Confirm the fixture really is the shape regression that broke production.
        raw = cv2.imdecode(np.frombuffer(picture, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        lines = cv2.HoughLinesP(cv2.Canny(raw[60:240, 20:190], 60, 160), 1, np.pi / 180,
                                threshold=12, minLineLength=18, maxLineGap=6)
        self.assertIsNotNone(lines, "fixture does not produce any Hough lines")
        self.assertEqual(lines.shape[1], 4, "HoughLinesP row layout changed again")

        found = worker.find_close_cross(picture)
        self.assertIsNotNone(found, "diagonal cross was not detected")
        # find_close_cross maps crop coordinates back by (+20, +60).
        self.assertAlmostEqual(found[0], 108, delta=8)
        self.assertAlmostEqual(found[1], 140, delta=8)

    def test_blank_page_yields_no_cross(self):
        blank = np.full((1920, 1080, 3), 245, dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", blank)
        self.assertTrue(ok)
        self.assertIsNone(worker.find_close_cross(encoded.tobytes()))


class LaunchClaimTests(unittest.TestCase):
    def test_losing_the_claim_never_starts_a_second_worker(self):
        db = MagicMock()
        db.execute.return_value.rowcount = 0  # another scheduler already claimed it
        with patch.object(service.subprocess, "Popen") as popen:
            service.launch(db, 42, {"vm_index": 2})
        popen.assert_not_called()
        # Only the claim UPDATE runs; no pid UPDATE for a job we do not own.
        self.assertEqual(db.execute.call_count, 1)

    def test_winning_the_claim_starts_the_worker(self):
        db = MagicMock()
        db.execute.return_value.rowcount = 1
        with patch.object(service.subprocess, "Popen") as popen:
            popen.return_value.pid = 1234
            service.launch(db, 42, {"vm_index": 2})
        popen.assert_called_once()
        self.assertIn("pid=?", db.execute.call_args_list[-1].args[0])


class SingleInstanceTests(unittest.TestCase):
    def test_server_refuses_a_second_bind_on_the_same_port(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            self.assertFalse(service.single_instance_server().allow_reuse_address,
                             "allow_reuse_address must stay off or Windows shares the port")
            with self.assertRaises(OSError):
                service.single_instance_server()(("127.0.0.1", port), service.Handler)
        finally:
            holder.close()

    def test_default_threading_http_server_would_have_shared_the_port(self):
        """Documents the regression: the stock class opts into SO_REUSEADDR."""
        self.assertTrue(service.ThreadingHTTPServer.allow_reuse_address)


class AdbSelfHealTests(unittest.TestCase):
    """2026-09-15: a long-lived adb server cached the serial as offline forever."""

    def bare_run(self, outputs, codes=None):
        run = object.__new__(worker.MuMuRun)  # skip __init__'s DB claim
        run.serial = "127.0.0.1:16448"
        run.calls = []
        codes = codes or {}
        state = {"n": 0}
        logs = []

        def fake(args, timeout=20):
            run.calls.append([str(part) for part in args])
            index = state["n"]
            state["n"] += 1
            if codes.get(index):
                raise RuntimeError(codes[index])
            return outputs[index].encode()

        run.command = fake            # bound on the bare instance, no class patching
        run.log = lambda message, phase=None: logs.append(message)
        return run, logs

    def test_healthy_connect_issues_no_restart(self):
        run, logs = self.bare_run(["", "connected to 127.0.0.1:16448",
                                   "List of devices attached\n127.0.0.1:16448\tdevice\n"])
        worker.MuMuRun.adb_connect(run)
        self.assertEqual(logs, [])
        self.assertNotIn("kill-server", " ".join(" ".join(c) for c in run.calls))

    def test_offline_entry_triggers_one_server_restart(self):
        run, logs = self.bare_run(["", "already connected",
                                   "List of devices attached\n127.0.0.1:16448\toffline\n",
                                   "", "connected to 127.0.0.1:16448"])
        worker.MuMuRun.adb_connect(run)
        self.assertEqual(len(logs), 1)
        verbs = [call[1] for call in run.calls]  # every call is [adb, verb, ...]
        self.assertEqual(verbs, ["disconnect", "connect", "devices", "kill-server", "connect"])

    def test_disconnect_failure_is_ignored(self):
        run, logs = self.bare_run(["", "connected to 127.0.0.1:16448",
                                   "List of devices attached\n127.0.0.1:16448\tdevice\n"],
                                  {0: "error: no such device"})
        worker.MuMuRun.adb_connect(run)  # must not raise despite disconnect failing
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
