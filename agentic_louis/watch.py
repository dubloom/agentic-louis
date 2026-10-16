"""Render saved Codex events without opening or taking over its conversation."""

import json
from pathlib import Path
import re
import time


def brief(value, *, lines=8, chars=1600):
    value = str(value).strip()
    selected = "\n".join(value.splitlines()[-lines:])
    selected = selected[-chars:]
    return ("… output trimmed; use --full-output for everything …\n" if selected != value else "") + selected


def render_event(event, *, full_output=False):
    if not isinstance(event, dict):
        return ""
    kind = event.get("type")
    if kind == "thread.started":
        return "Agent conversation started."
    if kind == "turn.started":
        return "Working…"
    if kind == "turn.completed":
        return "Turn completed."
    if kind in {"error", "turn.failed"}:
        error = event.get("error", event)
        return "Error: " + str(error.get("message", error) if isinstance(error, dict) else error)
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    item_type = item.get("type")
    if item_type == "agent_message" and kind == "item.completed":
        return "\nAgent: " + item.get("text", "")
    if item_type == "command_execution":
        if kind == "item.started":
            command = item.get("command", "")
            return "\n$ " + (command if full_output else brief(command, lines=3, chars=500))
        if kind == "item.completed":
            output = item.get("aggregated_output", "").strip()
            status = f"Command {item.get('status', 'completed')} (exit {item.get('exit_code', '?')})."
            if output:
                status += "\n" + (output if full_output else brief(output))
            return status
    if item_type == "file_change" and kind == "item.completed":
        return "\nFiles changed:\n" + "\n".join(
            f"  {change.get('kind', 'update')}: {change.get('path', '?')}"
            for change in item.get("changes", []) if isinstance(change, dict))
    if kind == "item.completed" and item_type in {"mcp_tool_call", "web_search"}:
        label = item.get("tool") or item.get("query") or item_type
        return f"Tool: {label} ({item.get('status', 'completed')})"
    return ""


def watch_attempt(*, state_dir, key, attempt, full_output=False, follow=True):
    key = key.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key) or attempt < 1:
        raise ValueError("watch requires a valid Jira issue key and a positive attempt number.")
    directory = Path(state_dir) / "runs" / key / f"attempt-{attempt}"
    path = directory / "events.jsonl"
    print(f"Watching {key}, attempt {attempt}. Ctrl-C stops watching; the agent keeps running.", flush=True)
    print(f"Raw log: {path}", flush=True)
    try:
        with path.open() as events:
            pending = ""
            while True:
                chunk = events.readline()
                if chunk:
                    pending += chunk
                    if not pending.endswith("\n"):
                        continue
                    try:
                        event = json.loads(pending)
                    except json.JSONDecodeError:
                        pass  # Ignore incomplete or non-event diagnostics.
                    else:
                        rendered = render_event(event, full_output=full_output)
                        if rendered:
                            print(rendered, flush=True)
                    pending = ""
                    continue
                if not follow or (directory / "exit.json").exists():
                    print("End of saved log." if not follow else "Agent process stopped.", flush=True)
                    return 0
                time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
