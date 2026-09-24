---
name: bram-herdr
description: DRAFT. Put a project under Bram with its agent owned by herdr, without the setup tedium. Use when Raymond says "bram this project", "set up bram for X", "launch bram for the X agent", "restart the X bram", "attach bram to <herdr pane>", "which bram is running where", or when starting Bram on a new repo. Covers preflight (protect CLAUDE.md, gitignore Bram's files), the "Do not start an agent" setting, picking the herdr agent, launching Bram, and attaching.
status: draft (helper script written and dry-run tested 2026-09-23)
created: 2026-09-23
---

<!-- cc:2026.09.23 — drafted in bram-0922 from the #389 work and the unglueit-0923 adoption (judell/bram#395, #396). Raymond's own skill; NOT bram-managed. Bram's bundled skills live in app/skills/ and Setup overwrites those; this one is safe from that. -->

# Bram + herdr: onboard a project

**Goal:** Raymond keeps every agent in herdr (one workspace, driven from the CLI) and uses Bram as each project's board: Worklist, approvals, reminders. Getting a project there should be one command, not the eight manual steps it took on 2026-09-23.

**Requires a Bram built from judell/bram `main` at or after `1aa63a1`** (PR judell/bram#394 merged 2026-09-24), checked out at `~/C/src/bram` and launched via its `./bram` symlink. Released Bram ≤0.6.9 has no "Do not start an agent" setting.

**Branch layout in `~/C/src/bram` (set up 2026-09-24):**
- `main` tracks `upstream/main` (Jon's judell/bram). Only fast-forward it; never commit on it.
- Each change gets its own branch cut from `main` (e.g. `dock-badge-403`, `shellrc-greeting-none`), so it can become a clean PR.
- **`ry-dev`** is Raymond's running build: `main` plus the change branches he wants live, joined by **merge, never rebase** (cited SHAs must stay valid). **Keep `ry-dev` checked out when building**, since every Bram runs the one binary this checkout builds. Refresh it with `git fetch upstream && git merge --ff-only upstream/main` on `main`, then `git merge main` (and any new change branch) on `ry-dev`.
- Don't cut a change from `ry-dev`, or its PR drags every other change along.

**Launching from any Claude session (CoS included):** the script takes absolute paths, so the caller's cwd doesn't matter. For a project already set up for Bram, `bram_herdr.py up <project>` is safe to run straight away; it refuses if that Bram is already running. For a project **new** to Bram, dry-run first and ask Raymond the step 2 question (commit Bram's files, or exclude them) before the real launch.

## The routine (what the helper script automates)

1. **Preflight: protect the project's instruction files.** Bram's first-run Setup edits `CLAUDE.md` and `AGENTS.md` (it appends a `<!-- bram:start -->…<!-- bram:end -->` block). On 2026-09-23 in Gluejar, `CLAUDE.md` was left holding **only** the block (judell/bram#396, candidate cause: `read_to_string(...).unwrap_or_default()` treating a read error as empty). So before the first launch:
   - make sure `CLAUDE.md` / `AGENTS.md` are committed, or copy them to `.bram-preflight/` with a timestamp;
   - after launch, verify each file is its old content **plus** the block (`git diff --numstat` shows `+N −0`). If not, restore it and report on #396.
2. **Keep Bram's files out of the project's history.** Setup writes `.bram.json`, `resources/`, `.claude/bram-conventions.md`, `.claude/skills/loose-ends/`, and hook settings. Ask Raymond once per project: commit them, or add them to `.git/info/exclude`, which is local-only and leaves the repo's `.gitignore` alone. **Omnibus folders** (like Gluejar: an untracked-heavy parent over nested repos) should exclude them, and should exclude large untracked trees (virtualenvs) too. Since judell/bram ee92681 those no longer slow the Worklist, but they're still noise.
3. **Set "Do not start an agent".** Merge into `.bram.json` (create it if missing; never clobber other keys):
   `{"shell": {"startupPolicy": "none", "agent": "<claude|codex>"}}`, with `agent` = the kind of herdr agent that will be attached. This is what the Settings → On Bram launch dropdown writes.
4. **Pick the herdr agent.** `herdr agent list` → filter `cwd == <project>`:
   - one match → use it;
   - several → ask which (show `pane_id`, `agent`, `name`, `agent_status`);
   - none → offer `herdr pane split <some pane> --cwd <project>` then `herdr agent start <name> --kind claude --pane <new>`.
5. **Launch Bram** from the bram checkout so disk-served files are used: `cd ~/C/src/bram && ./bram <project>` (background; kill by PID only, never `pkill bram`, since other Bram instances may be running).
6. **Attach — automatic since 2026-09-24.** `bram_herdr.py up` launches Bram with `PROMPT_COMMAND='unset PROMPT_COMMAND; herdr agent attach <pane>'` in its environment. Bram's terminal is bash and inherits that environment, so bash attaches by itself just before its first prompt. There is no Bram change, and nothing is typed into the terminal. `up` then confirms the attach from the process tree (`ok attached: <pane>`). This was verified live on onrealm (`w3:p49`). Pass `--no-auto-attach` to get the old behaviour. Only if auto-attach fails (`up` says `!! auto-attach not seen`), or when launching Bram by hand, fall back to the manual step below. Either way, still tell Raymond which pane each Bram attached to.

   Manual fallback: put `herdr agent attach <pane_id>` on the clipboard (`pbcopy`). **Always also show Raymond the literal command in chat**, in its own code block, with the real pane id filled in:

   ````
   ```
   herdr agent attach w3:p44
   ```
   ````

   Then tell him: *"click into Bram's terminal, paste (or type the line above), Enter."* The clipboard is a convenience, not the channel: a later `up`, or any copy, silently replaces it, and Raymond asked (2026-09-23) to always be given the incantation. When relaunching several Brams, give one code block per Bram, each labelled with its project.
7. **Verify** from `<project>/resources/bram-traces/bram-trace.log`:
   - `op=autostart policy=none provider=<kind>`, with an empty `command=`;
   - no `console-error`;
   - and step 1's `CLAUDE.md` check.

## Switching the attached agent

Identity is recorded **at launch only**. To move Bram from Codex to Claude: change `.bram.json` `shell.agent` → quit Bram → relaunch → attach the other pane, giving the `herdr agent attach <pane>` line in chat as in step 6. A stale identity makes Bram ignore the attached agent's turn-ends for claims (65da73a), so the spinner won't clear.

## Talking to agents

- Bram's message box reaches **only the attached agent**.
- Anything else goes through herdr: `herdr agent prompt <pane> "<text>"` (`--wait --timeout <ms>` to block). It's rejected if the agent is sitting on a permission prompt.
- Permission prompts don't show in Bram's pane for herdr-launched agents. Answer them in the terminal.

## Status: "which Bram is running where"

For each running Bram (`ps` for `bram <path>`), read `<path>/resources/.bram-port.json` (port, pid, root) and `GET /__app-info` (version). Join with `herdr agent list` by cwd. Report a table of project, Bram pid/port/version, attached herdr pane, and other herdr agents in that folder.

## Known gaps: these belong in Bram, not here

Tracked on judell/bram#389 (UX roll-up comment, 2026-09-23):

- a launch flag or machine-wide default instead of a per-project setting;
- Bram listing and attaching herdr agents itself;
- identity read from herdr instead of asserted;
- a switch without a relaunch;
- an agent-pane reload control;
- permission prompts in the pane.

**When one of these lands upstream, delete the matching workaround here.**

## Helper script

`~/C/src/python-learning/scripts/bram_herdr.py` (stdlib only). Run it with `~/.pyenv/versions/myenv/bin/python`.

- `bram_herdr.py status`: every folder with a herdr agent or a running Bram, with Bram's version/pid/port and which herdr pane it has attached (found by walking the process tree from the Bram pid to its `herdr agent attach` child). Read-only.
- `bram_herdr.py up <project> [--pane <id>] [--kind claude|codex] [--exclude] [--dry-run]`: steps 1–7. It refuses if Bram is already running for the project, and asks for `--pane` when several agents share the folder. It launches Bram with the `CLAUDE_CODE_*` variables scrubbed (bram #339 launch hygiene), auto-attaches the pane via `PROMPT_COMMAND` (step 6; `--no-auto-attach` to skip), waits for the `op=autostart` trace line, and checks that `CLAUDE.md`/`AGENTS.md` kept the user's text outside Bram's `<!-- bram:start/end -->` block (Setup may legitimately refresh the block itself). **Always `--dry-run` first on a new project.**
- `bram_herdr.py front <project>`: bring that project's Bram window to the front. `<project>` is a path or just the folder name (`wtdickens`). It raises the Bram process by pid via `osascript` / System Events; macOS may ask for Automation permission the first time. Use it when Raymond says "show me the X Bram".
- `bram_herdr.py say [<project>] "<text>" [--pane <id>] [--wait]`: `herdr agent prompt` to the agent attached to that project's Bram.

It writes only `.bram.json` (merged), `.bram-preflight/` (backups), and, with `--exclude`, `.git/info/exclude`. It never edits `CLAUDE.md`.

Tested 2026-09-23: `status` and `up --dry-run` (Gluejar refusal, wtdickens non-git preflight, bram two-agent prompt). **Not yet exercised:** a real `up` launch, and `say`.
