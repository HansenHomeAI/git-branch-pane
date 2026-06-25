#!/usr/bin/env python3
"""Local Git branch graph pane.

Run:
    python3 git_branch_pane.py /path/to/repo
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import threading
import time
import textwrap
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FIELD_SEP = "\x1f"
GRAPH_COLORS = [
    "#4D8DFF",
    "#6EA8FF",
    "#4CC7E8",
    "#2DA6C2",
    "#4FC97A",
    "#36A96B",
    "#7775E6",
    "#9B94F2",
    "#A98BEA",
    "#8D75D8",
    "#CFA64A",
    "#D96F63",
    "#E07AA8",
    "#B88AF0",
    "#7BCFA4",
    "#E0B85A",
]
COLOR_SEQUENCE = [0, 2, 6, 12, 4, 15, 8, 14, 11, 1, 13, 3, 10, 5, 7, 9]
RESERVED_BRANCH_COLORS = {"main": 0, "development": 6}
BRANCH_COLOR_SEQUENCE = [2, 12, 4, 15, 8, 14, 11, 1, 13, 3, 10, 5, 7, 9]
BRANCH_COLOR_SCHEMA_VERSION = 3
GIT_TIMEOUT_SECONDS = 15
GIT_CACHE_SECONDS = 2
GIT_FETCH_INTERVAL_SECONDS = 300
_GIT_CACHE_LOCK = threading.Condition()
_GIT_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, subprocess.CompletedProcess[str]]] = {}
_GIT_IN_FLIGHT: set[tuple[str, tuple[str, ...]]] = set()
_BRANCH_COLOR_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_FETCH_WORKERS: dict[str, threading.Thread] = {}
_FETCH_STATES: dict[str, dict[str, object]] = {}


def hidden_subprocess_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def run_git(repo: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    command_key = (repo, tuple(args))
    now = time.monotonic()
    with _GIT_CACHE_LOCK:
        cached = _GIT_CACHE.get(command_key)
        if cached and now - cached[0] < GIT_CACHE_SECONDS:
            result = cached[1]
            if check and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
            return result
        while command_key in _GIT_IN_FLIGHT:
            _GIT_CACHE_LOCK.wait(timeout=GIT_TIMEOUT_SECONDS)
            cached = _GIT_CACHE.get(command_key)
            if cached and time.monotonic() - cached[0] < GIT_CACHE_SECONDS:
                result = cached[1]
                if check and result.returncode:
                    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
                return result
        _GIT_IN_FLIGHT.add(command_key)

    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(
            exc.cmd or ["git", "-C", repo, *args],
            124,
            timeout_text(exc.stdout),
            timeout_text(exc.stderr) or "Git command timed out",
        )
    finally:
        with _GIT_CACHE_LOCK:
            _GIT_IN_FLIGHT.discard(command_key)
            _GIT_CACHE_LOCK.notify_all()

    with _GIT_CACHE_LOCK:
        _GIT_CACHE[command_key] = (time.monotonic(), result)
        if len(_GIT_CACHE) > 256:
            oldest = sorted(_GIT_CACHE.items(), key=lambda item: item[1][0])[:64]
            for old_key, _ in oldest:
                _GIT_CACHE.pop(old_key, None)

    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result


def clear_git_cache(repo: str) -> None:
    with _GIT_CACHE_LOCK:
        for key in [key for key in _GIT_CACHE if key[0] == repo]:
            _GIT_CACHE.pop(key, None)


def repo_root(path: str) -> str:
    candidate = os.path.abspath(os.path.expanduser(path or "."))
    result = run_git(candidate, ["rev-parse", "--show-toplevel"])
    return result.stdout.strip()


def git_ok(path: str) -> tuple[bool, str]:
    try:
        root = repo_root(path)
        return True, root
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        message = getattr(exc, "stderr", "") or str(exc)
        return False, message.strip()


def parse_query(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlparse(path)
    return parsed.path, urllib.parse.parse_qs(parsed.query)


def first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def fetch_interval_seconds() -> int:
    raw = os.environ.get("GBP_FETCH_INTERVAL_SECONDS", str(GIT_FETCH_INTERVAL_SECONDS))
    try:
        return max(5, int(raw))
    except ValueError:
        return GIT_FETCH_INTERVAL_SECONDS


def auto_fetch_enabled() -> bool:
    return os.environ.get("GBP_AUTO_FETCH", "1") != "0"


def fetch_state(repo: str) -> dict[str, object]:
    if not auto_fetch_enabled():
        return {"enabled": False, "intervalSeconds": fetch_interval_seconds()}
    ensure_fetch_worker(repo)
    with _FETCH_LOCK:
        state = dict(_FETCH_STATES.get(repo, {}))
    state.setdefault("enabled", True)
    state.setdefault("intervalSeconds", fetch_interval_seconds())
    return state


def update_fetch_state(repo: str, **updates: object) -> None:
    with _FETCH_LOCK:
        state = _FETCH_STATES.setdefault(repo, {"enabled": True, "intervalSeconds": fetch_interval_seconds()})
        state.update(updates)


def fetch_repo_once(repo: str) -> dict[str, object]:
    started = time.time()
    update_fetch_state(repo, inFlight=True, lastAttemptUnix=started, lastError="")
    remotes = run_git(repo, ["remote"], check=False)
    if remotes.returncode != 0:
        finished = time.time()
        error = remotes.stderr.strip() or remotes.stdout.strip() or "Unable to list git remotes"
        update_fetch_state(
            repo,
            inFlight=False,
            lastError=error,
            lastDurationSeconds=round(finished - started, 3),
            nextFetchUnix=finished + fetch_interval_seconds(),
        )
        return fetch_state(repo)
    if not remotes.stdout.split():
        finished = time.time()
        update_fetch_state(
            repo,
            inFlight=False,
            lastSuccessUnix=finished,
            lastError="",
            lastDurationSeconds=round(finished - started, 3),
            nextFetchUnix=finished + fetch_interval_seconds(),
            skippedReason="No remotes configured",
        )
        return fetch_state(repo)

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", repo, "fetch", "--all", "--prune", "--quiet"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(
            exc.cmd or ["git", "-C", repo, "fetch", "--all", "--prune", "--quiet"],
            124,
            timeout_text(exc.stdout),
            timeout_text(exc.stderr) or "Git fetch timed out",
        )

    finished = time.time()
    if result.returncode == 0:
        clear_git_cache(repo)
        update_fetch_state(
            repo,
            inFlight=False,
            lastSuccessUnix=finished,
            lastError="",
            lastDurationSeconds=round(finished - started, 3),
            nextFetchUnix=finished + fetch_interval_seconds(),
            skippedReason="",
        )
    else:
        update_fetch_state(
            repo,
            inFlight=False,
            lastError=result.stderr.strip() or result.stdout.strip() or f"git fetch exited {result.returncode}",
            lastDurationSeconds=round(finished - started, 3),
            nextFetchUnix=finished + fetch_interval_seconds(),
            skippedReason="",
        )
    return fetch_state(repo)


def auto_fetch_loop(repo: str) -> None:
    while True:
        fetch_repo_once(repo)
        time.sleep(fetch_interval_seconds())


def ensure_fetch_worker(repo: str) -> None:
    if not auto_fetch_enabled():
        return
    repo = os.path.abspath(repo)
    with _FETCH_LOCK:
        worker = _FETCH_WORKERS.get(repo)
        if worker and worker.is_alive():
            return
        _FETCH_STATES.setdefault(repo, {"enabled": True, "intervalSeconds": fetch_interval_seconds()})
        worker = threading.Thread(target=auto_fetch_loop, args=(repo,), daemon=True)
        _FETCH_WORKERS[repo] = worker
        worker.start()


def default_state_dir() -> Path:
    return Path(os.environ.get("GBP_STATE_DIR", Path.home() / ".local" / "state" / "git-branch-pane"))


def branch_color_state_file() -> Path:
    return default_state_dir() / "branch-colors.json"


def canonical_branch_name(name: str) -> str:
    clean = name.strip()
    for prefix in ("refs/heads/", "refs/remotes/"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]
    if clean.startswith("tag:") or clean == "HEAD" or "/HEAD" in clean or "HEAD ->" in clean:
        return ""
    parts = clean.split("/", 1)
    if len(parts) == 2 and parts[0] in {"origin", "upstream"}:
        clean = parts[1]
    return clean


def load_branch_color_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"repos": {}}
    return data if isinstance(data, dict) else {"repos": {}}


def save_branch_color_state(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def branch_sort_key(date: str, name: str) -> tuple[int, str]:
    try:
        return (-int(date), name.lower())
    except ValueError:
        return (0, name.lower())


def branch_ref_names(repo: str) -> list[str]:
    result = run_git(
        repo,
        ["for-each-ref", "--format=%(committerdate:unix)%00%(refname)", "refs/heads", "refs/remotes"],
        check=False,
    )
    names: dict[str, str] = {}
    for refname in result.stdout.splitlines():
        date, _, refname = refname.partition("\x00")
        name = canonical_branch_name(refname)
        if name:
            names[name] = max(date, names.get(name, ""), key=lambda value: int(value or 0))
    return sorted(names, key=lambda name: branch_sort_key(names[name], name))


def branch_color_assignments(repo: str) -> dict[str, int]:
    active = branch_ref_names(repo)
    repo_key = os.path.abspath(repo)
    state_path = branch_color_state_file()
    with _BRANCH_COLOR_LOCK:
        state = load_branch_color_state(state_path)
        repos = state.setdefault("repos", {})
        if not isinstance(repos, dict):
            repos = {}
            state["repos"] = repos
        repo_state = repos.setdefault(repo_key, {})
        if not isinstance(repo_state, dict):
            repo_state = {}
            repos[repo_key] = repo_state
        stored = repo_state.setdefault("branches", {})
        if not isinstance(stored, dict):
            stored = {}
            repo_state["branches"] = stored

        changed = False
        now = time.time()
        if repo_state.get("schemaVersion") != BRANCH_COLOR_SCHEMA_VERSION:
            stored = {
                branch: entry
                for branch, entry in stored.items()
                if branch in RESERVED_BRANCH_COLORS and isinstance(entry, dict)
            }
            repo_state["branches"] = stored
            repo_state["schemaVersion"] = BRANCH_COLOR_SCHEMA_VERSION
            changed = True

        active_colors: dict[str, int] = {}
        used_colors: set[int] = set()

        for branch in active:
            reserved = RESERVED_BRANCH_COLORS.get(branch)
            if reserved is None:
                continue
            entry = stored.get(branch)
            if not isinstance(entry, dict) or entry.get("color") != reserved:
                stored[branch] = {"color": reserved, "lastAssigned": now}
                changed = True
            active_colors[branch] = reserved
            used_colors.add(reserved)

        pending: list[str] = []
        seen_counts: dict[int, int] = {}
        max_reuse = max(1, (len([branch for branch in active if branch not in RESERVED_BRANCH_COLORS]) + len(BRANCH_COLOR_SEQUENCE) - 1) // len(BRANCH_COLOR_SEQUENCE))
        for branch in active:
            if branch in active_colors:
                continue
            entry = stored.get(branch)
            color = entry.get("color") if isinstance(entry, dict) else None
            if isinstance(color, int) and color in BRANCH_COLOR_SEQUENCE and seen_counts.get(color, 0) < max_reuse:
                active_colors[branch] = color
                used_colors.add(color)
                seen_counts[color] = seen_counts.get(color, 0) + 1
            else:
                pending.append(branch)

        def last_assigned(color: int) -> float:
            values = [
                float(entry.get("lastAssigned", 0))
                for entry in stored.values()
                if isinstance(entry, dict) and entry.get("color") == color
            ]
            return max(values) if values else 0

        color_last_assigned = {color: last_assigned(color) for color in BRANCH_COLOR_SEQUENCE}

        for branch in pending:
            candidates = [
                color for color in BRANCH_COLOR_SEQUENCE if seen_counts.get(color, 0) < max_reuse
            ] or BRANCH_COLOR_SEQUENCE
            color = min(candidates, key=lambda item: (color_last_assigned[item], BRANCH_COLOR_SEQUENCE.index(item)))
            color_last_assigned[color] = now + len(active_colors) / 1000
            stored[branch] = {"color": color, "lastAssigned": color_last_assigned[color]}
            active_colors[branch] = color
            used_colors.add(color)
            seen_counts[color] = seen_counts.get(color, 0) + 1
            changed = True

        if changed:
            save_branch_color_state(state_path, state)
        return active_colors


def status_summary(repo: str) -> dict[str, object]:
    branch = run_git(repo, ["branch", "--show-current"], check=False).stdout.strip()
    head = run_git(repo, ["rev-parse", "--short", "HEAD"], check=False).stdout.strip()
    porcelain = run_git(repo, ["status", "--porcelain=v1", "-b"], check=False).stdout.splitlines()
    return {
        "branch": branch or "(detached)",
        "head": head,
        "changes": [line for line in porcelain if not line.startswith("## ")],
        "statusLine": next((line[3:] for line in porcelain if line.startswith("## ")), ""),
    }


def graph(repo: str, limit: int) -> dict[str, object]:
    pretty = FIELD_SEP.join(["%H", "%h", "%P", "%an", "%ar", "%ad", "%D", "%s"])
    args = [
        "log",
        "--all",
        "--topo-order",
        "--decorate=full",
        "--date=iso-strict",
        f"--max-count={limit}",
        f"--pretty=format:{pretty}",
    ]
    result = run_git(repo, args, check=False)
    if result.returncode != 0:
        return {"rows": [], "error": result.stderr.strip()}

    rows: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        parts = line.split(FIELD_SEP)
        while len(parts) < 8:
            parts.append("")
        full, short, parents, author, rel_date, iso_date, decorations, subject = parts[:8]
        rows.append(
            {
                "kind": "commit",
                "hash": full,
                "short": short,
                "parents": parents.split() if parents else [],
                "author": author,
                "relativeDate": rel_date,
                "isoDate": iso_date,
                "decorations": normalize_decorations(decorations),
                "subject": subject,
                "isMerge": len(parents.split()) > 1 if parents else False,
            }
        )
    return {"rows": layout_rows(rows, branch_color_assignments(repo))}


def preferred_branch_color(row: dict[str, object], branch_colors: dict[str, int]) -> int | None:
    branches = sorted(
        {canonical_branch_name(str(name)) for name in row.get("decorations", [])},
        key=lambda name: (0 if name == "main" else 1 if name == "development" else 2, name.lower()),
    )
    for branch in branches:
        if branch and branch in branch_colors:
            return branch_colors[branch]
    return None


def layout_rows(rows: list[dict[str, object]], branch_colors: dict[str, int] | None = None) -> list[dict[str, object]]:
    """Assign graph lanes and row-local edges for a topo-ordered commit list."""
    active: list[str] = []
    color_by_hash: dict[str, int] = {}
    laid_out: list[dict[str, object]] = []
    max_lane = 0
    next_color = 0
    branch_colors = branch_colors or {}
    preferred_color_by_hash = {
        str(row["hash"]): color
        for row in rows
        if (color := preferred_branch_color(row, branch_colors)) is not None
    }

    def color_for(commit_hash: str, preferred: int | None = None) -> int:
        nonlocal next_color
        if commit_hash not in color_by_hash:
            if preferred is not None:
                color_by_hash[commit_hash] = preferred
            else:
                color_by_hash[commit_hash] = COLOR_SEQUENCE[next_color % len(COLOR_SEQUENCE)]
                next_color += 1
        return color_by_hash[commit_hash]

    for index, row in enumerate(rows):
        commit_hash = str(row["hash"])
        introduced = commit_hash not in active
        if introduced:
            lane = len(active)
            active.append(commit_hash)
        else:
            lane = active.index(commit_hash)

        before = active.copy()
        commit_color = color_for(commit_hash, preferred_color_by_hash.get(commit_hash))
        parents = [str(parent) for parent in row.get("parents", [])]
        after = active.copy()
        after.pop(lane)
        outgoing: list[dict[str, object]] = []

        if parents:
            for parent_index, parent in enumerate(parents):
                preferred = commit_color if parent_index == 0 else preferred_color_by_hash.get(parent)
                color_for(parent, preferred)

            first_parent = parents[0]
            if first_parent in after:
                target = after.index(first_parent)
            else:
                target = min(lane, len(after))
                after.insert(target, first_parent)
            outgoing.append({"to": target, "color": commit_color, "parent": first_parent})

            for extra_index, parent in enumerate(parents[1:], start=1):
                if parent in after:
                    target = after.index(parent)
                else:
                    target = min(lane + extra_index, len(after))
                    after.insert(target, parent)
                outgoing.append({"to": target, "color": color_by_hash[parent], "parent": parent})

        passthrough = []
        for before_lane, active_hash in enumerate(before):
            if active_hash == commit_hash or active_hash not in after:
                continue
            passthrough.append(
                {
                    "from": before_lane,
                    "to": after.index(active_hash),
                    "color": color_by_hash.get(active_hash, color_for(active_hash)),
                    "hash": active_hash,
                }
            )

        active = after
        row_lane_values = [
            lane,
            *[int(edge["to"]) for edge in outgoing],
            *[int(edge["from"]) for edge in passthrough],
            *[int(edge["to"]) for edge in passthrough],
        ]
        row_max_lane = max(row_lane_values)
        max_lane = max(max_lane, row_max_lane)
        row.update(
            {
                "index": index,
                "lane": lane,
                "rowMaxLane": row_max_lane,
                "color": commit_color,
                "dotColor": preferred_color_by_hash.get(commit_hash, commit_color),
                "incoming": not introduced,
                "outgoing": outgoing,
                "passthrough": passthrough,
            }
        )
        laid_out.append(row)

    for row in laid_out:
        row["maxLane"] = max_lane
    return laid_out


def normalize_decorations(value: str) -> list[str]:
    labels: list[str] = []
    for raw in [item.strip() for item in value.split(",") if item.strip()]:
        raw = raw.replace("HEAD -> ", "")
        for prefix in ("refs/heads/", "refs/remotes/", "refs/tags/"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
        labels.append(raw)
    return labels


def branches(repo: str) -> dict[str, object]:
    fmt = FIELD_SEP.join(["%(refname)", "%(objectname:short)", "%(upstream:short)", "%(committerdate:relative)", "%(contents:subject)"])
    result = run_git(repo, ["for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes"], check=False)
    current = run_git(repo, ["branch", "--show-current"], check=False).stdout.strip()
    branch_colors = branch_color_assignments(repo)
    rows = []
    for line in result.stdout.splitlines():
        refname, sha, upstream, date, subject = (line.split(FIELD_SEP) + ["", "", "", "", ""])[:5]
        if refname.endswith("/HEAD"):
            continue
        name = refname.replace("refs/heads/", "").replace("refs/remotes/", "")
        kind = "remote" if refname.startswith("refs/remotes/") else "local"
        rows.append(
            {
                "name": name,
                "refname": refname,
                "kind": kind,
                "sha": sha,
                "upstream": upstream,
                "date": date,
                "subject": subject,
                "current": kind == "local" and name == current,
                "color": branch_colors.get(canonical_branch_name(name)),
            }
        )
    rows.sort(key=lambda row: (row["kind"] != "local", not row["current"], row["name"].lower()))
    return {"branches": rows, "current": current}


def commit_details(repo: str, sha: str) -> dict[str, object]:
    safe_sha = sha.strip()
    show = run_git(
        repo,
        [
            "show",
            "--no-ext-diff",
            "--stat",
            "--format=fuller",
            "--no-renames",
            "--max-count=1",
            safe_sha,
        ],
        check=False,
    )
    contains = run_git(repo, ["branch", "--all", "--contains", safe_sha], check=False)
    return {
        "ok": show.returncode == 0,
        "text": show.stdout if show.returncode == 0 else show.stderr,
        "contains": [line.strip().lstrip("* ").strip() for line in contains.stdout.splitlines() if line.strip()],
    }


def checkout_branch(repo: str, branch: str) -> dict[str, object]:
    result = run_git(repo, ["switch", branch], check=False)
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "status": status_summary(repo) if result.returncode == 0 else None,
    }


def copy_to_clipboard(text: str) -> dict[str, object]:
    commands = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]]
    errors = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"{command[0]} timed out")
            continue
        if result.returncode == 0:
            return {"ok": True, "method": command[0]}
        errors.append(result.stderr.strip() or f"{command[0]} exited {result.returncode}")
    return {"ok": False, "error": "; ".join(errors) or "No clipboard command found"}


def make_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    ports = [port] if port == 0 else [port, *range(port + 1, port + 21)]
    last_error: OSError | None = None
    for candidate in ports:
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
            if port != 0 and getattr(exc, "errno", None) in {errno.EADDRINUSE, 48, 98}:
                continue
            raise
    if last_error:
        raise last_error
    raise OSError("No usable port found")


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Git Branch Pane</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111111;
      --pane: #111111;
      --rail: #111111;
      --line: #2a2f35;
      --text: #eef1f4;
      --muted: #939aa3;
      --hot: #6aa9ff;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 13px;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
      overflow: hidden;
    }
    .stage {
      height: 100vh;
      display: flex;
      align-items: stretch;
      background: #111111;
    }
    .pane {
      width: 100vw;
      min-width: 0;
      height: 100vh;
      background: var(--pane);
      border-right: 1px solid #2c3137;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .graph-wrap {
      position: relative;
      flex: 1;
      overflow: auto;
      scrollbar-color: #3b424a #101214;
    }
    .graph-canvas {
      position: relative;
      min-height: 100%;
      min-width: 100%;
    }
    #graphSvg {
      position: absolute;
      inset: 0 auto auto 0;
      overflow: visible;
      pointer-events: none;
    }
    .commit {
      position: absolute;
      left: 0;
      height: 38px;
      right: 0;
      display: flex;
      align-items: center;
      cursor: default;
    }
    .commit:hover { background: rgba(255,255,255,.055); }
    .dot {
      position: absolute;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      border: 2px solid #0e1012;
      transform: translate(-5px, -5px);
      box-shadow: 0 0 0 1px rgba(255,255,255,.16);
      z-index: 2;
    }
    .dot.merge {
      width: 12px;
      height: 12px;
      transform: translate(-6px, -6px);
    }
    .label {
      position: absolute;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 14px;
      line-height: 21px;
      color: #d8dde3;
    }
    .commit:not(.head-row) .label { color: #a5abb4; font-weight: 500; }
    .head-row .label { color: #f2f5f8; font-weight: 700; }
    .refs {
      display: inline-flex;
      gap: 4px;
      max-width: 220px;
      vertical-align: top;
      margin-right: 5px;
    }
    .ref {
      max-width: 145px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      border-radius: 999px;
      padding: 2px 8px 3px;
      color: #071018;
      background: #9bd1ff;
      font: 13px var(--sans);
      font-weight: 750;
    }
    .empty {
      padding: 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.4;
    }
    .tip {
      position: fixed;
      z-index: 10;
      max-width: min(380px, calc(100vw - 28px));
      display: none;
      border: 0;
      border-radius: 18px;
      padding: 17px 19px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.028)),
        rgba(31,31,31,.72);
      box-shadow: 0 18px 46px rgba(0,0,0,.38);
      -webkit-backdrop-filter: blur(22px);
      backdrop-filter: blur(22px);
      pointer-events: none;
    }
    .tip-title {
      margin-bottom: 12px;
      color: #f4f4f4;
      font-family: var(--sans);
      font-size: 15px;
      font-weight: 650;
      line-height: 1.28;
    }
    .tip-meta {
      color: rgba(232,232,232,.74);
      font: 14px/1.42 var(--sans);
      font-weight: 450;
      overflow-wrap: anywhere;
    }
    .rest {
      flex: 1;
      background: #111111;
      display: none;
    }
    @media (max-width: 520px) {
      .pane { width: 100vw; min-width: 100vw; }
    }
  </style>
</head>
<body>
  <div class="stage">
    <main class="pane">
      <div id="graphWrap" class="graph-wrap">
        <div id="graphCanvas" class="graph-canvas">
          <svg id="graphSvg" xmlns="http://www.w3.org/2000/svg"></svg>
        </div>
      </div>
    </main>
    <div class="rest"></div>
  </div>
  <div id="tip" class="tip"></div>
  <script>
    const state = {
      repo: new URLSearchParams(location.search).get('repo') || '',
      limit: new URLSearchParams(location.search).get('limit') || '1000',
      rows: [],
      branches: [],
      tipTimer: null,
      hoverEvent: null,
      hoverCommit: null,
    };
    const colors = [
      '#4D8DFF',
      '#6EA8FF',
      '#4CC7E8',
      '#2DA6C2',
      '#4FC97A',
      '#36A96B',
      '#7775E6',
      '#9B94F2',
      '#A98BEA',
      '#8D75D8',
      '#CFA64A',
      '#D96F63',
      '#E07AA8',
      '#B88AF0',
      '#7BCFA4',
      '#E0B85A'
    ];
    const rowH = 38;
    const topPad = 18;
    const laneGap = 28;
    const leftPad = 28;
    const $ = (id) => document.getElementById(id);

    function api(path, params = {}) {
      const url = new URL(path, location.href);
      if (state.repo) url.searchParams.set('repo', state.repo);
      Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
      return fetch(url).then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        return data;
      });
    }

    function html(value) {
      return String(value).replace(/[&<>"']/g, (ch) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
    }

    function graphColor(index) {
      return colors[Math.abs(Number(index) || 0) % colors.length];
    }

    function canonicalBranchName(name) {
      const clean = String(name || '').trim();
      if (clean.startsWith('tag:')) return '';
      if (clean === 'HEAD' || clean.includes('/HEAD') || clean.includes('HEAD ->')) return '';
      const slash = clean.indexOf('/');
      if (slash > 0 && ['origin', 'upstream'].includes(clean.slice(0, slash))) return clean.slice(slash + 1);
      return clean;
    }

    function branchColorIndex(name) {
      const canonical = canonicalBranchName(name);
      const branch = state.branches.find((item) => canonicalBranchName(item.name) === canonical && Number.isFinite(item.color));
      return branch ? branch.color : null;
    }

    function colorText(hex) {
      const raw = hex.replace('#', '');
      const r = parseInt(raw.slice(0, 2), 16);
      const g = parseInt(raw.slice(2, 4), 16);
      const b = parseInt(raw.slice(4, 6), 16);
      return (r * .299 + g * .587 + b * .114) > 150 ? '#111417' : '#F1EDE0';
    }

    function branchNamesFor(row) {
      const exact = state.branches.filter((branch) => branch.sha === row.short).map((branch) => branch.name);
      const decorated = (row.decorations || []).filter((name) => !name.startsWith('tag:'));
      return [...new Set([...exact, ...decorated])];
    }

    function linePath(x1, y1, x2, y2) {
      if (x1 === x2) return `M ${x1} ${y1} L ${x2} ${y2}`;
      const dy = y2 - y1;
      const direction = Math.sign(x2 - x1);
      const radius = Math.max(4, Math.min(12, Math.abs(x2 - x1) / 2, Math.abs(dy) / 2));
      const midY = y1 + dy / 2;
      return `M ${x1} ${y1} L ${x1} ${midY - radius} Q ${x1} ${midY} ${x1 + direction * radius} ${midY} L ${x2 - direction * radius} ${midY} Q ${x2} ${midY} ${x2} ${midY + radius} L ${x2} ${y2}`;
    }

    function rowMaxLane(row) {
      if (Number.isFinite(row.rowMaxLane)) return row.rowMaxLane;
      const lanes = [row.lane || 0];
      (row.outgoing || []).forEach((edge) => lanes.push(edge.to || 0));
      (row.passthrough || []).forEach((edge) => lanes.push(edge.from || 0, edge.to || 0));
      return Math.max(0, ...lanes);
    }

    function labelLeftFor(row) {
      return leftPad + (rowMaxLane(row) + 1) * laneGap + 14;
    }

    function renderGraph() {
      const commits = state.rows.filter((row) => row.kind === 'commit');
      if (!commits.length) {
        $('graphCanvas').innerHTML = '<div class="empty">No commits found.</div><svg id="graphSvg" xmlns="http://www.w3.org/2000/svg"></svg>';
        return;
      }
      const layout = commits;
      const maxLane = Math.max(0, ...layout.map((row) => row.maxLane));
      const graphWidth = leftPad + (maxLane + 1) * laneGap + 204;
      const canvasWidth = Math.max(graphWidth, $('graphWrap').clientWidth);
      const height = topPad * 2 + layout.length * rowH;
      const paths = [];

      layout.forEach((row) => {
        const y = topPad + row.index * rowH;
        (row.passthrough || []).forEach((edge) => {
          const x1 = leftPad + edge.from * laneGap;
          const x2 = leftPad + edge.to * laneGap;
          paths.push(`<path d="${linePath(x1, y - rowH / 2, x2, y + rowH / 2)}" stroke="${graphColor(edge.color)}" />`);
        });
        const x = leftPad + row.lane * laneGap;
        if (row.incoming) {
          paths.push(`<path d="M ${x} ${y - rowH / 2} L ${x} ${y}" stroke="${graphColor(row.color)}" />`);
        }
        (row.outgoing || []).forEach((edge) => {
          const x2 = leftPad + edge.to * laneGap;
          paths.push(`<path d="${linePath(x, y, x2, y + rowH / 2)}" stroke="${graphColor(edge.color)}" />`);
        });
      });

      const svg = `<svg id="graphSvg" width="${canvasWidth}" height="${height}" xmlns="http://www.w3.org/2000/svg">
        <g fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${paths.join('')}</g>
      </svg>`;
      const nodes = layout.map((row) => {
        const y = topPad + row.index * rowH;
        const x = leftPad + row.lane * laneGap;
        const names = branchNamesFor(row);
        const isHead = names.length > 0;
        const color = graphColor(Number.isFinite(row.dotColor) ? row.dotColor : row.color);
        const refs = names.slice(0, 2).map((name) => {
          const refColor = graphColor(branchColorIndex(name) ?? (Number.isFinite(row.dotColor) ? row.dotColor : row.color));
          return `<span class="ref" style="background:${refColor};color:${colorText(refColor)}">${html(name)}</span>`;
        }).join('');
        const text = isHead ? refs : html(row.subject);
        return `<div class="commit ${isHead ? 'head-row' : ''}" data-sha="${html(row.hash)}" style="top:${y - rowH / 2}px">
          <span class="dot ${row.isMerge ? 'merge' : ''}" style="left:${x}px;top:${rowH / 2}px;background:${color}"></span>
          <span class="label" style="left:${labelLeftFor(row)}px;right:8px">${text}</span>
        </div>`;
      }).join('');

      $('graphCanvas').style.width = `${canvasWidth}px`;
      $('graphCanvas').style.height = `${height}px`;
      $('graphCanvas').innerHTML = svg + nodes;
      document.querySelectorAll('.commit[data-sha]').forEach((node) => {
        [node.querySelector('.dot'), node.querySelector('.label')].forEach((target) => {
          target.addEventListener('mouseenter', scheduleTip);
          target.addEventListener('mousemove', trackTip);
          target.addEventListener('mouseleave', cancelTip);
        });
        node.addEventListener('click', (event) => selectCommit(node.dataset.sha, event));
      });
    }

    function rowForSha(sha) {
      return state.rows.find((row) => row.hash === sha);
    }

    function detailLines(row) {
      const refs = branchNamesFor(row);
      const lines = [
        row.subject,
        `hash: ${row.hash}`,
        `author: ${row.author}`,
        `date: ${row.relativeDate}`,
      ];
      if (refs.length) lines.push(`branches: ${refs.join(', ')}`);
      if (row.isMerge) lines.push('merge: yes');
      if ((row.parents || []).length) lines.push(`parents: ${row.parents.map((parent) => parent.slice(0, 8)).join(', ')}`);
      return lines;
    }

    function detailHtml(row) {
      const lines = detailLines(row);
      return `<div class="tip-title">${html(lines[0])}</div>
        <div class="tip-meta">${lines.slice(1).map(html).join('<br>')}</div>`;
    }

    async function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (err) {
        }
      }
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.left = '-9999px';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (ok) return true;
      const response = await fetch('/api/copy', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({ text })
      });
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || 'copy failed');
      return true;
    }

    async function selectCommit(sha, event) {
      clearTimeout(state.tipTimer);
      state.tipTimer = null;
      const row = rowForSha(sha);
      if (!row || !branchNamesFor(row).length) return;
      try {
        await copyText(detailLines(row).join('\n'));
        $('tip').innerHTML = `${detailHtml(row)}<div class="tip-meta"><br>copied</div>`;
        $('tip').style.display = 'block';
        if (event) moveTip(event);
      } catch (err) {
        $('tip').innerHTML = `${detailHtml(row)}<div class="tip-meta"><br>copy failed</div>`;
        $('tip').style.display = 'block';
        if (event) moveTip(event);
      }
    }

    function scheduleTip(event) {
      clearTimeout(state.tipTimer);
      state.hoverEvent = event;
      state.hoverCommit = event.currentTarget.closest('.commit');
      state.tipTimer = setTimeout(() => showTip(), 500);
    }

    function trackTip(event) {
      state.hoverEvent = event;
      if ($('tip').style.display === 'block') moveTip(event);
    }

    function showTip() {
      const row = rowForSha(state.hoverCommit?.dataset.sha);
      if (!row) return;
      $('tip').innerHTML = detailHtml(row);
      $('tip').style.display = 'block';
      if (state.hoverEvent) moveTip(state.hoverEvent);
    }

    function cancelTip() {
      clearTimeout(state.tipTimer);
      state.tipTimer = null;
      state.hoverEvent = null;
      state.hoverCommit = null;
      hideTip();
    }

    function moveTip(event) {
      const tip = $('tip');
      const pad = 14;
      const nextToCursor = event.clientX + pad;
      const leftOfCursor = event.clientX - tip.offsetWidth - pad;
      const belowCursor = event.clientY + pad;
      const aboveCursor = event.clientY - tip.offsetHeight - pad;
      const x = nextToCursor + tip.offsetWidth <= window.innerWidth - pad ? nextToCursor : leftOfCursor;
      let y = belowCursor;
      if (belowCursor + tip.offsetHeight > window.innerHeight - pad) {
        y = aboveCursor >= pad ? aboveCursor : window.innerHeight - tip.offsetHeight - pad;
      }
      tip.style.left = `${Math.max(pad, x)}px`;
      tip.style.top = `${Math.max(pad, y)}px`;
    }

    function hideTip() {
      $('tip').style.display = 'none';
    }

    async function load() {
      try {
        const [repo, graphData, branchData] = await Promise.all([
          api('/api/repo'),
          api('/api/graph', { limit: state.limit }),
          api('/api/branches')
        ]);
        state.repo = repo.root;
        if (!new URLSearchParams(location.search).get('repo')) {
          history.replaceState(null, '', `?repo=${encodeURIComponent(state.repo)}`);
        }
        state.rows = graphData.rows || [];
        state.branches = branchData.branches || [];
        renderGraph();
      } catch (err) {
        $('graphCanvas').innerHTML = `<div class="empty">${html(err.message)}</div><svg id="graphSvg" xmlns="http://www.w3.org/2000/svg"></svg>`;
      }
    }

    document.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'r') {
        event.preventDefault();
        load();
      }
    });
    window.addEventListener('resize', () => {
      if (state.rows.length) renderGraph();
    });
    setInterval(load, 15000);
    load();
  </script>
</body>
</html>
"""


class PaneHandler(BaseHTTPRequestHandler):
    server_version = "GitBranchPane/1.0"

    def do_GET(self) -> None:
        path, params = parse_query(self.path)
        if path == "/":
            self.send_text(HTML, "text/html; charset=utf-8")
            return
        if path == "/api/repo":
            self.json_repo(params)
            return
        if path == "/api/graph":
            self.json_graph(params)
            return
        if path == "/api/branches":
            self.json_branches(params)
            return
        if path == "/api/commit":
            self.json_commit(params)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/copy":
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(copy_to_clipboard(str(body.get("text", ""))))
            return
        if path != "/api/checkout":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        root = self.resolve_repo_from_value(body.get("repo", ""))
        if not root:
            return
        branch = str(body.get("branch", "")).strip()
        if not branch or branch.startswith("-"):
            self.send_json({"ok": False, "stderr": "Invalid branch"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(checkout_branch(root, branch))

    def json_repo(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
        self.send_json(
            {
                "root": root,
                "name": os.path.basename(root),
                "status": status_summary(root),
                "freshness": fetch_state(root),
            }
        )

    def json_graph(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
        ensure_fetch_worker(root)
        limit = max(25, min(3000, int(first(params, "limit", "500") or "500")))
        self.send_json(graph(root, limit))

    def json_branches(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
        ensure_fetch_worker(root)
        self.send_json(branches(root))

    def json_commit(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
        sha = first(params, "sha")
        if not sha:
            self.send_json({"error": "Missing sha"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(commit_details(root, sha))

    def resolve_repo(self, params: dict[str, list[str]]) -> str | None:
        return self.resolve_repo_from_value(first(params, "repo", self.server.default_repo))  # type: ignore[attr-defined]

    def resolve_repo_from_value(self, value: str) -> str | None:
        ok, root_or_error = git_ok(value or self.server.default_repo)  # type: ignore[attr-defined]
        if not ok:
            self.send_json({"error": root_or_error}, HTTPStatus.BAD_REQUEST)
            return None
        return root_or_error

    def send_text(self, text: str, content_type: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        if not getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local interactive Git branch graph pane.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 git_branch_pane.py .
              python3 git_branch_pane.py ~/code/my-repo --port 8765
              ssh -L 8765:127.0.0.1:8765 user@host
            """
        ),
    )
    parser.add_argument("repo", nargs="?", default=".", help="Repository path to show")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Keep 127.0.0.1 unless you know you want LAN access.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. If busy, the next available port is used.")
    parser.add_argument("--open", action="store_true", help="Open the pane URL in the default browser")
    parser.add_argument("--quiet", action="store_true", help="Suppress access logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ok, root_or_error = git_ok(args.repo)
    if not ok:
        print(root_or_error, file=sys.stderr)
        return 2
    server = make_server(args.host, args.port, PaneHandler)
    server.default_repo = root_or_error  # type: ignore[attr-defined]
    server.quiet = args.quiet  # type: ignore[attr-defined]
    actual_host, actual_port = server.server_address[:2]
    url_host = "127.0.0.1" if actual_host in {"0.0.0.0", ""} else actual_host
    url = f"http://{url_host}:{actual_port}/?repo={urllib.parse.quote(root_or_error)}"
    print(f"Git Branch Pane: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
