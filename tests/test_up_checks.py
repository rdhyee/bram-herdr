"""Tests that `up` checks BRAM_REPO before writing anything (standard library only).

Run from the repo root:  python3 -m unittest discover -s tests
"""
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bram_herdr as bh  # noqa: E402


def up_args(project, **kw):
    base = dict(project=str(project), pane=None, kind=None, exclude=False,
                dry_run=False, no_auto_attach=True)
    base.update(kw)
    return argparse.Namespace(**base)


class CheckBramRepo(unittest.TestCase):
    def test_missing_symlink_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as repo, \
             mock.patch.object(bh, "BRAM_REPO", Path(repo)):
            with self.assertRaises(SystemExit) as cm:
                bh.check_bram_repo()
        self.assertIn("Set BRAM_REPO", str(cm.exception))
        self.assertIn(repo, str(cm.exception))

    def test_present_symlink_passes(self):
        with tempfile.TemporaryDirectory() as repo:
            (Path(repo) / "bram").write_text("")
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)):
                bh.check_bram_repo()  # no exception


class UpWritesNothingWhenRepoIsWrong(unittest.TestCase):
    def test_no_files_written_and_herdr_not_consulted(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as proj:
            (Path(proj) / "CLAUDE.md").write_text("mine\n")
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)), \
                 mock.patch.object(bh, "pick_agent") as pick:
                for dry in (False, True):
                    with self.subTest(dry_run=dry):
                        with self.assertRaises(SystemExit) as cm:
                            bh.cmd_up(up_args(proj, dry_run=dry))
                        self.assertIn("Set BRAM_REPO", str(cm.exception))
                pick.assert_not_called()
            self.assertEqual(sorted(p.name for p in Path(proj).iterdir()), ["CLAUDE.md"])


if __name__ == "__main__":
    unittest.main()
