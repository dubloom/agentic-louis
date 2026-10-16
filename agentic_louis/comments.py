"""Native Jira formatting for worker comments."""

import re
from urllib.parse import unquote, urlsplit


def text(value):
    return {"type": "text", "text": value}


def paragraph(value):
    # Agent summaries often contain backtick-delimited commands and file paths.
    content = []
    for part in re.split(r"(`[^`\n]+`)", value):
        if part:
            is_code = re.fullmatch(r"`[^`\n]+`", part) is not None
            node = text(part[1:-1] if is_code else part)
            if is_code:
                node["marks"] = [{"type": "code"}]
            content.append(node)
    return {"type": "paragraph", "content": content}


def heading(value, level=4):
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def code(value, language="text"):
    return {"type": "codeBlock", "attrs": {"language": language}, "content": [text(value)]}


def paragraphs(value):
    return [paragraph(line) for line in value.splitlines() if line.strip()]


def document(*nodes):
    return {"type": "doc", "version": 1, "content": list(nodes)}


def reference(token):
    return {"type": "expand", "attrs": {"title": "Automation reference"},
            "content": [paragraph(token)]}


def links(urls):
    items = []
    for url in urls:
        parts = urlsplit(url).path.strip("/").split("/")
        label = url
        if len(parts) >= 4:
            repository = "/".join(parts[:2])
            if parts[2] == "tree":
                label = f"{repository} · {unquote('/'.join(parts[3:]))}"
            elif parts[2] == "pull":
                label = f"{repository} #{parts[3]}"
        node = text(label)
        node["marks"] = [{"type": "link", "attrs": {"href": url}}]
        items.append({"type": "listItem", "content": [{"type": "paragraph", "content": [node]}]})
    return {"type": "bulletList", "content": items}


def started(attempt, watch_command, remote_watch_command):
    return document(
        heading(f"Agent started · attempt {attempt}", 3),
        paragraph("Watch progress live. Press Ctrl-C to stop watching; the agent keeps running."),
        heading("Watch from your laptop"), code(remote_watch_command, "bash"),
        heading("Watch on the worker workspace"), code(watch_command, "bash"))


def resume_nodes(commands):
    if not commands:
        return []
    local_command, remote_command = commands
    return [
        heading("Resume from your laptop"), code(remote_command, "bash"),
        heading("Resume on the worker workspace"), code(local_command, "bash"),
        paragraph("Run the local resume command from the worker's launch directory so its `.env` is loaded. "
                  "Use the worker's Codex account and `CODEX_HOME`. "
                  "The wrapper checks that the worker has finished and this is still the latest attempt.")]


def blocked(run, resume_commands=None):
    reason, separator, validation = run.get("error", "Unknown error").partition("\nValidation: ")
    reason = reason.removeprefix("Codex reported incomplete work: ")
    nodes = [heading(f"Work blocked · attempt {run.get('attempt', 0)}", 3)]
    # Keep raw command output together instead of turning every log line into a paragraph.
    first, newline, detail = reason.partition("\n")
    nodes.append(paragraph(first))
    for title, urls in (("Saved branches", run.get("partial_branches", [])),
                        ("Draft PRs", run.get("partial_prs", []))):
        if urls:
            nodes.extend([heading(title), links(urls)])
    if separator:
        nodes.extend([heading("Validation"), *paragraphs(validation)])
    if newline and detail.strip():
        nodes.append({"type": "expand", "attrs": {"title": "Failure details"}, "content": [code(detail)]})
    if run.get("branch_publication_errors"):
        nodes.extend([heading("Branch publication errors"), code("\n\n".join(run["branch_publication_errors"]))])
    nodes.extend([heading("Next steps"), paragraph(
        "Inspect the worker status and run logs. Resolve the blocker, then move this ticket to Ready to retry.")])
    return document(*nodes, *resume_nodes(resume_commands))


def ready(run, resume_commands=None):
    result = run["result"]
    nodes = [heading(f"Ready for review · attempt {run.get('attempt', 0)}", 3),
             *paragraphs(result["summary"]), heading("Draft PRs"),
             links([pr["url"] for pr in result["prs"]]),
             heading("Validation"), *paragraphs(result["validation"])]
    if run.get("verified_checks"):
        nodes.extend([heading("Worker checks"), code("\n".join(run["verified_checks"]))])
    return document(*nodes, *resume_nodes(resume_commands))
