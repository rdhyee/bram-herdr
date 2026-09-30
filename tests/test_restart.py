"""Tests for `restart` and its helpers (standard library only).

Everything that could kill a process, start an agent or launch Bram is mocked,
so these tests never touch herdr, a real Bram or your real cache.

Run from the repo root:  python3 -m unittest discover -s tests
"""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bram_herdr as bh  # noqa: E402

OLD_PID, NEW_PID = 4242, 9999


def restart_args(project, **kw):
    base = dict(project=str(project), fresh=False, model=None, name=None,
                resume=False, expect_head=None, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def agent(pane="w1:p2", status="idle", kind="claude", name="proj-agent"):
    return {"pane_id": pane, "agent": kind, "agent_status": status, "name": name}


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class FindPaneId(unittest.TestCase):
    def test_finds_nested_id(self):
        self.assertEqual(bh.find_pane_id({"result": {"pane": {"pane_id": "w1:p9"}}}), "w1:p9")
        self.assertEqual(bh.find_pane_id([{"x": 1}, {"pane_id": "w1:p3"}]), "w1:p3")

    def test_none_when_absent_or_odd(self):
        for bad in (None, {}, [], "x", {"pane_id": 5}, {"a": [1, 2, {"b": None}]}):
            with self.subTest(bad=bad):
                self.assertIsNone(bh.find_pane_id(bad))


class SplitPane(unittest.TestCase):
    def test_returns_new_pane_and_passes_required_direction(self):
        reply = completed(json.dumps({"result": {"pane_id": "w1:p9"}}))
        with mock.patch.object(bh.subprocess, "run", return_value=reply) as run:
            self.assertEqual(bh.split_pane("w1:p2", "/proj"), "w1:p9")
        argv = run.call_args[0][0]
        self.assertIn("--direction", argv)   # without it herdr creates nothing
        self.assertIn("--cwd", argv)

    def test_fails_loudly_when_no_pane_was_created(self):
        for reply in (completed("usage: herdr pane split ..."),
                      completed(json.dumps({"result": {"pane_id": "w1:p2"}})),  # same pane
                      completed(json.dumps({"result": {"pane_id": "w1:p9"}}), returncode=1),
                      completed("")):
            with self.subTest(reply=reply.stdout):
                with mock.patch.object(bh.subprocess, "run", return_value=reply):
                    with self.assertRaises(SystemExit) as cm:
                        bh.split_pane("w1:p2", "/proj")
                self.assertIn("pane split failed", str(cm.exception))


class RestartWorld(unittest.TestCase):
    """A fake world: one project with a running Bram (pid OLD_PID) and one agent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        self.killed = []
        self.old_agent = agent()
        self.bram_cmd = "/src/bram/app/bram /proj"
        self.attached = "w1:p2"
        self.rec = {"pid": OLD_PID, "port": 50000}

        def pid_alive(pid):
            return pid == NEW_PID or (pid == OLD_PID and not self.killed)

        def port_record(_project):
            return {"pid": NEW_PID, "port": 50001} if self.killed else self.rec

        self.up = mock.Mock(return_value=(NEW_PID, "w1:p2"))
        self.prompts = []
        patches = [
            mock.patch.object(bh, "need"),
            mock.patch.object(bh, "check_bram_repo"),
            mock.patch.object(bh, "port_record", side_effect=port_record),
            mock.patch.object(bh, "pid_alive", side_effect=pid_alive),
            mock.patch.object(bh, "process_table",
                              side_effect=lambda: {OLD_PID: (1, self.bram_cmd)}),
            mock.patch.object(bh, "attached_pane", side_effect=lambda pid, t: self.attached),
            mock.patch.object(bh, "herdr_agents", side_effect=lambda: [self.old_agent]),
            mock.patch.object(bh, "app_info", return_value={"current": "0.7.2"}),
            mock.patch.object(bh, "up", self.up),
            mock.patch.object(bh, "herdr_prompt",
                              side_effect=lambda t, text: self.prompts.append((t, text)) or True),
            mock.patch.object(bh.os, "kill", side_effect=lambda pid, sig: self.killed.append((pid, sig))),
            mock.patch.object(bh.time, "sleep"),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def restart(self, **kw):
        bh.cmd_restart(restart_args(self.proj, **kw))

    def claim(self):
        (self.proj / "resources").mkdir(exist_ok=True)
        (self.proj / "resources" / ".inflight-claim.json").write_text("{}")


class RestartRefuses(RestartWorld):
    def assert_refuses(self, text, **kw):
        with self.assertRaises(SystemExit) as cm:
            self.restart(**kw)
        self.assertIn(text, str(cm.exception))
        self.assertEqual(self.killed, [])
        self.up.assert_not_called()

    def test_model_or_name_needs_fresh(self):
        self.assert_refuses("only apply with --fresh", model="m")
        self.assert_refuses("only apply with --fresh", name="n")

    def test_not_a_directory(self):
        with self.assertRaises(SystemExit) as cm:
            bh.cmd_restart(restart_args(self.proj / "nope"))
        self.assertIn("not a directory", str(cm.exception))

    def test_no_running_bram(self):
        self.rec = None
        self.assert_refuses("no running Bram")

    def test_pid_that_is_not_bram_is_never_killed(self):
        self.bram_cmd = "/usr/bin/python3 something.py"
        self.assert_refuses("is not a bram process")

    def test_no_attached_agent(self):
        self.attached = None
        self.assert_refuses("has no herdr agent attached")

    def test_attached_pane_unknown_to_herdr(self):
        self.attached = "w9:p9"
        self.assert_refuses("is not a herdr agent")

    def test_working_agent_stops_a_real_run(self):
        self.old_agent = agent(status="working")
        self.assert_refuses("is working")

    def test_inflight_claim_stops_a_real_run(self):
        self.claim()
        self.assert_refuses("Worklist claim is in flight")

    def test_head_mismatch_stops_a_real_run(self):
        with mock.patch.object(bh, "run", return_value="abc123\n"):
            self.assert_refuses("expected ffff", expect_head="ffff")


class RestartDryRun(RestartWorld):
    def test_changes_nothing_but_notes_failed_checks(self):
        self.old_agent = agent(status="working")
        self.claim()
        self.restart(dry_run=True)
        self.assertEqual(self.killed, [])
        self.assertTrue(self.up.call_args[1]["dry_run"])
        self.assertTrue(self.up.call_args[1]["bram_quit_first"])
        self.assertEqual(self.prompts, [])


class RestartRuns(RestartWorld):
    def test_same_agent_kills_only_that_pid_and_relaunches_on_same_pane(self):
        self.restart()
        self.assertEqual(self.killed, [(OLD_PID, 15)])
        self.assertEqual(self.up.call_args[1]["agent"]["pane_id"], "w1:p2")
        self.assertFalse(self.up.call_args[1]["dry_run"])
        self.assertEqual(self.prompts, [])

    def test_resume_prompt_sent_only_after_attach_confirmed(self):
        self.restart(resume=True)
        self.assertEqual(self.prompts, [("w1:p2", "/resume")])

    def test_unconfirmed_attach_skips_prompts_and_fails(self):
        self.up.return_value = (NEW_PID, None)
        with self.assertRaises(SystemExit) as cm:
            self.restart(resume=True)
        self.assertIn("attach not confirmed", str(cm.exception))
        self.assertEqual(self.prompts, [])

    def test_bram_that_will_not_exit_is_not_relaunched(self):
        # the process survives the kill: pid_alive stays True
        with mock.patch.object(bh, "pid_alive", return_value=True), \
             mock.patch.object(bh.time, "time", side_effect=[0, 0, 5, 20, 20, 20]):
            with self.assertRaises(SystemExit) as cm:
                self.restart()
        self.assertIn("did not exit", str(cm.exception))
        self.up.assert_not_called()

    def fresh(self, **kw):
        self.killed.clear()      # a fresh world for each call: Bram running again
        self.prompts.clear()
        replies =[completed(json.dumps({"result": {"pane_id": "w1:p9"}})), completed()]
        with mock.patch.object(bh.subprocess, "run", side_effect=replies) as run:
            self.restart(fresh=True, **kw)
        return run.call_args_list

    def test_fresh_passes_model_only_when_given(self):
        calls = self.fresh(model="claude-sonnet-5-5", name="demo")
        start = calls[1][0][0]
        self.assertEqual(start[:4], ["herdr", "agent", "start", "demo"])
        self.assertEqual(start[-3:], ["--", "--model", "claude-sonnet-5-5"])
        calls = self.fresh(name="demo2")
        self.assertNotIn("--model", calls[1][0][0])   # no hard-coded default model

    def test_fresh_uses_new_pane_keeps_old_and_renames(self):
        self.fresh(name="demo")
        self.assertEqual(self.up.call_args[1]["agent"]["pane_id"], "w1:p9")
        self.assertEqual(self.prompts, [("w1:p9", "/rename demo")])

    def test_fresh_default_name_is_project_and_date(self):
        self.fresh()
        name = self.prompts[0][1].split(" ", 1)[1]
        self.assertTrue(name.startswith(self.proj.name + "-"), name)

    def test_fresh_agent_start_failure_reports_bram_is_quit(self):
        replies = [completed(json.dumps({"result": {"pane_id": "w1:p9"}})),
                   completed("", returncode=1, stderr="boom")]
        with mock.patch.object(bh.subprocess, "run", side_effect=replies):
            with self.assertRaises(SystemExit) as cm:
                self.restart(fresh=True, name="demo")
        self.assertIn("Bram is quit", str(cm.exception))
        self.up.assert_not_called()


if __name__ == "__main__":
    unittest.main()
