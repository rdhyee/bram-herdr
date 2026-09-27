# bram-herdr

A small command-line helper for running several [Bram](https://github.com/judell/bram) instances, one per project, where each project's coding agent (Claude Code or Codex) lives in [herdr](https://herdr.dev) instead of being started by Bram.

Bram's **"Do not start an agent"** setting ([judell/bram#394](https://github.com/judell/bram/pull/394)) makes that arrangement possible. `bram_herdr.py` takes out the chores around it: protecting your instruction files, setting the option, launching Bram, and attaching the right herdr agent to Bram's terminal.

## Why

herdr keeps every agent in one terminal workspace that you can drive from the command line. Bram gives each project a board: the Worklist, approvals, reminders. Using both means one Bram per project, each with its agent owned by herdr. Doing that by hand took eight steps per project. With this tool it's one command.

## Commands

```sh
bram_herdr.py status
```
Which Bram is running where, with its version, pid and port, which herdr pane it has attached, and the other herdr agents in that folder. Read-only.

```sh
bram_herdr.py up PROJECT [--pane ID] [--kind claude|codex] [--exclude] [--dry-run] [--no-auto-attach]
```
Backs up `CLAUDE.md`/`AGENTS.md` if git can't restore them, sets "Do not start an agent" in `PROJECT/.bram.json`, launches Bram, and attaches the herdr agent working in `PROJECT`. Afterwards it checks Bram's trace log and that your instruction files kept their text. Use `--pane` when several agents work in the folder, and `--exclude` to add Bram's files to `.git/info/exclude`. **Run `--dry-run` first on a new project.**

```sh
bram_herdr.py front PROJECT
```
Brings that project's Bram window to the front (macOS). `PROJECT` is a path or just the folder name.

```sh
bram_herdr.py say [PROJECT] TEXT [--pane ID] [--wait]
```
Sends a prompt to the agent attached to that project's Bram, through `herdr agent prompt`.

`bram_herdr.py --help` and `bram_herdr.py <command> --help` give the full option list.

## Requirements

- **macOS.** `front` uses `osascript` (System Events; macOS may ask for Automation permission the first time), and `up` copies the attach command with `pbcopy`. The script also uses `ps`, `lsof`, `curl` and `git`. On other systems `front` exits with a clear message and `up` skips the clipboard, but only macOS has been tested.
- **Python 3.7 or later**, standard library only. Nothing to `pip install`. (3.7 is needed for `subprocess.run(capture_output=…)`. Checked against `python:3.6-slim`, where it fails, and `python:3.7-slim`, where it works.)
- **[herdr](https://herdr.dev)** on your `PATH` (`brew install herdr`). Tested with herdr 0.8.2.
- **Bram built from [judell/bram](https://github.com/judell/bram) `main` at or after `1aa63a1`**, the merge of #394. Released Bram 0.6.9 and earlier doesn't have the "Do not start an agent" setting. The script launches Bram through the `./bram` symlink in that checkout, so the files it serves come from disk.
- **`BRAM_REPO`** set to that checkout. It defaults to `~/C/src/bram`.

## Install

```sh
git clone https://github.com/rdhyee/bram-herdr.git
export BRAM_REPO=~/src/bram          # your judell/bram checkout
python3 bram-herdr/bram_herdr.py status
```

Optionally, make the Claude Code skill available, so an agent can run the routine when you say "bram this project":

```sh
ln -s "$PWD/bram-herdr/skill" ~/.claude/skills/bram-herdr
```

## What it touches

`up` writes only these:

- `PROJECT/.bram.json`: it sets `shell.startupPolicy` and `shell.agent` and keeps your other keys.
- `PROJECT/.bram-preflight/<timestamp>/`: backups of `CLAUDE.md`/`AGENTS.md` when git can't restore them.
- `.git/info/exclude`: only with `--exclude`.

It **never** edits `CLAUDE.md` or `AGENTS.md`. After Bram's Setup runs, it checks that your text outside Bram's `<!-- bram:start --> … <!-- bram:end -->` block is unchanged, because Setup has been seen to wipe it ([judell/bram#396](https://github.com/judell/bram/issues/396)). Bram's own launch output goes to `~/.cache/bram-herdr/<project>.log`.

## How auto-attach works

Roughly: Bram's built-in terminal runs the attach command for you as soon as it opens.

More precisely: Bram's terminal is `bash`, and it inherits Bram's environment. `up` launches Bram with

```sh
PROMPT_COMMAND='unset PROMPT_COMMAND; herdr agent attach <pane>'
```

bash runs `PROMPT_COMMAND` just before it shows its first prompt, so the shell attaches once, by itself. Nothing gets typed into the terminal, and Bram doesn't need any change. The `unset` comes first so that detaching drops you back at a plain prompt, and so the attach process (and anything after it) doesn't inherit the variable. `up` then confirms the attach by looking for a `herdr agent attach` process under the Bram pid. If it isn't there within 20 seconds, `up` prints the command for you to run by hand. `--no-auto-attach` skips all this and only puts the command on the clipboard.

`up` also removes `CLAUDE_CODE_*` variables from Bram's environment. Without that, a Bram launched from inside a Claude Code session would make its own agent think it's a child session.

## Leftover attaches

If something is already running `herdr agent attach <pane>` for the pane `up` is about to use, the new Bram's attach fails without saying so. Before launching, `up` checks for that and warns, with the pid to stop.

Before Bram 0.7.1, quitting a Bram left its attach running, orphaned to launchd, and `up` killed those orphans itself. Bram now hangs up its terminal's jobs on every way of quitting ([judell/bram#405](https://github.com/judell/bram/issues/405)), so that cleanup was retired. Use Bram 0.7.1 or later.

## Limits, and where this should go upstream

These are workarounds, and they should go away as Bram grows the features:

- [judell/bram#389](https://github.com/judell/bram/issues/389): run an external command such as `herdr agent attach` in Bram's agent pane while keeping the Worklist gate. This is the umbrella issue this tool works around.
- [judell/bram#401](https://github.com/judell/bram/issues/401): one view across several separate Brams.
- [judell/bram#402](https://github.com/judell/bram/issues/402): steer Bram from a phone.
- [judell/bram#403](https://github.com/judell/bram/issues/403): tell Bram instances apart in the Dock.

## License

MIT. See [LICENSE](LICENSE).
