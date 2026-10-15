import json
import os
from pathlib import Path
import signal
import subprocess


class CommandError(ValueError):
    pass


def child_environment():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("JIRA_") and k != "SLACK_WORKFLOW_URL"}


def command(args, *, cwd=None, timeout=60, env=None, log_path=None):
    try:
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env or child_environment(),
                                   text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise CommandError(f"Cannot start command {args[0]}.") from None
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_group(process)
        stdout, stderr = process.communicate()
    if log_path is not None:
        Path(log_path).write_text(stdout + "\n" + stderr)
    if timed_out:
        raise CommandError(f"Command {args[0]} exceeded its {timeout}s timeout.")
    if process.returncode:
        # Do not print stderr: a configured remote URL or tool error can include credentials.
        raise CommandError(f"Command {args[0]} {args[1] if len(args) > 1 else ''} failed (exit {process.returncode}).")
    return stdout.strip()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def identity(pid):
    try:
        os.kill(pid, 0)
        if command(["ps", "-p", str(pid), "-o", "stat="]).startswith("Z"):
            return None
        return command(["ps", "-p", str(pid), "-o", "lstart="])
    except (OSError, CommandError):
        return None


def stop_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()
