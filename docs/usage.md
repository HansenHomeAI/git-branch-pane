# Usage

Install or update and immediately run the pane from any Git repository:

```sh
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```

After first install, run:

```sh
gbp
```

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

For an SSH host, run the pane on the remote host and tunnel the port back to your local machine.

```sh
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

Then, on the remote:

```sh
cd /remote/project
curl -fsSL https://raw.githubusercontent.com/HansenHomeAI/git-branch-pane/main/install.sh | sh
```
