"""Tests for add_excludes / BRAM_EXCLUDES (standard library; uses git and temp folders).

Run from the repo root:  python3 -m unittest discover -s tests
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bram_herdr as bh  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class AddExcludes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        self.exclude = self.proj / ".git" / "info" / "exclude"

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        return self.exclude.read_text().splitlines()

    def test_every_entry_is_written_including_bram_reference(self):
        bh.add_excludes(self.proj, dry=False)
        for entry in bh.BRAM_EXCLUDES:
            self.assertIn(entry, self.lines())
        self.assertIn(".claude/bram-reference/", self.lines())

    def test_settings_json_is_not_excluded(self):
        self.assertNotIn(".claude/settings.json", bh.BRAM_EXCLUDES)

    def test_second_call_adds_nothing(self):
        bh.add_excludes(self.proj, dry=False)
        before = self.exclude.read_text()
        bh.add_excludes(self.proj, dry=False)
        self.assertEqual(self.exclude.read_text(), before)

    def test_existing_lines_are_kept_and_only_missing_entries_added(self):
        self.exclude.write_text("*.log\nresources/\n")
        bh.add_excludes(self.proj, dry=False)
        lines = self.lines()
        self.assertIn("*.log", lines)
        self.assertEqual(lines.count("resources/"), 1)
        self.assertIn(".claude/bram-reference/", lines)

    def test_dry_run_writes_nothing(self):
        before = self.exclude.read_text() if self.exclude.exists() else None
        bh.add_excludes(self.proj, dry=True)
        after = self.exclude.read_text() if self.exclude.exists() else None
        self.assertEqual(before, after)

    def test_git_actually_ignores_bram_reference(self):
        bh.add_excludes(self.proj, dry=False)
        ref = self.proj / ".claude" / "bram-reference"
        ref.mkdir(parents=True)
        (ref / "diagnostics.md").write_text("x")
        out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=self.proj,
                             capture_output=True, text=True).stdout
        self.assertNotIn("bram-reference", out)


if __name__ == "__main__":
    unittest.main()
