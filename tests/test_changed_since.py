"""Tests for the "changed since you looked" marker (standard library only).

Run from the repo root:  python3 -m unittest discover -s tests
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bram_herdr as bh  # noqa: E402


def agent(pane, seq, terminal="t1", status="idle", focused=False):
    return {"pane_id": pane, "state_change_seq": seq, "terminal_id": terminal,
            "agent_status": status, "focused": focused, "agent": "claude",
            "cwd": "/tmp"}


class ChangesSinceSeen(unittest.TestCase):
    def test_first_sight_is_a_silent_baseline(self):
        changed, seen = bh.changes_since_seen([agent("w1:p1", 10)], {})
        self.assertEqual(changed, set())
        self.assertEqual(seen["w1:p1"]["seq"], 10)
        self.assertEqual(seen["w1:p1"]["terminal_id"], "t1")

    def test_higher_seq_is_flagged(self):
        _, seen = bh.changes_since_seen([agent("w1:p1", 10)], {})
        changed, _ = bh.changes_since_seen([agent("w1:p1", 11)], seen)
        self.assertEqual(changed, {"w1:p1"})

    def test_same_seq_is_not_flagged(self):
        _, seen = bh.changes_since_seen([agent("w1:p1", 10)], {})
        changed, _ = bh.changes_since_seen([agent("w1:p1", 10)], seen)
        self.assertEqual(changed, set())

    def test_flag_persists_until_marked_seen(self):
        _, seen = bh.changes_since_seen([agent("w1:p1", 10)], {})
        changed, seen = bh.changes_since_seen([agent("w1:p1", 12)], seen)
        changed_again, _ = bh.changes_since_seen([agent("w1:p1", 12)], seen)
        self.assertEqual(changed_again, {"w1:p1"})
        seen = bh.mark_seen(seen, [agent("w1:p1", 12)])
        cleared, _ = bh.changes_since_seen([agent("w1:p1", 12)], seen)
        self.assertEqual(cleared, set())

    def test_new_terminal_in_same_pane_resets_baseline(self):
        _, seen = bh.changes_since_seen([agent("w1:p1", 10, terminal="old")], {})
        changed, seen = bh.changes_since_seen([agent("w1:p1", 50, terminal="new")], seen)
        self.assertEqual(changed, set())
        self.assertEqual(seen["w1:p1"]["terminal_id"], "new")
        self.assertEqual(seen["w1:p1"]["seq"], 50)

    def test_missing_or_bad_seq_is_ignored(self):
        a = agent("w1:p1", None)
        b = agent("w1:p2", "not-a-number")
        changed, seen = bh.changes_since_seen([a, b], {})
        self.assertEqual(changed, set())
        self.assertEqual(seen, {})

    def test_malformed_records_become_a_baseline_not_a_crash(self):
        # Hand-edited or corrupt cache entries (Codex review of PR #1, finding 1).
        for bad in ({"seq": "x", "terminal_id": "t1"},
                    {"seq": True, "terminal_id": "t1"},
                    {"seq": None, "terminal_id": "t1"},
                    {"terminal_id": "t1"},
                    ["not", "a", "dict"],
                    "string"):
            with self.subTest(bad=bad):
                changed, seen = bh.changes_since_seen([agent("w1:p1", 10)], {"w1:p1": bad})
                self.assertEqual(changed, set())
                self.assertEqual(seen["w1:p1"]["seq"], 10)

    def test_inputs_are_not_mutated(self):
        seen = {}
        bh.changes_since_seen([agent("w1:p1", 10)], seen)
        bh.mark_seen(seen, [agent("w1:p1", 10)])
        self.assertEqual(seen, {})


class MarkSeen(unittest.TestCase):
    def test_records_current_state_and_time(self):
        seen = bh.mark_seen({}, [agent("w1:p1", 7)], now="2026-09-27T09:00:00")
        self.assertEqual(seen["w1:p1"],
                         {"terminal_id": "t1", "seq": 7, "at": "2026-09-27T09:00:00"})


class SeenFile(unittest.TestCase):
    def test_round_trip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "seen.json"
            with mock.patch.object(bh, "SEEN_FILE", path):
                self.assertEqual(bh.load_seen(), {})          # missing file
                data = bh.mark_seen({}, [agent("w1:p1", 3)], now="x")
                bh.save_seen(data)
                self.assertEqual(bh.load_seen(), data)
                path.write_text("{not json")
                self.assertEqual(bh.load_seen(), {})          # corrupt file
                path.write_text("[1, 2]")
                self.assertEqual(bh.load_seen(), {})          # wrong shape

    def test_save_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            bh.save_seen({"a": 1}, path)
            bh.save_seen({"a": 2}, path)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["seen.json"])


class UpdateSeen(unittest.TestCase):
    """Concurrent writers (Codex review of PR #1, finding 3)."""

    def test_concurrent_updates_keep_both_marks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"

            def mark(pane):
                def fn(seen):
                    time.sleep(0.05)   # widen the load -> save window
                    return bh.mark_seen(seen, [agent(pane, 1)]), None
                bh.update_seen(fn, path)

            threads = [threading.Thread(target=mark, args=(p,)) for p in ("w1:p1", "w1:p2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(bh.load_seen(path)), ["w1:p1", "w1:p2"])

    def test_returns_result_and_saves(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            out = bh.update_seen(lambda s: ({"x": {"seq": 1}}, "result"), path)
            self.assertEqual(out, "result")
            self.assertEqual(bh.load_seen(path), {"x": {"seq": 1}})

    def test_write_failure_still_returns_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.json"
            with mock.patch.object(bh, "save_seen", side_effect=OSError("disk full")), \
                 mock.patch("builtins.print") as printed:
                out = bh.update_seen(lambda s: (s, "still here"), path)
            self.assertEqual(out, "still here")
            self.assertIn("disk full", printed.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
