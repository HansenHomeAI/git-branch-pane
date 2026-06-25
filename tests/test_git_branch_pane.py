import json
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import git_branch_pane


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class GitBranchPaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict("os.environ", {"GBP_STATE_DIR": self.state_tmp.name})
        self.env.start()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "codex@example.test")
        git(self.repo, "config", "user.name", "Codex Test")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "switch", "-c", "feature")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-m", "feature work")
        git(self.repo, "switch", "main")
        (self.repo / "main.txt").write_text("main\n", encoding="utf-8")
        git(self.repo, "add", "main.txt")
        git(self.repo, "commit", "-m", "main work")
        git(self.repo, "merge", "--no-ff", "feature", "-m", "merge feature")

    def tearDown(self):
        self.env.stop()
        self.state_tmp.cleanup()
        self.tmp.cleanup()

    def test_graph_includes_branches_and_merge(self):
        data = git_branch_pane.graph(str(self.repo), 50)
        commits = [row for row in data["rows"] if row["kind"] == "commit"]
        subjects = [row["subject"] for row in commits]
        self.assertIn("merge feature", subjects)
        self.assertTrue(any(row["isMerge"] for row in commits))
        self.assertTrue(any("feature" in deco for row in commits for deco in row["decorations"]))

    def test_layout_tracks_merge_parent_edges(self):
        data = git_branch_pane.graph(str(self.repo), 50)
        commits = [row for row in data["rows"] if row["kind"] == "commit"]
        merge = next(row for row in commits if row["subject"] == "merge feature")
        self.assertEqual(len(merge["outgoing"]), 2)
        self.assertFalse(merge["incoming"])
        self.assertGreaterEqual(merge["maxLane"], 1)
        for row in commits:
            self.assertIsInstance(row["lane"], int)
            self.assertIsInstance(row["rowMaxLane"], int)
            self.assertLessEqual(row["lane"], row["maxLane"])
            self.assertLessEqual(row["rowMaxLane"], row["maxLane"])
            for edge in [*row["outgoing"], *row["passthrough"]]:
                self.assertGreaterEqual(edge["to"], 0)
                self.assertLessEqual(edge["to"], row["maxLane"])

    def test_layout_handles_repeated_splits_and_merges(self):
        git(self.repo, "switch", "-c", "second")
        (self.repo / "second.txt").write_text("second\n", encoding="utf-8")
        git(self.repo, "add", "second.txt")
        git(self.repo, "commit", "-m", "second work")
        git(self.repo, "switch", "main")
        (self.repo / "main.txt").write_text("main\nmore\n", encoding="utf-8")
        git(self.repo, "add", "main.txt")
        git(self.repo, "commit", "-m", "main follow-up")
        git(self.repo, "merge", "--no-ff", "second", "-m", "merge second")

        data = git_branch_pane.graph(str(self.repo), 50)
        commits = [row for row in data["rows"] if row["kind"] == "commit"]
        merges = [row for row in commits if row["isMerge"]]
        self.assertGreaterEqual(len(merges), 2)
        self.assertTrue(any(row["rowMaxLane"] < row["maxLane"] for row in commits))
        for row in commits:
            for edge in row["outgoing"]:
                self.assertTrue(any(parent_row["hash"] == edge["parent"] for parent_row in commits[row["index"] + 1 :]))
            for edge in row["passthrough"]:
                self.assertGreaterEqual(edge["from"], 0)
                self.assertGreaterEqual(edge["to"], 0)

    def test_layout_can_emit_all_palette_colors(self):
        self.assertEqual(
            git_branch_pane.GRAPH_COLORS,
            [
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
            ],
        )
        rows = [
            {
                "kind": "commit",
                "hash": f"{index:040x}",
                "short": f"{index:07x}",
                "parents": [],
                "decorations": [],
                "subject": f"independent {index}",
                "isMerge": False,
            }
            for index in range(len(git_branch_pane.GRAPH_COLORS))
        ]

        commits = git_branch_pane.layout_rows(rows)
        self.assertEqual(len(git_branch_pane.GRAPH_COLORS), 16)
        self.assertEqual(set(row["color"] for row in commits), set(range(16)))

    def test_html_palette_matches_server_palette(self):
        for color in git_branch_pane.GRAPH_COLORS:
            self.assertIn(f"'{color}'", git_branch_pane.HTML)

    def test_commit_labels_do_not_use_native_title_tooltips(self):
        self.assertNotIn('class="label" style="left:${labelLeftFor(row)}px;right:8px" title=', git_branch_pane.HTML)

    def test_reserved_branch_colors_are_fixed(self):
        git(self.repo, "switch", "-c", "development")
        (self.repo / "development.txt").write_text("development\n", encoding="utf-8")
        git(self.repo, "add", "development.txt")
        git(self.repo, "commit", "-m", "development work")

        data = git_branch_pane.graph(str(self.repo), 50)
        rows_by_branch = {
            decoration: row
            for row in data["rows"]
            for decoration in row["decorations"]
            if decoration in {"main", "development"}
        }

        self.assertEqual(rows_by_branch["main"]["dotColor"], 0)
        self.assertEqual(rows_by_branch["development"]["dotColor"], 6)

    def test_dynamic_branch_color_sticks_after_new_branch(self):
        first = git_branch_pane.graph(str(self.repo), 50)
        feature_color = next(row["color"] for row in first["rows"] if "feature" in row["decorations"])

        git(self.repo, "switch", "-c", "second")
        (self.repo / "second.txt").write_text("second\n", encoding="utf-8")
        git(self.repo, "add", "second.txt")
        git(self.repo, "commit", "-m", "second work")

        second = git_branch_pane.graph(str(self.repo), 50)
        next_feature_color = next(row["color"] for row in second["rows"] if "feature" in row["decorations"])

        self.assertEqual(next_feature_color, feature_color)

    def test_branch_color_rotation_does_not_collapse_after_palette_wrap(self):
        for index in range(14):
            git(self.repo, "switch", "main")
            branch = f"branch-{index:02d}"
            git(self.repo, "switch", "-c", branch)
            (self.repo / f"{branch}.txt").write_text(f"{branch}\n", encoding="utf-8")
            git(self.repo, "add", f"{branch}.txt")
            git(self.repo, "commit", "-m", f"{branch} work")

        data = git_branch_pane.branches(str(self.repo))
        colors = {
            row["name"]: row["color"]
            for row in data["branches"]
            if row["name"].startswith("branch-")
        }

        self.assertEqual(len(colors), 14)
        self.assertGreaterEqual(len(set(colors.values())), len(git_branch_pane.BRANCH_COLOR_SEQUENCE))

    def test_branch_listing_marks_current(self):
        data = git_branch_pane.branches(str(self.repo))
        names = {row["name"]: row for row in data["branches"]}
        self.assertIn("main", names)
        self.assertIn("feature", names)
        self.assertTrue(names["main"]["current"])
        self.assertEqual(names["main"]["color"], 0)

    def test_fetch_repo_once_gets_new_remote_branch_and_clears_cache(self):
        with tempfile.TemporaryDirectory() as remote_tmp:
            remote = Path(remote_tmp) / "remote.git"
            clone = Path(remote_tmp) / "clone"
            run("git", "init", "--bare", str(remote))
            git(self.repo, "remote", "add", "origin", str(remote))
            git(self.repo, "push", "-u", "origin", "main")
            run("git", "clone", str(remote), str(clone))
            git(clone, "config", "user.email", "codex@example.test")
            git(clone, "config", "user.name", "Codex Test")
            git(clone, "switch", "-c", "remote-only")
            (clone / "remote-only.txt").write_text("remote only\n", encoding="utf-8")
            git(clone, "add", "remote-only.txt")
            git(clone, "commit", "-m", "remote only")
            git(clone, "push", "-u", "origin", "remote-only")

            before = {row["name"] for row in git_branch_pane.branches(str(self.repo))["branches"]}
            state = git_branch_pane.fetch_repo_once(str(self.repo))
            after = {row["name"] for row in git_branch_pane.branches(str(self.repo))["branches"]}

        self.assertNotIn("origin/remote-only", before)
        self.assertIn("origin/remote-only", after)
        self.assertEqual(state["lastError"], "")
        self.assertIn("lastSuccessUnix", state)

    def test_http_endpoints(self):
        server = git_branch_pane.ThreadingHTTPServer(("127.0.0.1", 0), git_branch_pane.PaneHandler)
        server.default_repo = str(self.repo)
        server.quiet = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            url = base + "/api/graph?" + urllib.parse.urlencode({"repo": str(self.repo), "limit": 20})
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("rows", payload)
            self.assertTrue(any(row.get("subject") == "merge feature" for row in payload["rows"]))
            url = base + "/api/repo?" + urllib.parse.urlencode({"repo": str(self.repo)})
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("freshness", payload)
            self.assertTrue(payload["freshness"]["enabled"])
            self.assertEqual(payload["freshness"]["intervalSeconds"], git_branch_pane.GIT_FETCH_INTERVAL_SECONDS)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
