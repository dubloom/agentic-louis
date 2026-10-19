"""Supervise a Codex process and save its exit status across worker restarts."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

# tmux and Codex's notification callback invoke this file from task directories.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentic_louis.process import atomic_json, child_environment, command, CommandError, identity
from agentic_louis.ddtool import gitlab_project_token
from agentic_louis.tmux import stop_terminal, terminal_args


def notify(directory, token, payload):
    """Persist the session ID and hand off results only after a complete turn."""
    launch = json.loads((directory / "launch.json").read_text())
    if launch.get("token") != token:
        raise ValueError("Notification belongs to another launch.")
    event = json.loads(payload)
    if event.get("type") != "agent-turn-complete":
        return
    if Path(event.get("cwd", "")).resolve() != Path(launch["cwd"]).resolve():
        return
    session = str(uuid.UUID(event["thread-id"]))
    atomic_json(directory / "session.json", {"token": token, "session_id": session})
    if (directory / "result.json").is_file():
        atomic_json(directory / "completion.json", {"token": token, "session_id": session})


def terminal(directory):
    launch = json.loads((directory / "launch.json").read_text())
    # tmux makes its pane process a process-group leader. Codex and its local
    # runtime stay in that group; --no-daemon prevents an unowned shared runtime.
    atomic_json(directory / "child.json", {"pid": os.getpid(), "identity": identity(os.getpid()),
                                           "token": launch["token"]})
    # PR watchers must keep polling while potentially long setup commands run.
    # Run setup inside the supervised terminal so timeout and orphan cleanup
    # cover it, and do not start Codex if preparation fails.
    try:
        for index, args in enumerate(launch.get("setup", [])):
            command(args, cwd=launch["setup_cwd"], timeout=launch["timeout"],
                    log_path=directory / f"setup-{index}.log")
        environment = child_environment()
        if launch.get("gitlab_repository"):
            owner, repository = launch["gitlab_repository"].split("/")
            # This terminal launcher runs on the host, before Codex applies its
            # sandbox. Keep credentials out of launch.json and process arguments.
            environment["GITLAB_TOKEN"] = gitlab_project_token(owner, repository)
            environment["GITLAB_HOST"] = "gitlab.ddbuild.io"
            print(f"GitLab authentication ready for {owner}/{repository}.", flush=True)
    except (OSError, ValueError) as exc:
        atomic_json(directory / "terminal-exit.json", {"exit_code": 1, "token": launch["token"], "error": str(exc)})
        return 1
    exit_code = subprocess.call(launch["args"], cwd=launch["cwd"], env=environment)
    atomic_json(directory / "terminal-exit.json", {"exit_code": exit_code, "token": launch["token"]})
    return exit_code


def supervise_terminal(directory, launch):
    exit_code, error = 1, None
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        # Finish the bounded tmux startup before cleanup. Raising inside
        # new-session could leave a server starting after cleanup has finished.
        stopping = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        command(terminal_args(launch) + ["new-session", "-d", "-s", "agent", "-x", "160", "-y", "48",
                                       "-c", str(directory), sys.executable, str(Path(__file__).resolve()),
                                       "terminal", str(directory)], timeout=10)
        atomic_json(directory / "terminal.json", {"token": launch["token"]})
        deadline = time.monotonic() + launch["timeout"]
        while True:
            if stopping:
                error = "Worker stopped this run."
                break
            completion = directory / "completion.json"
            if completion.exists():
                if json.loads(completion.read_text()).get("token") != launch["token"]:
                    raise ValueError("Completion belongs to another launch.")
                if not (directory / "result.json").is_file():
                    raise ValueError("Codex completed without a result file.")
                exit_code = 0
                break
            terminal_exit = directory / "terminal-exit.json"
            if terminal_exit.exists():
                saved = json.loads(terminal_exit.read_text())
                if saved.get("token") != launch["token"]:
                    raise ValueError("Terminal exit belongs to another launch.")
                error = saved.get("error") or f"Codex terminal exited with code {saved['exit_code']} before submitting a completed result."
                break
            if time.monotonic() >= deadline:
                error = "Codex exceeded the configured run timeout."
                break
            command(terminal_args(launch) + ["has-session", "-t", "agent"], timeout=5)
            time.sleep(0.2)
    except (OSError, ValueError) as exc:
        error = f"Codex terminal stopped: {exc}"
    finally:
        # Ignore subsequent signals while cleaning up the detached tmux server.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            output = command(terminal_args(launch) + ["capture-pane", "-p", "-S", "-", "-t", "agent"], timeout=5)
            (directory / "terminal.log").write_text(output)
        except (OSError, CommandError):
            pass
        try:
            stop_terminal(directory, launch["token"])
        except (OSError, ValueError) as exc:
            exit_code = 1
            error = (error + " " if error else "") + f"Terminal cleanup failed: {exc}"
        atomic_json(directory / "exit.json", {"exit_code": exit_code, "error": error, "token": launch["token"]})


def main():
    directory = Path(sys.argv[1]).resolve()
    launch = json.loads((directory / "launch.json").read_text())
    # flock in the worker does not leak to this process.
    atomic_json(directory / "supervisor.json", {"pid": os.getpid(), "identity": identity(os.getpid()),
                                                "started": time.time(), "token": launch["token"]})
    if launch.get("transport") == "tmux":
        supervise_terminal(directory, launch)
        return
    process = None

    def stop_family():
        # Share a process group so the worker can stop orphan descendants after a crash.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def terminate(signum, frame):
        stop_family()
        raise InterruptedError("Worker stopped this run.")

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    exit_code, error = 1, None
    try:
        with (directory / "events.jsonl").open("ab") as events, (directory / "stderr.log").open("ab") as errors:
            process = subprocess.Popen(launch["args"], cwd=launch["cwd"], stdin=subprocess.PIPE,
                                       stdout=events, stderr=errors, env=child_environment())
            atomic_json(directory / "child.json", {"pid": process.pid, "identity": identity(process.pid),
                                                   "token": launch["token"]})
            try:
                process.communicate(launch["prompt"].encode(), timeout=launch["timeout"])
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                stop_family()
                error = "Codex exceeded the configured run timeout."
    except (OSError, InterruptedError) as exc:
        error = f"Codex supervisor stopped: {type(exc).__name__}"
    finally:
        stop_family()
        atomic_json(directory / "exit.json", {"exit_code": exit_code, "error": error, "token": launch["token"]})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        notify(Path(sys.argv[2]).resolve(), sys.argv[3], sys.argv[4])
    elif len(sys.argv) > 1 and sys.argv[1] == "terminal":
        sys.exit(terminal(Path(sys.argv[2]).resolve()))
    else:
        main()
