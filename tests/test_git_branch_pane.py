import json
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

import git_branch_pane


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class GitBranchPaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.assertEqual(len(git_branch_pane.GRAPH_COLORS), 12)
        self.assertEqual(set(row["color"] for row in commits), set(range(12)))

    def test_branch_listing_marks_current(self):
        data = git_branch_pane.branches(str(self.repo))
        names = {row["name"]: row for row in data["branches"]}
        self.assertIn("main", names)
        self.assertIn("feature", names)
        self.assertTrue(names["main"]["current"])

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
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
