#!/usr/bin/env python3
"""bram_herdr.py — run projects under Bram with their agents owned by herdr.

See README.md, and skill/SKILL.md for the Claude Code skill that drives it.

Requires Bram built from judell/bram main at or after 1aa63a1 (PR #394,
"Do not start an agent"), launched via the ./bram symlink in its checkout.
Point BRAM_REPO at that checkout (default: ~/C/src/bram). Also needs herdr
(https://herdr.dev) and Python 3.7+. macOS: `front` uses osascript and `up`
copies to the clipboard with pbcopy. Launch logs go to ~/.cache/bram-herdr/.

Commands:
  status                      Which Bram is running where, and which herdr
                              agent each one has attached.
  up PROJECT [--pane ID]      Preflight, set "Do not start an agent", launch
     [--kind claude|codex]    Bram, and attach the herdr agent automatically
     [--exclude] [--dry-run]  (PROMPT_COMMAND in Bram's inherited env; see
     [--no-auto-attach]       launch_bram). Clears leftover attaches first.
  front PROJECT               Bring that project's Bram window to the front
                              (PROJECT: path or folder name, e.g. myproject).
  say PROJECT TEXT [--wait]   Prompt the herdr agent attached to PROJECT's
                              Bram (or --pane ID).

Standard library only. Writes only PROJECT/.bram.json (merged, other keys kept),
PROJECT/.bram-preflight/ (backups), and, with --exclude, .git/info/exclude.
It never edits CLAUDE.md or AGENTS.md; it backs them up and checks them.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BRAM_REPO = Path(os.environ.get("BRAM_REPO", "~/C/src/bram")).expanduser()
LOG_DIR = Path("~/.cache/bram-herdr").expanduser()
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
# Bram-written paths that don't belong in a project's history (skill step 2).
BRAM_EXCLUDES = (
    ".bram.json",
    "resources/",
    ".claude/bram-conventions.md",
    ".claude/skills/loose-ends/",
    ".bram-preflight/",
)
# Launch hygiene from bram docs/developing-bram.md (#339): an instance started
# from inside a Claude Code session must not inherit these, or its own agent
# believes it is a child session and stops saving transcripts.
SCRUB_ENV = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
)


# ---------- helpers ----------

def need(cmd, why):
    """Exit with a clear message, not a traceback, when a command is missing."""
    if not shutil.which(cmd):
        raise SystemExit(f"{cmd} not found on PATH: {why}")


def run(cmd, cwd=None, check=True):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f"{cmd[0]} not found on PATH (needed for: {' '.join(cmd)})")
    if check and r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def same_dir(a, b):
    """herdr can report /Volumes/<disk>/MacHD/Users/... for ~/..., so compare
    by inode rather than by string."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def herdr_agents():
    need("herdr", "install herdr, see https://herdr.dev")
    out = run(["herdr", "agent", "list"])
    return json.loads(out)["result"]["agents"]


def agents_in(project):
    return [a for a in herdr_agents() if same_dir(a.get("cwd", ""), project)]


def agent_label(a):
    name = a.get("name") or a.get("terminal_title_stripped") or ""
    return f"{a['pane_id']}  {a['agent']:<6}  {a.get('agent_status', '?'):<8}  {name}"


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def process_table():
    """pid -> (ppid, command)."""
    table = {}
    for line in run(["ps", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def attached_pane(bram_pid, table):
    """Find a `herdr agent attach <pane>` process descended from bram_pid."""
    for pid, (_, cmd) in table.items():
        if not cmd.startswith("herdr agent attach"):
            continue
        p, hops = pid, 0
        while p in table and hops < 12:
            p = table[p][0]
            hops += 1
            if p == bram_pid:
                return cmd.split()[3] if len(cmd.split()) > 3 else "?"
    return None


def process_cwd(pid):
    out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], check=False)
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def running_bram_projects(table):
    """Project dirs of every running Bram, found from `ps` rather than from
    herdr, so a Bram on a folder with no herdr agent still shows up. Bram's
    CLI is `bram [PROJECT_DIR]` (default: its cwd)."""
    found = []
    for pid, (_, cmd) in table.items():
        argv = cmd.split()
        if not argv or os.path.basename(argv[0]) != "bram":
            continue  # skips bram-guard, bram-*.sh, etc.
        cwd = process_cwd(pid)
        arg = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "."
        path = arg if os.path.isabs(arg) else os.path.join(cwd or "", arg)
        if os.path.isdir(path):
            found.append(os.path.normpath(path))
    return found


def port_record(project):
    f = Path(project) / "resources" / ".bram-port.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def app_info(port):
    try:
        out = run(["curl", "-4", "-sS", "--max-time", "2",
                   f"http://127.0.0.1:{port}/__app-info"], check=False)
        return json.loads(out) if out else {}
    except ValueError:
        return {}


# ---------- status ----------

def cmd_status(_args):
    table = process_table()
    # Candidate projects: every folder a herdr agent works in, plus the bram repo.
    agents = herdr_agents()
    candidates = {}
    for a in agents:
        cwd = a.get("cwd")
        if cwd:
            candidates.setdefault(os.path.realpath(cwd), cwd)
    candidates.setdefault(os.path.realpath(BRAM_REPO), str(BRAM_REPO))
    ps_found = set()
    for path in running_bram_projects(table):
        candidates.setdefault(os.path.realpath(path), path)
        ps_found.add(os.path.realpath(path))

    rows = []
    for real, shown in sorted(candidates.items()):
        rec = port_record(shown)
        running = bool(rec and pid_alive(rec.get("pid")))
        here = [a for a in agents if same_dir(a.get("cwd", ""), shown)]
        if not running and real in ps_found:
            # A bram process exists, but no live port file: starting up,
            # refused (e.g. a $HOME launch), or wedged.
            rows.append((shown, None, rec, "", None, here))
            continue
        if not running and not here:
            continue
        pane = attached_pane(int(rec["pid"]), table) if running else None
        version = app_info(rec["port"]).get("current", "?") if running else ""
        rows.append((shown, running, rec, version, pane, here))

    for shown, running, rec, version, pane, here in rows:
        home = str(Path.home())
        # herdr may report /Volumes/<disk>/MacHD/Users/... for a ~ path.
        name = "~" + shown[shown.find(home) + len(home):] if home in shown else shown
        if running is None:
            print(f"{name}\n  a bram process is running here, but it has no live port file "
                  "(starting up, refused, or stuck)")
        elif running:
            print(f"{name}\n  Bram {version}  pid {rec['pid']}  port {rec['port']}  "
                  f"attached: {pane or 'nothing'}")
        else:
            print(f"{name}\n  Bram not running")
        for a in here:
            mark = "*" if pane and a["pane_id"] == pane else " "
            print(f"   {mark} {agent_label(a)}")
    print("\n* = attached in that project's Bram terminal")


# ---------- up ----------

def preflight(project, stamp, dry):
    """Back up instruction files that git can't restore (untracked/modified)."""
    backups = {}
    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=project,
                            capture_output=True, text=True).stdout.strip() == "true"
    dirty = set()
    if in_git:
        porcelain = run(["git", "status", "--porcelain", "--", *INSTRUCTION_FILES],
                        cwd=project, check=False)
        dirty = {line[3:].strip() for line in porcelain.splitlines()}
    for name in INSTRUCTION_FILES:
        f = project / name
        if not f.exists():
            continue
        backups[name] = f.read_bytes()
        # Not a git repo -> git can't restore anything, so always back up.
        if not in_git or name in dirty:
            why = "not in git" if not in_git else "has uncommitted changes"
            dest = project / ".bram-preflight" / stamp / name
            print(f"  preflight: {name} {why} -> backup {dest.relative_to(project)}")
            if not dry:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        else:
            print(f"  preflight: {name} is committed (git can restore it)")
    return backups


BRAM_BLOCK = re.compile(rb"<!-- bram:start -->.*?<!-- bram:end -->\n?", re.S)


def outside_bram_block(data):
    """The user's own text: everything outside Bram's managed block, which
    Setup legitimately rewrites when the conventions change (a refreshed
    block in a project's AGENTS.md once raised a false alarm)."""
    return BRAM_BLOCK.sub(b"", data).strip()


def check_instruction_files(project, before):
    """Setup may add or refresh its marker block, but must leave the user's
    own text intact (judell/bram#396)."""
    ok = True
    for name, old in before.items():
        new = (project / name).read_bytes() if (project / name).exists() else b""
        if old and outside_bram_block(old) != outside_bram_block(new):
            ok = False
            print(f"  !! {name}: original content is NOT preserved "
                  f"({len(old)} -> {len(new)} bytes). Restore it from git or "
                  f".bram-preflight/, and report on judell/bram#396.")
        else:
            print(f"  ok {name}: original content preserved ({len(old)} -> {len(new)} bytes)")
    return ok


def merge_bram_json(project, kind, dry):
    f = project / ".bram.json"
    cfg = {}
    if f.exists():
        cfg = json.loads(f.read_text())
    shell = cfg.setdefault("shell", {})
    changed = shell.get("startupPolicy") != "none" or shell.get("agent") != kind
    shell["startupPolicy"] = "none"
    shell["agent"] = kind
    print(f"  .bram.json: shell.startupPolicy=none, shell.agent={kind}"
          f"{'' if changed else ' (already set)'}")
    if changed and not dry:
        f.write_text(json.dumps(cfg, indent=2) + "\n")


def add_excludes(project, dry):
    gitdir = run(["git", "rev-parse", "--git-dir"], cwd=project, check=False).strip()
    if not gitdir:
        print("  exclude: not a git repo, skipped")
        return
    ex = (project / gitdir / "info" / "exclude").resolve()
    have = ex.read_text().splitlines() if ex.exists() else []
    add = [p for p in BRAM_EXCLUDES if p not in have]
    print(f"  exclude: adding {add or 'nothing (already present)'} to .git/info/exclude")
    if add and not dry:
        ex.parent.mkdir(parents=True, exist_ok=True)
        with ex.open("a") as fh:
            fh.write("\n# Bram (bram_herdr.py)\n" + "\n".join(add) + "\n")


def pick_agent(project, pane, kind):
    here = agents_in(project)
    if pane:
        match = [a for a in herdr_agents() if a["pane_id"] == pane]
        if not match:
            raise SystemExit(f"no herdr agent in pane {pane}")
        return match[0]
    if kind:
        here = [a for a in here if a["agent"] == kind]
    if len(here) == 1:
        return here[0]
    if not here:
        raise SystemExit(
            f"no herdr agent works in {project}.\nStart one, e.g.:\n"
            f"  herdr pane split <pane> --direction down --cwd {project}\n"
            f"  herdr agent start <name> --kind claude --pane <new-pane>\n"
            "then rerun with --pane <new-pane>.")
    print("several herdr agents work here; rerun with --pane <id>:")
    for a in here:
        print("  " + agent_label(a))
    raise SystemExit(2)


def launch_bram(project, dry, attach=None):
    if not (BRAM_REPO / "bram").exists():
        raise SystemExit(f"no ./bram symlink in {BRAM_REPO}. Set BRAM_REPO to your "
                         "judell/bram checkout (built from main at or after 1aa63a1).")
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    if attach:
        # Auto-attach with no Bram change. Bram's terminal is
        # bash (--noprofile --rcfile app/shell/claude-code-shellrc -i) and it
        # inherits Bram's environment. Bash imports PROMPT_COMMAND from the
        # environment and runs it just before the first prompt, so this types
        # nothing into the PTY: the shell itself attaches once. `unset` first
        # so detaching returns to a plain prompt, and so the attach process
        # (and anything after) doesn't inherit it.
        env["PROMPT_COMMAND"] = f"unset PROMPT_COMMAND; {attach}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{project.name}.log"
    print(f"  launch: (cd {BRAM_REPO} && ./bram {project})  log: {log}")
    if dry:
        return None
    fh = log.open("ab")
    proc = subprocess.Popen(["./bram", str(project)], cwd=BRAM_REPO, env=env,
                            stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    return proc.pid


def clear_orphan_attaches(pane):
    """Quitting a Bram leaves its `herdr agent attach <pane>` running, reparented
    to launchd (ppid 1), still holding the pane, so the next Bram's auto-attach
    silently fails (seen in practice). Kill only those
    orphans; an attach under a live Bram is real and left alone."""
    for pid, (ppid, cmd) in process_table().items():
        argv = cmd.split()
        if argv[:3] == ["herdr", "agent", "attach"] and argv[3:4] == [pane]:
            if ppid == 1:
                print(f"  cleanup: stopping leftover attach to {pane} (pid {pid}) from a quit Bram")
                os.kill(pid, 15)
            else:
                print(f"  !! {pane} is already attached in a running terminal (pid {pid}); "
                      "the new Bram's attach may fail")
    time.sleep(0.5)


def wait_for_autostart(project, since_ms, timeout=45):
    trace = project / "resources" / "bram-traces" / "bram-trace.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if trace.exists():
            for line in reversed(trace.read_text(errors="replace").splitlines()):
                if "[agent-switch] op=autostart" in line:
                    ts = line[1:25]
                    try:
                        when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        break
                    if when.timestamp() * 1000 >= since_ms - 2000:
                        return line.split("] ", 2)[-1]
                    break
        time.sleep(1)
    return None


def cmd_up(args):
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"not a directory: {project}")
    rec = port_record(project)
    if rec and pid_alive(rec.get("pid")):
        raise SystemExit(f"Bram is already running for {project} (pid {rec['pid']}). "
                         "Quit it first, or use `status`.")
    agent = pick_agent(project, args.pane, args.kind)
    kind = agent["agent"]
    if kind not in ("claude", "codex"):
        raise SystemExit(f"pane {agent['pane_id']} runs {kind}; Bram supports claude/codex")
    print(f"project: {project}\nagent:   {agent_label(agent)}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    before = preflight(project, stamp, args.dry_run)
    merge_bram_json(project, kind, args.dry_run)
    if args.exclude:
        add_excludes(project, args.dry_run)

    # Plain `herdr` (PATH is inherited): attached_pane() matches on the
    # command line starting with "herdr agent attach".
    attach = f"herdr agent attach {agent['pane_id']}"
    if not args.dry_run:
        clear_orphan_attaches(agent["pane_id"])
    auto = not args.no_auto_attach
    since = int(time.time() * 1000)
    pid = launch_bram(project, args.dry_run, attach=attach if auto else None)
    if args.dry_run:
        print(f"  (dry run) would {'auto-attach' if auto else 'copy to clipboard'}: {attach}")
        return
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=attach, text=True)
    else:
        print("  note: pbcopy not found (not macOS?); the attach command is not on the clipboard")
    print(f"  Bram pid {pid}. {'Auto-attaching' if auto else 'Clipboard'}: {attach}")

    line = wait_for_autostart(project, since)
    if not line:
        print("  !! no autostart line in the trace within 45 s. Check the Bram window.")
    elif "policy=none" in line:
        print(f"  ok trace: {line}")
    else:
        print(f"  !! Bram launched an agent itself: {line}")
    check_instruction_files(project, before)
    if auto:
        got = None
        deadline = time.time() + 20
        while time.time() < deadline and not got:
            got = attached_pane(pid, process_table())
            if not got:
                time.sleep(1)
        if got:
            print(f"  ok attached: {got} (no typing needed)")
            return
        print("  !! auto-attach not seen within 20 s. Fall back to typing it:")
    else:
        print("\nNext: click into Bram's terminal and run (it's also on the clipboard):")
    print(f"\n    {attach}\n")


# ---------- front ----------

def running_brams():
    """[(project_path, pid)] for every running Bram, from `ps`."""
    table = process_table()
    out = []
    for pid, (_, cmd) in table.items():
        argv = cmd.split()
        if argv and os.path.basename(argv[0]) == "bram":
            for path in running_bram_projects({pid: table[pid]}):
                out.append((path, pid))
    return out


def cmd_front(args):
    """Bring a project's Bram window to the front. Each Bram is
    its own process with one window, so raising the process raises that
    Bram. PROJECT may be a path or just the folder name (`myproject`)."""
    need("osascript", "`front` needs macOS")
    brams = running_brams()
    want = Path(args.project).expanduser()
    match = [(p, pid) for p, pid in brams
             if (want.exists() and same_dir(p, want)) or os.path.basename(p) == args.project]
    if not match:
        names = ", ".join(sorted(os.path.basename(p) for p, _ in brams)) or "none"
        raise SystemExit(f"no running Bram for {args.project!r} (running: {names})")
    path, pid = match[0]
    script = (f'tell application "System Events" to set frontmost of '
              f'(first process whose unix id is {pid}) to true')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"osascript failed (macOS may need Automation/Accessibility "
                         f"permission for this terminal): {r.stderr.strip()}")
    print(f"front: {path} (pid {pid})")


# ---------- say ----------

def cmd_say(args):
    pane = args.pane
    if not pane:
        project = Path(args.project).expanduser().resolve()
        rec = port_record(project)
        if not (rec and pid_alive(rec.get("pid"))):
            raise SystemExit(f"no running Bram for {project}; use --pane")
        pane = attached_pane(int(rec["pid"]), process_table())
        if not pane:
            raise SystemExit("that Bram has no herdr agent attached; use --pane")
    need("herdr", "install herdr, see https://herdr.dev")
    cmd = ["herdr", "agent", "prompt", pane, args.text]
    if args.wait:
        cmd += ["--wait", "--timeout", str(args.timeout * 1000)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        result = json.loads(r.stdout).get("result", {})
        print(f"{pane}: {result.get('type', r.stdout.strip())}")
    except ValueError:
        print(r.stdout or r.stderr)
    sys.exit(r.returncode)


def main():
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"environment:\n"
               f"  BRAM_REPO   judell/bram checkout, built from main at or after 1aa63a1\n"
               f"              and launched via its ./bram symlink (now: {BRAM_REPO})\n"
               f"logs:\n"
               f"  {LOG_DIR}/<project>.log  (Bram's output from `up`)\n\n"
               f"See README.md for requirements and how auto-attach works.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("status", help="which Bram is running where, and which herdr agent "
                                  "each has attached (read-only)").set_defaults(fn=cmd_status)
    u = sub.add_parser("up", help="preflight, set 'Do not start an agent', launch Bram, "
                                  "and attach the herdr agent")
    u.add_argument("project", help="project directory to run Bram on")
    u.add_argument("--pane", help="herdr pane id of the agent to attach (e.g. w1:p2); "
                                  "needed when several agents work in the project")
    u.add_argument("--kind", choices=["claude", "codex"],
                   help="only consider herdr agents of this kind")
    u.add_argument("--exclude", action="store_true",
                   help="add Bram's files to .git/info/exclude (local-only)")
    u.add_argument("--dry-run", action="store_true",
                   help="print what would happen; write and launch nothing")
    u.add_argument("--no-auto-attach", action="store_true",
                   help="don't attach automatically; just put the command on the clipboard")
    u.set_defaults(fn=cmd_up)
    f = sub.add_parser("front", help="bring a project's Bram window to the front (macOS)")
    f.add_argument("project", help="path, or just the folder name (e.g. myproject)")
    f.set_defaults(fn=cmd_front)
    s = sub.add_parser("say", help="prompt the herdr agent attached to a project's Bram")
    s.add_argument("project", nargs="?", default=".",
                   help="project whose Bram's attached agent gets the text (default: .)")
    s.add_argument("text", help="the prompt to send")
    s.add_argument("--pane", help="send to this herdr pane instead of looking it up")
    s.add_argument("--wait", action="store_true",
                   help="wait for herdr to report the agent's next state "
                        "(passes --wait to `herdr agent prompt`)")
    s.add_argument("--timeout", type=int, default=600, help="seconds, with --wait")
    s.set_defaults(fn=cmd_say)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
