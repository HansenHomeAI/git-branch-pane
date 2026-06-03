# Usage

Install or update and immediately start the persistent pane from any Git repository:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```

After first install, run:

```sh
gbp
```

`gbp` starts or restarts a background local server and prints the URL. It keeps running after the shell command returns, with no built-in timeout.

Or:

```sh
gbp /path/to/repo
```

Paste-command target for a repo outside the current directory:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh -s -- /path/to/repo
```

Install/update only:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | GBP_NO_RUN=1 sh
```

Requirements on each machine are `git`, `python3`, and `~/.local/bin` in `PATH` for the short `gbp` command.

Server controls:

```sh
gbp --status
gbp --stop
gbp --foreground
```

Use `gbp --foreground` only if you want an attached terminal process.

For an SSH host, run the pane on the remote host and tunnel the port back to your local machine.

```sh
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

Then, on the remote:

```sh
cd /remote/project
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```
