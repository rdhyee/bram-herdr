---
name: bram-herdr
description: Put a project under Bram with its agent owned by herdr, without the setup tedium. Use when the user says "bram this project", "set up bram for X", "launch bram for the X agent", "restart the X bram", "attach bram to <herdr pane>", "which bram is running where", or when starting Bram on a new repo. Covers preflight (protect CLAUDE.md, keep Bram's files out of history), the "Do not start an agent" setting, picking the herdr agent, launching Bram, and attaching.
---

# Bram + herdr: onboard a project

**Goal:** keep every coding agent in [herdr](https://herdr.dev) (one workspace, driven from the CLI) and use [Bram](https://github.com/judell/bram) as each project's board: Worklist, approvals, reminders. Getting a project there should take one command, not eight manual steps.

**Requires a Bram built from judell/bram `main` at or after `1aa63a1`** (the merge of judell/bram#394), launched via the `./bram` symlink in its checkout. Set `BRAM_REPO` to that checkout (the helper defaults to `~/C/src/bram`). Released Bram ≤ 0.6.9 has no "Do not start an agent" setting. If you build Bram from a fork, keep the branch you build checked out, because every Bram runs the one binary that checkout builds.

**Launching from any Claude session:** the script takes absolute paths, so the caller's cwd doesn't matter. For a project already set up for Bram, `bram_herdr.py up <project>` is safe to run straight away, since it refuses if that Bram is already running. For a project **new** to Bram, dry-run first and ask the user the step 2 question (commit Bram's files, or exclude them) before the real launch.

## The routine (what the helper script automates)

1. **Preflight: protect the project's instruction files.** Bram's first-run Setup edits `CLAUDE.md` and `AGENTS.md` (it adds a `<!-- bram:start -->…<!-- bram:end -->` block). Setup has been seen to leave `CLAUDE.md` holding **only** the block (judell/bram#396). So before the first launch:
   - make sure `CLAUDE.md` / `AGENTS.md` are committed, or copy them to `.bram-preflight/` with a timestamp;
   - after launch, verify each file is its old content **plus** the block (`git diff --numstat` shows `+N −0`). If not, restore it and report on judell/bram#396.
2. **Keep Bram's files out of the project's history.** Setup writes `.bram.json`, `resources/`, `.claude/bram-conventions.md`, `.claude/bram-reference/`, `.claude/skills/loose-ends/`, and hook settings. Ask the user once per project: commit them, or add them to `.git/info/exclude`, which is local-only and leaves the repo's `.gitignore` alone. A folder that holds several nested repos, or lots of untracked material, should exclude them. It should also exclude large untracked trees such as virtualenvs.
3. **Set "Do not start an agent".** Merge into `.bram.json` (create it if missing; never clobber other keys):
   `{"shell": {"startupPolicy": "none", "agent": "<claude|codex>"}}`, where `agent` is the kind of herdr agent that will be attached. The Settings → On Bram launch dropdown writes the same thing.
4. **Pick the herdr agent.** `herdr agent list` → filter `cwd == <project>`:
   - one match → use it;
   - several → ask which (show `pane_id`, `agent`, `name`, `agent_status`);
   - none → offer `herdr pane split <some pane> --cwd <project>` then `herdr agent start <name> --kind claude --pane <new>`.
5. **Launch Bram** from the Bram checkout so disk-served files are used: `cd "$BRAM_REPO" && ./bram <project>`. Run it in the background. Kill it by PID only, never `pkill bram`, since other Bram instances may be running.
6. **Attach, automatically.** `bram_herdr.py up` launches Bram with `PROMPT_COMMAND='unset PROMPT_COMMAND; herdr agent attach <pane>'` in its environment. Bram's terminal is bash and inherits that environment, so bash attaches by itself just before its first prompt. That needs no Bram change and types nothing into the terminal. `up` then confirms the attach from the process tree (`ok attached: <pane>`). Pass `--no-auto-attach` to skip it. Fall back to the manual step below only when auto-attach fails (`up` says `!! auto-attach not seen`) or when launching Bram by hand. Either way, tell the user which pane each Bram attached to.

   Manual fallback: put `herdr agent attach <pane_id>` on the clipboard (`pbcopy` on macOS). **Always also show the user the literal command in chat**, in its own code block, with the real pane id filled in:

   ````
   ```
   herdr agent attach w1:p2
   ```
   ````

   Then tell them: *"click into Bram's terminal, paste (or type the line above), Enter."* The clipboard is only a convenience: a later `up`, or any copy, silently replaces it. When relaunching several Brams, give one code block per Bram, each labelled with its project.
7. **Verify** from `<project>/resources/bram-traces/bram-trace.log`:
   - `op=autostart policy=none provider=<kind>`, with an empty `command=`;
   - no `console-error`;
   - and step 1's `CLAUDE.md` check.

## Switching the attached agent

Bram records the agent's identity **at launch only**. To move Bram from Codex to Claude: change `shell.agent` in `.bram.json` → quit Bram → relaunch → attach the other pane, giving the `herdr agent attach <pane>` line in chat as in step 6. If the identity is stale, Bram ignores the attached agent's turn-ends for claims, so the spinner won't clear.

## Talking to agents

- Bram's message box reaches **only the attached agent**.
- Anything else goes through herdr: `herdr agent prompt <pane> "<text>"` (add `--wait` to block until herdr reports the next state). herdr rejects it if the agent is sitting on a permission prompt.
- Permission prompts don't show in Bram's pane for herdr-launched agents. Answer them in the terminal.

## Status: "which Bram is running where"

For each running Bram (`ps` for `bram <path>`), read `<path>/resources/.bram-port.json` (port, pid, root) and `GET /__app-info` (version). Join with `herdr agent list` by cwd. Report a table of project, Bram pid/port/version, attached herdr pane, and other herdr agents in that folder.

## Known gaps: these belong in Bram, not here

Tracked on judell/bram#389:

- a launch flag or machine-wide default instead of a per-project setting;
- Bram listing and attaching herdr agents itself;
- identity read from herdr instead of asserted;
- a switch without a relaunch;
- an agent-pane reload control;
- permission prompts in the pane.

**When one of these lands upstream, delete the matching workaround here.**

## Helper script

`bram_herdr.py` at the root of this repo (standard library only, Python 3.7+). Run it with any `python3`.

- `bram_herdr.py status`: every folder with a herdr agent or a running Bram, with Bram's version/pid/port and which herdr pane it has attached. It finds the pane by walking the process tree from the Bram pid to its `herdr agent attach` child. Read-only.
- `bram_herdr.py up <project> [--pane <id>] [--kind claude|codex] [--exclude] [--dry-run] [--no-auto-attach]` runs steps 1–7:
  - It refuses if Bram is already running for the project, and asks for `--pane` when several agents share the folder.
  - It stops any `herdr agent attach` left over from a quit Bram, then launches Bram with the `CLAUDE_CODE_*` variables removed from its environment.
  - It auto-attaches the pane via `PROMPT_COMMAND` (step 6) and waits for the `op=autostart` trace line.
  - It checks that `CLAUDE.md`/`AGENTS.md` kept the user's text outside Bram's `<!-- bram:start/end -->` block. Setup may legitimately refresh the block itself.
  - **Always `--dry-run` first on a new project.**
- `bram_herdr.py front <project>`: bring that project's Bram window to the front (macOS). `<project>` is a path or just the folder name. It raises the Bram process by pid via `osascript` / System Events, and macOS may ask for Automation permission the first time. Use it when the user says "show me the X Bram".
- `bram_herdr.py say [<project>] "<text>" [--pane <id>] [--wait]`: `herdr agent prompt` to the agent attached to that project's Bram.

It writes only `.bram.json` (merged), `.bram-preflight/` (backups), and, with `--exclude`, `.git/info/exclude`. It never edits `CLAUDE.md` or `AGENTS.md`.
