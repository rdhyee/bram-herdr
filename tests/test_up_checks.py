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

    def test_executable_file_passes(self):
        with tempfile.TemporaryDirectory() as repo:
            bram = Path(repo) / "bram"
            bram.write_text("#!/bin/sh\n")
            bram.chmod(0o755)
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)):
                bh.check_bram_repo()  # no exception

    def test_non_executable_file_or_directory_is_rejected(self):
        # Codex review of PR #2, finding 5: existing is not the same as runnable.
        with tempfile.TemporaryDirectory() as repo:
            bram = Path(repo) / "bram"
            bram.write_text("")
            bram.chmod(0o644)
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)):
                with self.assertRaises(SystemExit) as cm:
                    bh.check_bram_repo()
            self.assertIn("not an executable file", str(cm.exception))
            bram.unlink()
            bram.mkdir()
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)):
                with self.assertRaises(SystemExit) as cm:
                    bh.check_bram_repo()
            self.assertIn("not an executable file", str(cm.exception))


class LaunchBramDryRun(unittest.TestCase):
    def test_dry_run_creates_no_log_folder(self):
        # Codex review of PR #2, finding 4: a dry run must not create anything.
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as cache:
            bram = Path(repo) / "bram"
            bram.write_text("#!/bin/sh\n")
            bram.chmod(0o755)
            log_dir = Path(cache) / "not-yet"
            with mock.patch.object(bh, "BRAM_REPO", Path(repo)), \
                 mock.patch.object(bh, "LOG_DIR", log_dir), \
                 mock.patch("builtins.print"):
                self.assertIsNone(bh.launch_bram(Path("/some/proj"), dry=True))
            self.assertFalse(log_dir.exists())


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


class UpAfterLaunch(unittest.TestCase):
    def test_failure_after_the_launch_says_bram_is_running(self):
        # Codex round 2, finding 3: up() raises LaunchedError once Bram has started
        with tempfile.TemporaryDirectory() as proj:
            agent = {"pane_id": "w1:p2", "agent": "claude", "agent_status": "idle"}
            with mock.patch.object(bh, "check_bram_repo"), \
                 mock.patch.object(bh, "port_record", return_value=None), \
                 mock.patch.object(bh, "preflight", return_value={}), \
                 mock.patch.object(bh, "merge_bram_json"), \
                 mock.patch.object(bh, "warn_existing_attaches"), \
                 mock.patch.object(bh, "launch_bram", return_value=31337), \
                 mock.patch.object(bh.shutil, "which", return_value=None), \
                 mock.patch.object(bh, "wait_for_autostart", side_effect=OSError("disk")), \
                 mock.patch("builtins.print"):
                with self.assertRaises(bh.LaunchedError) as cm:
                    bh.up(proj, agent=agent)
        self.assertIn("pid 31337", str(cm.exception))
        self.assertIn("OSError: disk", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
