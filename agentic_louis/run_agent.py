"""Supervise a Codex process and save its exit status across worker restarts."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .process import atomic_json, child_environment, identity


def main():
    directory = Path(sys.argv[1]).resolve()
    launch = json.loads((directory / "launch.json").read_text())
    # flock in the worker does not leak to this process.
    atomic_json(directory / "supervisor.json", {"pid": os.getpid(), "identity": identity(os.getpid()),
                                                "started": time.time(), "token": launch["token"]})
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
    main()
