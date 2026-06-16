import os
import unittest
from unittest import mock

import gbp_launcher


class LauncherArgumentTests(unittest.TestCase):
    def test_parse_preserves_repo_options(self):
        mode, args = gbp_launcher.parse_args([".", "--host", "127.0.0.1", "--port", "8765"])

        self.assertEqual(mode, "daemon")
        self.assertEqual(args, [".", "--host", "127.0.0.1", "--port", "8765"])

    def test_daemon_env_does_not_override_status_or_stop(self):
        with mock.patch.dict(os.environ, {"GBP_DAEMON": "0"}):
            self.assertEqual(gbp_launcher.parse_args(["--status"])[0], "status")
            self.assertEqual(gbp_launcher.parse_args(["--stop"])[0], "stop")

    def test_daemon_env_switches_default_start_to_foreground(self):
        with mock.patch.dict(os.environ, {"GBP_DAEMON": "0"}):
            mode, args = gbp_launcher.parse_args([])

        self.assertEqual(mode, "foreground")
        self.assertEqual(args, ["."])


if __name__ == "__main__":
    unittest.main()
