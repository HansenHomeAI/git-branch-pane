# Git Branch Pane

A dependency-free local web pane for seeing Git branch and merge relationships across all refs.

It is built for the practical Codex workflow: start it in the integrated terminal, open the URL in the Codex in-app browser, and keep the browser pane beside your thread while you work.

## Best Option

Codex does not currently document a native API for adding custom permanent sidebar panes. The closest working route is a narrow local web app in the Codex in-app browser. This gives you the core branch graph workflow without waiting on a Codex UI extension surface.

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

On the remote machine:

```sh
python3 git_branch_pane.py /path/to/repo --host 127.0.0.1 --port 8765
```

On your Mac:

```sh
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

Then open:

```text
http://127.0.0.1:8765/?repo=/path/to/repo
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
