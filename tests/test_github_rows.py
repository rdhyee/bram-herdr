"""Tests for the GitHub rows in `status` (standard library only; no network).

Run from the repo root:  python3 -m unittest discover -s tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bram_herdr as bh  # noqa: E402


def node(repo, n, title="t", updated="2026-09-20T10:00:00Z"):
    return {"number": n, "title": title, "url": f"https://github.com/{repo}/issues/{n}",
            "updatedAt": updated, "repository": {"nameWithOwner": repo}}


def response(review=(), assigned=(), assigned_count=None):
    return {"data": {
        "review": {"issueCount": len(review), "nodes": list(review)},
        "assigned": {"issueCount": len(assigned) if assigned_count is None else assigned_count,
                     "nodes": list(assigned)}}}


class ParseGithub(unittest.TestCase):
    def test_both_lists_and_counts(self):
        data = response(review=[node("judell/bram", 9, "Review me")],
                        assigned=[node("a/b", 1), node("c/d", 2)], assigned_count=69)
        out = bh.parse_github(data)
        self.assertEqual(out["review"]["count"], 1)
        self.assertEqual(out["review"]["rows"][0],
                         {"repo": "judell/bram", "number": 9, "title": "Review me",
                          "url": "https://github.com/judell/bram/issues/9",
                          "updated": "2026-09-20"})
        self.assertEqual(out["assigned"]["count"], 69)
        self.assertEqual(len(out["assigned"]["rows"]), 2)

    def test_empty_and_malformed(self):
        self.assertEqual(bh.parse_github({}),
                         {"review": {"count": 0, "rows": []},
                          "assigned": {"count": 0, "rows": []}})
        data = {"data": {"review": {"issueCount": 1, "nodes": [None, {"title": "no url"}]}}}
        self.assertEqual(bh.parse_github(data)["review"]["rows"], [])


class GithubLines(unittest.TestCase):
    def test_assigned_is_capped_with_more_line(self):
        data = response(assigned=[node("a/b", i) for i in range(20)], assigned_count=69)
        lines = bh.github_lines(bh.parse_github(data))
        self.assertIn("review requested (0): none", lines[0])
        self.assertIn("assigned to you (69), 5 most recently updated:", lines[1])
        self.assertEqual(len([l for l in lines if l.startswith("    a/b#")]), bh.ASSIGNED_SHOWN)
        self.assertIn("… and 64 more: https://github.com/issues/assigned", lines[-1])

    def test_all_review_requests_shown_and_no_more_line_when_few(self):
        data = response(review=[node("x/y", i) for i in range(7)], assigned=[node("a/b", 1)])
        lines = bh.github_lines(bh.parse_github(data))
        self.assertEqual(len([l for l in lines if l.startswith("    x/y#")]), 7)
        self.assertFalse(any("more:" in l for l in lines))


class GithubRows(unittest.TestCase):
    """github_rows(): cache, one gh call, and failures that never raise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "github.json"
        self.patches = [mock.patch.object(bh, "GITHUB_CACHE", self.cache),
                        mock.patch.object(bh.shutil, "which", return_value="/usr/bin/gh")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def ok(self, data):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr="")

    def write_cache(self, fetched, data):
        self.cache.write_text(json.dumps({"fetched": fetched, "data": data}))

    def test_fetches_once_and_caches(self):
        data = response(review=[node("a/b", 1)])
        with mock.patch.object(bh.subprocess, "run", return_value=self.ok(data)) as run:
            out, note = bh.github_rows(now=1000)
        self.assertIsNone(note)
        self.assertEqual(out["review"]["count"], 1)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["gh", "api", "graphql"])
        self.assertIn("review-requested:@me", argv[-1])
        self.assertIn("assignee:@me", argv[-1])
        self.assertEqual(run.call_args[1]["timeout"], 10)
        self.assertEqual(json.loads(self.cache.read_text())["fetched"], 1000)

    def test_fresh_cache_skips_gh(self):
        self.write_cache(1000, response(assigned=[node("a/b", 1)]))
        with mock.patch.object(bh.subprocess, "run") as run:
            out, note = bh.github_rows(now=1000 + bh.GITHUB_TTL - 1)
        run.assert_not_called()
        self.assertIsNone(note)
        self.assertEqual(out["assigned"]["count"], 1)

    def test_stale_cache_calls_gh(self):
        self.write_cache(1000, response())
        fresh = response(review=[node("n/m", 5)])
        with mock.patch.object(bh.subprocess, "run", return_value=self.ok(fresh)) as run:
            out, _ = bh.github_rows(now=1000 + bh.GITHUB_TTL + 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(out["review"]["count"], 1)

    def test_refresh_ignores_fresh_cache(self):
        self.write_cache(1000, response())
        with mock.patch.object(bh.subprocess, "run", return_value=self.ok(response())) as run:
            bh.github_rows(refresh=True, now=1001)
        self.assertEqual(run.call_count, 1)

    def test_missing_gh_skips_without_crashing(self):
        with mock.patch.object(bh.shutil, "which", return_value=None), \
             mock.patch.object(bh.subprocess, "run") as run:
            out, note = bh.github_rows(now=1000)
        run.assert_not_called()
        self.assertIsNone(out)
        self.assertIn("GitHub rows skipped: gh not found", note)

    def test_failure_falls_back_to_stale_cache_with_age(self):
        self.write_cache(1000, response(assigned=[node("a/b", 1)]))
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502\nmore")
        with mock.patch.object(bh.subprocess, "run", return_value=failed):
            out, note = bh.github_rows(now=1000 + 600)
        self.assertEqual(out["assigned"]["count"], 1)
        self.assertEqual(note, "GitHub: showing rows cached 10 min ago (HTTP 502)")

    def test_timeout_without_cache_is_a_skip_note(self):
        boom = subprocess.TimeoutExpired(cmd="gh", timeout=10)
        with mock.patch.object(bh.subprocess, "run", side_effect=boom):
            out, note = bh.github_rows(now=1000)
        self.assertIsNone(out)
        self.assertEqual(note, "GitHub rows skipped: gh timed out after 10 s")

    def test_graphql_errors_without_data(self):
        bad = self.ok({"errors": [{"message": "nope"}]})
        with mock.patch.object(bh.subprocess, "run", return_value=bad):
            out, note = bh.github_rows(now=1000)
        self.assertIsNone(out)
        self.assertIn("unexpected answer from GitHub", note)
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
