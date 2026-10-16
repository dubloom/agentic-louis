"""Find blocked Jira work and resume its saved Codex session interactively."""

import json
from pathlib import Path
import subprocess
import sys
import uuid
import sqlite3
from contextlib import closing

from .config import default_state_dir
from .jira import Jira
from .process import child_environment, identity
from .projects import PROJECTS, is_managed, project_label
from .terminal import blocked_tickets, make_console, selection_prompt


def interrupted_session(launch):
    """Best-effort recovery before the first turn-completion notification.

    Codex's local database is an implementation detail; require an exact initial
    prompt match and fail closed if its schema changes or matches are ambiguous.
    """
    if not launch.get("codex_home") or not launch.get("prompt"):
        return None
    matches = set()
    for path in Path(launch["codex_home"]).glob("state_*.sqlite"):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                rows = db.execute("SELECT id FROM threads WHERE cwd=? AND first_user_message=? AND source='cli'",
                                  (launch["cwd"], launch["prompt"])).fetchall()
                matches.update(str(uuid.UUID(row[0])) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            continue
    return next(iter(matches)) if len(matches) == 1 else None


def session_for(run, project, state_dir, key):
    if not run or run.get("project") != project or run.get("key") != key:
        raise ValueError("No execution checkpoint for this project.")
    if not run.get("attempt_dir"):
        raise ValueError("No saved Codex attempt; the agent may have failed before starting.")
    directory = state_dir / "runs" / run["key"]
    attempt = Path(run["attempt_dir"]).resolve()
    if attempt.parent != directory or not attempt.is_dir():
        raise ValueError("Saved attempt is unavailable here. Run on the worker machine with its original --state-dir.")
    launch = json.loads((attempt / "launch.json").read_text())
    if launch.get("token") != run.get("launch_token") or Path(launch["cwd"]).resolve() != directory:
        raise ValueError("Saved launch does not match this attempt.")
    if launch.get("transport") == "tmux":
        session_path = attempt / "session.json"
        if not session_path.exists():
            session = interrupted_session(launch)
            if session:
                return session, directory
            raise ValueError("No saved Codex session ID for this attempt yet.")
        saved = json.loads(session_path.read_text())
        if saved.get("token") != run.get("launch_token"):
            raise ValueError("Saved Codex session belongs to another attempt.")
        try:
            return str(uuid.UUID(saved["session_id"])), directory
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("Saved Codex session ID is invalid.") from None
    with (attempt / "events.jsonl").open() as events:
        for line in events:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # Interrupted runs can end with an incomplete event.
            if isinstance(event, dict) and event.get("type") == "thread.started":
                try:
                    session = str(uuid.UUID(event["thread_id"]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise ValueError("Saved Codex session ID is invalid.") from None
                return session, directory
    raise ValueError("No Codex session ID in this attempt's event log.")


def ensure_stopped(run):
    processes = [{"pid": run.get("supervisor_pid"), "identity": run.get("supervisor_identity")}]
    for name in ("supervisor.json", "child.json"):
        path = Path(run["attempt_dir"]) / name
        if path.exists():
            metadata = json.loads(path.read_text())
            if metadata.get("token") != run.get("launch_token"):
                raise ValueError("Saved process metadata does not match this attempt.")
            processes.append(metadata)
    for process in processes:
        if process.get("pid") and process.get("identity") and identity(process["pid"]) == process["identity"]:
            raise ValueError("The previous agent is still running. Wait for the worker to stop it before intervening.")


def resume_attempt(jira, *, project, state_dir, key, attempt, profile=None, dry_run=False):
    """Resume a specific completed attempt without claiming or changing Jira work."""
    key = key.upper()
    current = jira.issue(key)
    if not is_managed(current, project):
        raise ValueError("This ticket is no longer managed by this project.")
    run = jira.run(key)
    session, cwd = session_for(run, project, state_dir, key)
    if run.get("attempt") != attempt:
        raise ValueError("This command refers to an older attempt. Use the latest Jira comment.")
    ensure_stopped(run)
    # The supervisor may have exited while the worker still validates or publishes.
    # Ready tickets can be admitted again, so do not open them interactively.
    if run.get("phase") not in {"blocked", "review"} or not (
            Jira.is_blocked(current) or current["fields"]["status"]["id"] in jira.columns()["In Review"]):
        raise ValueError("Wait for the worker to finish and the ticket to reach Blocked or In Review before resuming.")
    if dry_run:
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Opening Codex requires an interactive terminal.")
    args = ["codex", "resume", session, "--cd", str(cwd), "--ask-for-approval", "on-request"]
    if profile:
        args.extend(["--profile", profile])
    return subprocess.call(args, cwd=cwd, env=child_environment())


def open_blocked(jira, *, project=None, state_dir=None, profile=None, key=None, dry_run=False):
    console = make_console()
    rows = []
    for name in (project,) if project else PROJECTS:
        directory = Path(state_dir).expanduser().resolve() if state_dir else default_state_dir(name)
        for issue in jira.candidates(project_label(name), None):
            if is_managed(issue, name) and Jira.is_blocked(issue):
                rows.append((issue, name, directory))
    rows.sort(key=lambda row: (row[1], int(row[0]["key"].rsplit("-", 1)[1])))
    if key:
        rows = [row for row in rows if row[0]["key"] == key.upper()]
        if not rows:
            raise ValueError(f"{key} is not a managed Blocked ticket in the selected projects.")
    if not rows:
        blocked_tickets(console, [])
        return 0
    tickets = []
    for issue, name, directory in rows:
        run = jira.run(issue["key"])
        try:
            session_for(run, name, directory, issue["key"])
            detail = " ".join(str((run or {}).get("error", "")).split())
            available = True
        except (ValueError, OSError) as error:
            detail = f"Session unavailable: {error}"
            available = False
        tickets.append((issue, name, detail, available))
    if dry_run:
        blocked_tickets(console, tickets)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        blocked_tickets(console, tickets)
        raise ValueError("Opening Codex requires an interactive terminal. Use --dry-run to list blocked tickets.")
    if key:
        blocked_tickets(console, tickets)
        selected = rows[0]
    else:
        try:
            answer = selection_prompt(console, tickets)
        except EOFError:
            return 0
        if not answer:
            return 0
        selected = next(row for row in rows if row[0]["key"] == answer)
    issue, name, directory = selected
    # Re-read after the picker: Jira may have changed while the user was choosing.
    current = jira.issue(issue["key"])
    if not is_managed(current, name) or not Jira.is_blocked(current):
        raise ValueError("This ticket is no longer managed and Blocked. Refresh the list.")
    run = jira.run(issue["key"])
    session, cwd = session_for(run, name, directory, issue["key"])
    ensure_stopped(run)
    args = ["codex", "resume", session, "--cd", str(cwd), "--ask-for-approval", "on-request"]
    if profile:
        args.extend(["--profile", profile])
    console.print(f"\n[bold cyan]Opening {issue['key']} in Codex…[/]")
    console.print("After resolving the blocker, exit Codex and move the ticket to Ready.", style="dim")
    console.print()
    return subprocess.call(args, cwd=cwd, env=child_environment())
