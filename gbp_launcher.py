#!/usr/bin/env python3
"""Cross-platform launcher for the Git Branch Pane server."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("GBP_STATE_DIR", Path.home() / ".local" / "state" / "git-branch-pane"))


def default_app_py() -> Path:
    env_path = os.environ.get("GBP_APP_PY")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().with_name("git_branch_pane.py")


def is_windows() -> bool:
    return os.name == "nt"


def hidden_subprocess_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {"stdin": subprocess.DEVNULL}
    if is_windows():
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if is_windows():
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int, force: bool = False) -> None:
    if pid <= 0:
        return
    if is_windows():
        process_terminate = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(process_terminate, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


class Launcher:
    def __init__(self) -> None:
        self.state_dir = default_state_dir()
        self.pid_file = self.state_dir / "server.pid"
        self.url_file = self.state_dir / "server.url"
        self.repo_file = self.state_dir / "server.repo"
        self.log_file = self.state_dir / "server.log"
        self.app_py = default_app_py()

    def read_pid(self) -> int | None:
        try:
            raw = self.pid_file.read_text(encoding="utf-8").strip()
            return int(raw) if raw.isdigit() else None
        except OSError:
            return None

    def running_pid(self) -> int | None:
        pid = self.read_pid()
        if pid and pid_alive(pid):
            return pid
        return None

    def stop(self) -> int:
        pid = self.running_pid()
        if not pid:
            print("Git Branch Pane is not running.")
            self.pid_file.unlink(missing_ok=True)
            return 0

        try:
            terminate_pid(pid)
        except OSError:
            pass
        for _ in range(3):
            if not pid_alive(pid):
                break
            time.sleep(1)
        if pid_alive(pid):
            try:
                terminate_pid(pid, force=True)
            except OSError:
                pass
        print(f"Stopped Git Branch Pane ({pid}).")
        self.pid_file.unlink(missing_ok=True)
        return 0

    def status(self) -> int:
        pid = self.running_pid()
        if pid:
            print(f"Git Branch Pane is running ({pid}).")
            if self.url_file.exists():
                print(f"URL: {self.url_file.read_text(encoding='utf-8').strip()}")
            if self.repo_file.exists():
                print(f"Repo: {self.repo_file.read_text(encoding='utf-8').strip()}")
            print(f"Log: {self.log_file}")
            return 0
        print("Git Branch Pane is not running.")
        if self.url_file.exists():
            print(f"Last URL: {self.url_file.read_text(encoding='utf-8').strip()}")
        print(f"Log: {self.log_file}")
        return 1

    def repo_display(self, repo_args: list[str]) -> str:
        repo = "." if not repo_args or repo_args[0].startswith("--") else repo_args[0]
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--show-toplevel"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
        return result.stdout.strip() if result.returncode == 0 else str(Path(repo).resolve())

    def command(self, repo_args: list[str], quiet: bool, open_browser: bool) -> list[str]:
        cmd = [sys.executable, str(self.app_py), *repo_args]
        if quiet:
            cmd.append("--quiet")
        if open_browser:
            cmd.append("--open")
        return cmd

    def foreground(self, repo_args: list[str], open_browser: bool) -> int:
        return subprocess.call(self.command(repo_args, quiet=False, open_browser=open_browser))

    def start_detached(self, repo_args: list[str], open_browser: bool) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.running_pid():
            self.stop_quietly()
        self.pid_file.unlink(missing_ok=True)
        self.url_file.unlink(missing_ok=True)
        self.repo_file.write_text(f"{self.repo_display(repo_args)}\n", encoding="utf-8")
        self.log_file.write_text("", encoding="utf-8")

        with self.log_file.open("ab", buffering=0) as log_handle, open(os.devnull, "rb", buffering=0) as stdin_handle:
            kwargs = {
                "stdin": stdin_handle,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "close_fds": True,
            }
            if is_windows():
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(self.command(repo_args, quiet=True, open_browser=open_browser), **kwargs)

        self.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
        url = self.wait_for_url(process.pid)
        if not url:
            print("Git Branch Pane failed to start. Log:", file=sys.stderr)
            print(self.log_file.read_text(encoding="utf-8", errors="replace")[:4000], file=sys.stderr)
            self.pid_file.unlink(missing_ok=True)
            return 1

        self.url_file.write_text(f"{url}\n", encoding="utf-8")
        print(f"Started Git Branch Pane ({process.pid}).")
        print(f"URL: {url}")
        print("Stop with: gbp --stop")
        return 0

    def stop_quietly(self) -> None:
        pid = self.running_pid()
        if not pid:
            self.pid_file.unlink(missing_ok=True)
            return
        try:
            terminate_pid(pid)
        except OSError:
            pass
        for _ in range(3):
            if not pid_alive(pid):
                break
            time.sleep(1)
        if pid_alive(pid):
            try:
                terminate_pid(pid, force=True)
            except OSError:
                pass
        self.pid_file.unlink(missing_ok=True)

    def wait_for_url(self, pid: int) -> str | None:
        for _ in range(8):
            if not pid_alive(pid):
                return None
            try:
                for line in self.log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Git Branch Pane: "):
                        return line.removeprefix("Git Branch Pane: ").strip()
            except OSError:
                pass
            time.sleep(1)
        return None


def parse_args(argv: list[str]) -> tuple[str, list[str]]:
    mode = "daemon"
    remaining = list(argv)
    if remaining:
        if remaining[0] == "--daemon":
            remaining.pop(0)
        elif remaining[0] == "--foreground":
            mode = "foreground"
            remaining.pop(0)
        elif remaining[0] == "--stop":
            mode = "stop"
            remaining.pop(0)
        elif remaining[0] == "--status":
            mode = "status"
            remaining.pop(0)
    if mode == "daemon" and os.environ.get("GBP_DAEMON", "1") == "0":
        mode = "foreground"
    if not remaining and mode in {"daemon", "foreground"}:
        remaining = ["."]
    return mode, remaining


def main(argv: list[str] | None = None) -> int:
    mode, repo_args = parse_args(list(argv or []))
    launcher = Launcher()
    default_open = "0" if is_windows() else "1"
    open_browser = os.environ.get("GBP_OPEN", default_open) != "0" and "SSH_CONNECTION" not in os.environ

    if mode == "stop":
        return launcher.stop()
    if mode == "status":
        return launcher.status()
    if mode == "foreground":
        return launcher.foreground(repo_args, open_browser)
    return launcher.start_detached(repo_args, open_browser)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
