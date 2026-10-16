"""Private tmux servers for supervised interactive attempts."""

import json
import os
from pathlib import Path
import signal
import time

from .process import CommandError, command, identity


def terminal_args(launch):
    # A private, unpredictable socket prevents interference with other tmux work.
    return ["tmux", "-L", "agentic-louis-" + launch["token"], "-f", "/dev/null"]


def terminal_launch(attempt, token):
    path = Path(attempt) / "launch.json"
    if not path.exists():
        return None
    launch = json.loads(path.read_text())
    if launch.get("token") != token:
        raise ValueError("Saved terminal launch token does not match this attempt.")
    return launch if launch.get("transport") == "tmux" else None


def stop_terminal(attempt, token):
    launch = terminal_launch(attempt, token)
    if not launch:
        return
    child_path = Path(attempt) / "child.json"
    child = json.loads(child_path.read_text()) if child_path.exists() else {}
    if child and child.get("token") != token:
        raise ValueError("Saved terminal process token does not match this attempt.")
    pid, saved = child.get("pid"), child.get("identity")
    group = None
    descendants = {}
    if pid and saved and identity(pid) == saved:
        try:
            if os.getpgid(pid) != pid:
                raise ValueError("Terminal process no longer owns its process group.")
            # Tool commands can start new sessions, outside the terminal's group.
            # Snapshot their identities before terminating the root process.
            pairs = [tuple(map(int, row.split())) for row in command(["ps", "-axo", "pid=,ppid="]).splitlines()]
            owned = {pid}
            while True:
                found = {process for process, parent in pairs if parent in owned} - owned
                if not found:
                    break
                owned.update(found)
            descendants = {process: identity(process) for process in owned - {pid}}
            for process, start in descendants.items():
                if start and identity(process) == start:
                    try:
                        os.kill(process, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            group = pid
            os.killpg(group, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while identity(pid) == saved and time.monotonic() < deadline:
                time.sleep(0.05)
        except ProcessLookupError:
            pass
    # Always remove this attempt's server, including after a supervisor crash.
    # A missing socket means the pane already exited.
    try:
        command(terminal_args(launch) + ["kill-server"], timeout=5)
    except CommandError as error:
        if "no server running" not in str(error) and "No such file" not in str(error):
            raise
    if group and identity(pid) in (None, saved):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process, start in descendants.items():
        if start and identity(process) == start:
            try:
                os.kill(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
