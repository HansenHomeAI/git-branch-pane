#!/usr/bin/env python3
"""Local Git branch graph pane.

Run:
    python3 git_branch_pane.py /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FIELD_SEP = "\x1f"
GRAPH_COLORS = [
    "#149ce6",
    "#25d933",
    "#ff3333",
    "#b425e8",
    "#f49b0b",
    "#d624b8",
    "#90d617",
    "#00c2b2",
    "#f6d13a",
    "#ff7a59",
]


def run_git(repo: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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
    return {"rows": layout_rows(rows)}


def layout_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Assign graph lanes and row-local edges for a topo-ordered commit list."""
    active: list[str] = []
    color_by_hash: dict[str, int] = {}
    laid_out: list[dict[str, object]] = []
    max_lane = 0

    def color_for(commit_hash: str, preferred: int | None = None) -> int:
        if commit_hash not in color_by_hash:
            color_by_hash[commit_hash] = preferred if preferred is not None else len(color_by_hash) % len(GRAPH_COLORS)
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
        commit_color = color_for(commit_hash)
        parents = [str(parent) for parent in row.get("parents", [])]
        after = active.copy()
        after.pop(lane)
        outgoing: list[dict[str, object]] = []

        if parents:
            for parent_index, parent in enumerate(parents):
                preferred = commit_color if parent_index == 0 else None
                color_for(parent, preferred)

            first_parent = parents[0]
            if first_parent in after:
                target = after.index(first_parent)
            else:
                target = min(lane, len(after))
                after.insert(target, first_parent)
            outgoing.append({"to": target, "color": color_by_hash[first_parent], "parent": first_parent})

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
        lane_values = [lane, len(active) - 1, *[int(edge["to"]) for edge in outgoing], *[int(edge["to"]) for edge in passthrough]]
        max_lane = max(max_lane, *lane_values)
        row.update(
            {
                "index": index,
                "lane": lane,
                "color": commit_color,
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


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Git Branch Pane</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0c0e;
      --pane: #151719;
      --rail: #101214;
      --line: #2a2f35;
      --text: #eef1f4;
      --muted: #939aa3;
      --hot: #6aa9ff;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      overflow: hidden;
    }
    .stage {
      height: 100vh;
      display: flex;
      align-items: stretch;
      background: #090a0b;
    }
    .pane {
      width: min(360px, 100vw);
      min-width: min(360px, 100vw);
      height: 100vh;
      background: var(--pane);
      border-right: 1px solid #2c3137;
      box-shadow: 12px 0 36px rgba(0,0,0,.2);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .top {
      min-height: 34px;
      display: flex;
      align-items: center;
      padding: 6px 8px;
      background: var(--rail);
      border-bottom: 1px solid var(--line);
    }
    .repo-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 800;
    }
    .repo-meta {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font: 10px var(--sans);
    }
    .heads {
      min-height: 32px;
      max-height: 62px;
      display: flex;
      gap: 6px;
      align-items: flex-start;
      flex-wrap: wrap;
      overflow: hidden;
      padding: 6px 8px;
      background: #111417;
      border-bottom: 1px solid var(--line);
    }
    .head {
      max-width: 190px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      border: 1px solid #343b43;
      border-left-width: 4px;
      border-radius: 6px;
      padding: 3px 7px 4px;
      color: #e8edf2;
      background: #1b1f23;
      font-size: 11px;
      font-weight: 750;
      cursor: pointer;
    }
    .head.current { background: #203428; border-color: #3d684b; }
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
      height: 28px;
      right: 0;
      display: flex;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,.035);
      cursor: default;
    }
    .commit:hover { background: rgba(255,255,255,.055); }
    .commit.selected { background: rgba(106,169,255,.15); }
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
      font-size: 11px;
      line-height: 18px;
      color: #d8dde3;
    }
    .commit:not(.head-row) .label { color: #8d949d; }
    .head-row .label { color: #f2f5f8; font-weight: 700; }
    .refs {
      display: inline-flex;
      gap: 4px;
      max-width: 190px;
      vertical-align: top;
      margin-right: 5px;
    }
    .ref {
      max-width: 145px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      border-radius: 5px;
      padding: 1px 5px 2px;
      color: #071018;
      background: #9bd1ff;
      font: 10px var(--mono);
      font-weight: 800;
    }
    .empty {
      padding: 18px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .tip {
      position: fixed;
      z-index: 10;
      max-width: 360px;
      display: none;
      border: 1px solid #3b434c;
      border-radius: 7px;
      padding: 8px 9px;
      background: #191c20;
      box-shadow: 0 16px 44px rgba(0,0,0,.35);
      pointer-events: none;
    }
    .tip-title {
      margin-bottom: 5px;
      color: #f3f6f9;
      font-size: 13px;
      font-weight: 800;
      line-height: 1.3;
    }
    .tip-meta {
      color: var(--muted);
      font: 11px/1.45 var(--mono);
    }
    .rest {
      flex: 1;
      background:
        linear-gradient(90deg, rgba(255,255,255,.025), transparent 34px),
        #0b0c0e;
    }
    @media (max-width: 520px) {
      .pane { width: 100vw; min-width: 100vw; }
      .rest { display: none; }
    }
  </style>
</head>
<body>
  <div class="stage">
    <main class="pane">
      <div class="top">
        <div>
          <div id="repoName" class="repo-name">Git Branch Pane</div>
          <div id="repoMeta" class="repo-meta">Loading...</div>
        </div>
      </div>
      <div id="heads" class="heads"></div>
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
      selected: null,
    };
    const colors = ['#149ce6','#25d933','#ff3333','#a927e8','#f49b0b','#d624b8','#90d617','#00c2b2','#f6d13a','#ff7a59'];
    const rowH = 28;
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

    function branchNamesFor(row) {
      const exact = state.branches.filter((branch) => branch.sha === row.short).map((branch) => branch.name);
      const decorated = (row.decorations || []).filter((name) => !name.startsWith('tag:'));
      return [...new Set([...exact, ...decorated])];
    }

    function renderHeads(layout) {
      const rowByShort = new Map(layout.map((row) => [row.short, row]));
      $('heads').innerHTML = state.branches.map((branch) => {
        const row = rowByShort.get(branch.sha);
        const color = graphColor(row ? row.color : 0);
        const cls = branch.current ? 'head current' : 'head';
        const title = `${branch.name}\n${branch.sha}\n${branch.subject || ''}`;
        return `<span class="${cls}" style="border-left-color:${color}" title="${html(title)}" data-head="${html(branch.name)}">${html(branch.name)}</span>`;
      }).join('');
      document.querySelectorAll('[data-head]').forEach((node) => {
        node.addEventListener('click', () => {
          const branch = state.branches.find((item) => item.name === node.dataset.head);
          const row = branch && state.rows.find((item) => item.short === branch.sha);
          if (row) {
            $('graphWrap').scrollTop = Math.max(0, topPad + row.index * rowH - 56);
            selectCommit(row.hash);
          }
        });
      });
    }

    function linePath(x1, y1, x2, y2) {
      if (x1 === x2) return `M ${x1} ${y1} L ${x2} ${y2}`;
      const bend = Math.max(14, Math.abs(x2 - x1) * .7);
      return `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`;
    }

    function renderGraph() {
      const commits = state.rows.filter((row) => row.kind === 'commit');
      if (!commits.length) {
        $('graphCanvas').innerHTML = '<div class="empty">No commits found.</div><svg id="graphSvg" xmlns="http://www.w3.org/2000/svg"></svg>';
        return;
      }
      const layout = commits;
      renderHeads(layout);
      const maxLane = Math.max(0, ...layout.map((row) => row.maxLane));
      const graphWidth = leftPad + (maxLane + 1) * laneGap + 204;
      const height = topPad * 2 + layout.length * rowH;
      const labelLeft = leftPad + (maxLane + 1) * laneGap + 14;
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

      const svg = `<svg id="graphSvg" width="${graphWidth}" height="${height}" xmlns="http://www.w3.org/2000/svg">
        <g fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${paths.join('')}</g>
      </svg>`;
      const nodes = layout.map((row) => {
        const y = topPad + row.index * rowH;
        const x = leftPad + row.lane * laneGap;
        const names = branchNamesFor(row);
        const isHead = names.length > 0;
        const selected = state.selected === row.hash ? ' selected' : '';
        const color = graphColor(row.color);
        const refs = names.slice(0, 2).map((name) => `<span class="ref" style="background:${color}">${html(name)}</span>`).join('');
        const text = isHead ? refs : html(row.subject);
        const title = `${row.subject}\n${row.short}\n${row.author} - ${row.relativeDate}\n${names.join(', ')}`;
        return `<div class="commit ${isHead ? 'head-row' : ''}${selected}" data-sha="${html(row.hash)}" style="top:${y - rowH / 2}px">
          <span class="dot ${row.isMerge ? 'merge' : ''}" style="left:${x}px;top:${rowH / 2}px;background:${color}"></span>
          <span class="label" style="left:${labelLeft}px;right:8px" title="${html(title)}">${text}</span>
        </div>`;
      }).join('');

      $('graphCanvas').style.width = `${graphWidth}px`;
      $('graphCanvas').style.height = `${height}px`;
      $('graphCanvas').innerHTML = svg + nodes;
      document.querySelectorAll('.commit[data-sha]').forEach((node) => {
        node.addEventListener('mouseenter', showTip);
        node.addEventListener('mousemove', moveTip);
        node.addEventListener('mouseleave', hideTip);
        node.addEventListener('click', () => selectCommit(node.dataset.sha));
      });
    }

    function rowForSha(sha) {
      return state.rows.find((row) => row.hash === sha);
    }

    async function selectCommit(sha) {
      state.selected = sha;
      renderGraph();
      const row = rowForSha(sha);
      if (row) {
        $('repoMeta').textContent = `${row.short}  ${row.subject}`;
      }
    }

    function showTip(event) {
      const row = rowForSha(event.currentTarget.dataset.sha);
      if (!row) return;
      const refs = branchNamesFor(row);
      $('tip').innerHTML = `<div class="tip-title">${html(row.subject)}</div>
        <div class="tip-meta">${html(row.short)}${row.isMerge ? '  merge' : ''}<br>${html(row.author)} - ${html(row.relativeDate)}<br>${html(refs.join(', ') || 'commit')}</div>`;
      $('tip').style.display = 'block';
      moveTip(event);
    }

    function moveTip(event) {
      const tip = $('tip');
      const pad = 14;
      const x = Math.min(window.innerWidth - tip.offsetWidth - pad, event.clientX + pad);
      const y = Math.min(window.innerHeight - tip.offsetHeight - pad, event.clientY + pad);
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
        $('repoName').textContent = repo.name;
        $('repoMeta').textContent = `${repo.status.branch}${repo.status.changes.length ? ' - changed' : ''}`;
        state.rows = graphData.rows || [];
        state.branches = branchData.branches || [];
        renderGraph();
      } catch (err) {
        $('repoMeta').textContent = err.message;
        $('graphCanvas').innerHTML = `<div class="empty">${html(err.message)}</div><svg id="graphSvg" xmlns="http://www.w3.org/2000/svg"></svg>`;
      }
    }

    document.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'r') {
        event.preventDefault();
        load();
      }
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
        if urllib.parse.urlparse(self.path).path != "/api/checkout":
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
        self.send_json({"root": root, "name": os.path.basename(root), "status": status_summary(root)})

    def json_graph(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
        limit = max(25, min(3000, int(first(params, "limit", "500") or "500")))
        self.send_json(graph(root, limit))

    def json_branches(self, params: dict[str, list[str]]) -> None:
        root = self.resolve_repo(params)
        if not root:
            return
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
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--quiet", action="store_true", help="Suppress access logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ok, root_or_error = git_ok(args.repo)
    if not ok:
        print(root_or_error, file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), PaneHandler)
    server.default_repo = root_or_error  # type: ignore[attr-defined]
    server.quiet = args.quiet  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/?repo={urllib.parse.quote(root_or_error)}"
    print(f"Git Branch Pane: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
