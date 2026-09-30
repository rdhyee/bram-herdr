#!/usr/bin/env python3
"""bram_herdr.py — run projects under Bram with their agents owned by herdr.

See README.md, and skill/SKILL.md for the Claude Code skill that drives it.

Requires Bram built from judell/bram main at or after 1aa63a1 (PR #394,
"Do not start an agent"), launched via the ./bram symlink in its checkout.
Point BRAM_REPO at that checkout (default: ~/C/src/bram). Also needs herdr
(https://herdr.dev) and Python 3.7+. macOS: `front` uses osascript and `up`
copies to the clipboard with pbcopy. Launch logs go to ~/.cache/bram-herdr/.

Commands:
  status [--refresh]          Which Bram is running where, which herdr agent
         [--no-github]        each one has attached, and a "Needs you" list:
                              agents changed since you looked, plus GitHub
                              review requests and issues assigned to you.
  seen [PANE ...] [--all]     Mark herdr agents as looked at.
  up PROJECT [--pane ID]      Preflight, set "Do not start an agent", launch
     [--kind claude|codex]    Bram, and attach the herdr agent automatically
     [--exclude] [--dry-run]  (PROMPT_COMMAND in Bram's inherited env; see
     [--no-auto-attach]       launch_bram). Warns if the pane is already held.
  front PROJECT               Bring that project's Bram window to the front
                              (PROJECT: path or folder name, e.g. myproject).
  say PROJECT TEXT [--wait]   Prompt the herdr agent attached to PROJECT's
                              Bram (or --pane ID).

Standard library only. Writes only PROJECT/.bram.json (merged, other keys kept),
PROJECT/.bram-preflight/ (backups), with --exclude .git/info/exclude, and its
own cache in ~/.cache/bram-herdr/ (seen.json, github.json).
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
import tempfile
import time
from contextlib import contextmanager

try:
    import fcntl  # macOS and Linux; the seen-file lock is skipped without it
except ImportError:  # pragma: no cover
    fcntl = None
from pathlib import Path

BRAM_REPO = Path(os.environ.get("BRAM_REPO", "~/C/src/bram")).expanduser()
LOG_DIR = Path("~/.cache/bram-herdr").expanduser()
SEEN_FILE = LOG_DIR / "seen.json"      # pane -> what it looked like when you last looked
GITHUB_CACHE = LOG_DIR / "github.json"
GITHUB_TTL = 300                        # seconds a GitHub answer is reused
GITHUB_FETCHED = 20                     # rows fetched per search; more -> "… and N more"
ASSIGNED_SHOWN = 5                      # assigned-to-you rows shown before "… and N more"
WAITING = ("idle", "done", "blocked")   # herdr states where an agent is waiting on you
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
# Bram-written paths that don't belong in a project's history (skill step 2).
# .claude/settings.json is deliberately absent: Setup edits it, but a project
# may have its own committed one, and excluding it would only get in the way.
BRAM_EXCLUDES = (
    ".bram.json",
    "resources/",
    ".claude/bram-conventions.md",
    ".claude/bram-reference/",
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


# ---------- changed since you looked ----------
#
# herdr stamps each agent with `state_change_seq`, a counter shared by all
# agents that records its latest state change, but it has no "last viewed"
# time. So we record, per pane, the seq we saw when you last looked, and flag
# the pane when herdr's number moves past it. "Looked" = `front`, `seen`, or
# herdr reporting the pane focused when `status` runs.

def _seq(agent):
    try:
        return int(agent.get("state_change_seq"))
    except (TypeError, ValueError):
        return None


def _seen_record(agent, now=None):
    return {"terminal_id": agent.get("terminal_id"),
            "seq": _seq(agent),
            "at": now or dt.datetime.now().isoformat(timespec="seconds")}


def load_seen(path=None):
    try:
        data = json.loads(Path(path or SEEN_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_seen(seen, path=None):
    """Atomic write through a unique temp file, so concurrent writers never
    share (or delete) each other's temp file."""
    path = Path(path or SEEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(seen, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _seen_lock(path=None):
    """Exclusive lock around a load -> update -> save of the seen file, so two
    runs at once (say `status` and `front`) can't lose each other's marks."""
    path = Path(path or SEEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def update_seen(fn, path=None):
    """Run fn(seen) -> (new_seen, result) under the lock, save, return result.
    If the cache can't be locked or written, still return fn's result (with a
    one-line note) rather than failing the command."""
    try:
        with _seen_lock(path):
            new, result = fn(load_seen(path))
            save_seen(new, path)
            return result
    except OSError as e:
        print(f"(could not update {path or SEEN_FILE}: {e})")
        return fn(load_seen(path))[1]


def _record_seq(rec):
    """The seq in a saved record, or None if the record is unusable."""
    if not isinstance(rec, dict):
        return None
    seq = rec.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def changes_since_seen(agents, seen):
    """Return (pane ids changed since last looked, updated seen dict).

    A pane with no usable record, or whose terminal was replaced, is recorded
    as a baseline and not flagged; otherwise the first run (or a hand-edited
    or corrupt cache) would flag everything, or crash."""
    seen = dict(seen)
    changed = set()
    for a in agents:
        pane, seq = a.get("pane_id"), _seq(a)
        if not pane or seq is None:
            continue
        rec = seen.get(pane)
        old = _record_seq(rec)
        if old is None or rec.get("terminal_id") != a.get("terminal_id"):
            seen[pane] = _seen_record(a)
        elif seq > old:
            changed.add(pane)
    return changed, seen


def mark_seen(seen, agents, now=None):
    """Record these agents as looked at, as of their current state."""
    seen = dict(seen)
    for a in agents:
        if a.get("pane_id") and _seq(a) is not None:
            seen[a["pane_id"]] = _seen_record(a, now)
    return seen


def mark_seen_and_save(agents):
    """Record these agents as looked at, under the seen-file lock."""
    update_seen(lambda seen: (mark_seen(seen, agents), None))


# ---------- GitHub rows ----------

_ITEM_FIELDS = "number title url updatedAt repository { nameWithOwner }"


def github_query():
    """One GraphQL request carrying both searches (aliased)."""
    def search(q):
        return (f'search(query: "{q}", type: ISSUE, first: {GITHUB_FETCHED}) '
                f'{{ issueCount nodes {{ '
                f'... on Issue {{ {_ITEM_FIELDS} }} ... on PullRequest {{ {_ITEM_FIELDS} }} }} }}')
    return ("query { "
            f"review: {search('is:open is:pr review-requested:@me archived:false sort:updated-desc')} "
            f"assigned: {search('is:open assignee:@me archived:false sort:updated-desc')} "
            "}")


def _dict(x):
    return x if isinstance(x, dict) else {}


def _str(x):
    return x if isinstance(x, str) else ""


def parse_github(data):
    """GraphQL response -> {"review"|"assigned": {"count": n, "rows": [...]}}.
    Anything of the wrong shape is skipped, never raised on."""
    out = {}
    for key in ("review", "assigned"):
        block = _dict(_dict(_dict(data).get("data")).get(key))
        nodes = block.get("nodes")
        rows = []
        for n in nodes if isinstance(nodes, list) else []:
            n = _dict(n)
            if not _str(n.get("url")):
                continue
            number = n.get("number")
            rows.append({"repo": _str(_dict(n.get("repository")).get("nameWithOwner")) or "?",
                         "number": number if isinstance(number, int) else "?",
                         "title": _str(n.get("title")),
                         "url": n["url"],
                         "updated": _str(n.get("updatedAt"))[:10]})
        count = block.get("issueCount")
        if not isinstance(count, int) or isinstance(count, bool) or count < len(rows):
            count = len(rows)
        out[key] = {"count": count, "rows": rows}
    return out


def _has_search_data(data):
    d = _dict(_dict(data).get("data"))
    return any(isinstance(d.get(k), dict) for k in ("review", "assigned"))


def _read_github_cache():
    """The saved answer, or None if it's missing or not the shape we write."""
    try:
        c = json.loads(GITHUB_CACHE.read_text())
    except (OSError, ValueError):
        return None
    if (isinstance(c, dict) and isinstance(c.get("fetched"), (int, float))
            and not isinstance(c.get("fetched"), bool) and _has_search_data(c.get("data"))):
        return c
    return None


def github_rows(refresh=False, now=None):
    """Return (parsed rows or None, note or None). One `gh` call at most,
    reused for GITHUB_TTL seconds; never raises."""
    try:
        return _github_rows(refresh, now)
    except Exception as e:  # last line of defence: GitHub must never break `status`
        return None, f"GitHub rows skipped: {type(e).__name__}: {e}"


def _github_rows(refresh, now):
    now = now if now is not None else time.time()
    cache = _read_github_cache()
    if cache and not refresh and now - cache["fetched"] < GITHUB_TTL:
        return parse_github(cache["data"]), None
    err = None
    if not shutil.which("gh"):
        err = "gh not found on PATH (https://cli.github.com)"
    else:
        try:
            r = subprocess.run(["gh", "api", "graphql", "-f", "query=" + github_query()],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                if _has_search_data(data):
                    try:
                        GITHUB_CACHE.parent.mkdir(parents=True, exist_ok=True)
                        GITHUB_CACHE.write_text(json.dumps({"fetched": now, "data": data}))
                    except OSError:
                        pass
                    return parse_github(data), None
                err = "unexpected answer from GitHub"
            else:
                err = (r.stderr.strip().splitlines() or ["gh failed"])[0]
        except subprocess.TimeoutExpired:
            err = "gh timed out after 10 s"
        except ValueError:
            err = "could not read gh's output"
        except OSError as e:
            err = f"could not run gh ({e})"
    if cache:
        mins = int((now - cache["fetched"]) // 60)
        return parse_github(cache["data"]), f"GitHub: showing rows cached {mins} min ago ({err})"
    return None, f"GitHub rows skipped: {err}"


def github_lines(parsed):
    lines = []
    rev = parsed["review"]
    lines.append(f"  review requested ({rev['count']})" + ("" if rev["rows"] else ": none"))
    for r in rev["rows"]:
        lines.append(f"    {r['repo']}#{r['number']}  {r['title'][:70]}  {r['url']}")
    if rev["count"] > len(rev["rows"]):
        # The query fetches the first GITHUB_FETCHED; say so instead of dropping the rest.
        lines.append(f"    … and {rev['count'] - len(rev['rows'])} more: "
                     "https://github.com/pulls/review-requested")
    asg = parsed["assigned"]
    shown = asg["rows"][:ASSIGNED_SHOWN]
    head = f"  assigned to you ({asg['count']})"
    if not shown:
        lines.append(head + ": none")
        return lines
    lines.append(head + (f", {len(shown)} most recently updated:" if asg["count"] > len(shown)
                         else ":"))
    for r in shown:
        lines.append(f"    {r['repo']}#{r['number']}  {r['title'][:70]}  ({r['updated']})  {r['url']}")
    if asg["count"] > len(shown):
        lines.append(f"    … and {asg['count'] - len(shown)} more: https://github.com/issues/assigned")
    return lines


# ---------- status ----------

def short_path(path):
    home = str(Path.home())
    # herdr may report /Volumes/<disk>/MacHD/Users/... for a ~ path.
    return "~" + path[path.find(home) + len(home):] if home in path else path


def cmd_status(args):
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

    # Changed since you looked: compare first, then count focused panes as seen
    # (you're looking at them right now), so they're never flagged.
    focused = [a for a in agents if a.get("focused")]

    def look(seen):
        changed, seen = changes_since_seen(agents, seen)
        return mark_seen(seen, focused), changed - {a["pane_id"] for a in focused}

    changed = update_seen(look)

    for shown, running, rec, version, pane, here in rows:
        name = short_path(shown)
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
            new = "●" if a["pane_id"] in changed else " "
            print(f"   {mark}{new} {agent_label(a)}")
    print("\n* = attached in that project's Bram terminal   ● = changed since you looked")

    print("\nNeeds you")
    waiting = sorted((a for a in agents
                      if a["pane_id"] in changed and a.get("agent_status") in WAITING),
                     key=lambda a: -(_seq(a) or 0))
    if waiting:
        print("  agents changed since you looked, now waiting:")
        for a in waiting:
            print(f"    ● {agent_label(a)}  ({short_path(a.get('cwd', ''))})")
    else:
        print("  no agent has changed and is waiting since you looked")
    if not args.no_github:
        parsed, note = github_rows(refresh=args.refresh)
        if parsed:
            for line in github_lines(parsed):
                print(line)
        if note:
            print(f"  {note}")
    print("\nMark agents as looked at with `seen PANE` or `seen --all`.")


# ---------- seen ----------

def cmd_seen(args):
    agents = herdr_agents()
    if args.all:
        chosen = agents
    else:
        if not args.panes:
            raise SystemExit("name one or more panes (e.g. `seen w1:p2`), or pass --all")
        by_id = {a["pane_id"]: a for a in agents}
        missing = [p for p in args.panes if p not in by_id]
        if missing:
            raise SystemExit(f"no herdr agent in pane(s): {', '.join(missing)}")
        chosen = [by_id[p] for p in args.panes]
    mark_seen_and_save(chosen)
    print("marked seen: " + (", ".join(a["pane_id"] for a in chosen) or "nothing"))


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


def check_bram_repo():
    """Fail early, before `up` writes anything, if BRAM_REPO has no ./bram."""
    if not (BRAM_REPO / "bram").exists():
        raise SystemExit(f"no ./bram symlink in {BRAM_REPO}. Set BRAM_REPO to your "
                         "judell/bram checkout (built from main at or after 1aa63a1).")


def launch_bram(project, dry, attach=None):
    check_bram_repo()
    env ={k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
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


def warn_existing_attaches(pane):
    """Warn if something already holds `herdr agent attach <pane>`; the new
    Bram's auto-attach would then fail silently. Before Bram 0.7.1 (judell/bram
    #405) quitting a Bram orphaned its attach to launchd and this tool killed
    those; Bram now hangs up its terminal's jobs on quit, so it only reports."""
    for pid, (ppid, cmd) in process_table().items():
        argv = cmd.split()
        if argv[:3] == ["herdr", "agent", "attach"] and argv[3:4] == [pane]:
            where = "orphaned to launchd" if ppid == 1 else "in a running terminal"
            print(f"  !! {pane} is already attached {where} (pid {pid}); the new Bram's "
                  f"attach may fail. Stop it with `kill {pid}` if it's stale.")


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
    check_bram_repo()  # before anything is written to the project
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
        warn_existing_attaches(agent["pane_id"])
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
    # You're looking at this Bram now, so its attached agent counts as seen.
    pane = attached_pane(pid, process_table())
    if pane and shutil.which("herdr"):
        hit = [a for a in herdr_agents() if a["pane_id"] == pane]
        if hit:
            mark_seen_and_save(hit)


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
               f"  {LOG_DIR}/<project>.log  (Bram's output from `up`)\n"
               f"cache:\n"
               f"  {SEEN_FILE}  (when you last looked at each agent)\n"
               f"  {GITHUB_CACHE}  (GitHub rows, reused for {GITHUB_TTL // 60} min)\n\n"
               f"See README.md for requirements and how auto-attach works.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    st = sub.add_parser("status", help="which Bram is running where, which herdr agent each "
                                       "has attached, and what needs you")
    st.add_argument("--refresh", action="store_true",
                    help=f"ask GitHub again instead of reusing the answer "
                         f"from the last {GITHUB_TTL // 60} minutes")
    st.add_argument("--no-github", action="store_true",
                    help="skip the GitHub rows (no gh call)")
    st.set_defaults(fn=cmd_status)
    se = sub.add_parser("seen", help="mark herdr agents as looked at, clearing their ● marker")
    se.add_argument("panes", nargs="*", metavar="PANE", help="herdr pane id(s), e.g. w1:p2")
    se.add_argument("--all", action="store_true", help="mark every herdr agent")
    se.set_defaults(fn=cmd_seen)
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
