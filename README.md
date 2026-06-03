# Git Branch Pane

A dependency-free local web pane for seeing Git branch and merge relationships across all refs.

It is built for the practical Codex workflow: start it in the integrated terminal, open the URL in the Codex in-app browser, and keep the browser pane beside your thread while you work.

## Best Option

Codex does not currently document a native API for adding custom permanent sidebar panes. The closest working route is a narrow local web app in the Codex in-app browser. This gives you the core branch graph workflow without waiting on a Codex UI extension surface.

## One Paste Command

From any Git project on any machine:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```

That command installs or updates the machine-level `gbp` tool, then starts a local server for the Git repo under your current directory. It does not copy anything into that project and does not modify that project's Git data.

After it is installed once, run this from any Git project:

```sh
gbp
```

Or point it at a specific repo:

```sh
gbp /path/to/any/repo
```

The paste command can also target a specific repo:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh -s -- /path/to/any/repo
```

## Run

After installing, run this from any Git project:

```sh
gbp
```

That starts the pane for the repository under your current directory.

## Install

From this checkout:

```sh
./scripts/install-gbp
```

The installer copies the pane to `~/.local/share/git-branch-pane/` and writes the `gbp` command to `~/.local/bin/`.

From the public repo, install without immediately launching:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | GBP_NO_RUN=1 sh
```

```sh
python3 git_branch_pane.py /path/to/repo --port 8765
```

Open:

```text
http://127.0.0.1:8765/?repo=/path/to/repo
```

For this project:

```sh
python3 git_branch_pane.py . --port 8765
```

## Use On SSH Machines

On your Mac, open the SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

On the remote machine, from the repo you want to view:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```

Or, after it is already installed:

```sh
gbp --host 127.0.0.1 --port 8765
```

Then open locally:

```text
http://127.0.0.1:8765
```

If the server reports a different port because `8765` is busy, tunnel that port instead.

## Deployment Model

Git Branch Pane is a machine-level tool, not a per-project dependency.

The public repo is cloned into:

```text
~/.local/share/git-branch-pane/source
```

The runnable app is copied into:

```text
~/.local/share/git-branch-pane/git_branch_pane.py
```

The global command is written to:

```text
~/.local/bin/gbp
```

## Features

- SVG branch graph with curved colored lines, split/merge lanes, and commit dots.
- Persistent branch head labels.
- Hover commit dots or labels for commit specs.
- Search commits, SHAs, authors, and refs.
- Auto-refresh every 15 seconds.
- No npm, no package install, no API key, no external service.

## Recommended Shell Shortcut

Add this to your shell profile on each computer:

```sh
gbp() {
  python3 /absolute/path/to/git_branch_pane.py "${1:-.}" --port "${GBP_PORT:-8765}"
}
```

Then run:

```sh
gbp .
```

The browser view is intentionally sparse: branch heads stay labeled, ordinary commits stay compact, and full details are available on hover.

## Options Compared

1. Local web pane in Codex Browser: best working path now. Fully implementable and portable over SSH.
2. Terminal UI such as `lazygit`, `tig`, or `gitk`: useful and mature, but not a Codex side/browser pane.
3. Codex plugin or skill: good for reusable setup and commands, but current Codex plugins bundle skills, apps, and MCP servers rather than custom native sidebar UI.
4. VS Code/Cursor extension: closest native editor behavior, but it means keeping VS Code/Cursor in the workflow.
