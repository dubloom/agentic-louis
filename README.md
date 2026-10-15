# Jira-driven agent development

Queue work from your local agent and run Codex on remote project workspaces. Jira connects the local and remote machines, from task intake to draft PR review.

## How it works

1. **Queue a task locally.** The intake skill creates a Jira ticket with a goal, context, acceptance criteria, and constraints.
2. **The remote worker claims it.** The worker reserves a slot, moves the ticket to **In Progress**, and launches Codex in separate worktrees.
3. **Codex implements the change.** It runs checks and publishes draft PRs for the affected repositories.
4. **The worker verifies the result.** It comments in Jira, moves the ticket to **In Review**, and sends a Slack notification.
5. **You review and finish.** Review comments, ready status, merging, and Jira completion remain manual.

**Each project has five slots shared by In Progress and In Review tickets.** A draft PR keeps its slot until its ticket leaves those columns. Move a finished ticket out of In Review to release capacity.

## Contents

- [Remote setup](#remote-setup)
- [Local intake through Atlassian MCP](#local-intake-through-atlassian-mcp)
- [Remote workspace](#remote-workspace)
- [Recovery and inspection](#recovery-and-inspection)
- [Read-only watcher](#read-only-watcher)
- [Verify](#verify)

## Remote setup

### Prerequisites

The remote machine needs:

- **Python 3.10 or later** on macOS or Linux.
- **Git**, **GitHub CLI**, and **Codex CLI**.
- A Codex profile using the file format supported by **Codex CLI 0.162.1**.

### Install and configure credentials

```sh
bash setup.sh
source .venv/bin/activate
cp .env.example .env
```

Set the following values in `.env` or your environment:

| Variable | Value |
| --- | --- |
| `JIRA_EMAIL` | Your Jira account email. |
| `JIRA_API_TOKEN` | Your Jira API token. |
| `SLACK_WORKFLOW_URL` | Your Slack Workflow Builder webhook URL. It must start with `https://hooks.slack.com/triggers/`. |
| `JIRA_API_BASE_URL` | Your Atlassian API gateway URL for a scoped Jira token. Leave empty for a site token. |

Scripts load `.env` from this repository. Environment values take precedence over `.env`.

### Jira requirements

The default board is [LTRA](https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049).

- The board must have **To Do**, **In Progress**, and **In Review** columns with distinct status mappings.
- The LTRA project must have a **Task** issue type.
- The remote token needs board and issue read access, issue transitions, and comments.
- Your Jira account needs the corresponding project permissions.

## Local intake through Atlassian MCP

### Install the skill

Install `skills/queue-project-task/` in your local agent skill directory and connect Atlassian MCP. Copy the whole folder rather than linking to the source:

```sh
mkdir -p ~/.agents/skills
cp -R skills/queue-project-task ~/.agents/skills/
```

This is a one-time copy from the source checkout or a copy you receive elsewhere. The skill contains its own project registry and has no dependency on this checkout.

After installation, use it from any directory, even on a machine without this Python project. **Local intake uses your Atlassian MCP connection** and needs no Python runtime, `.env` file, or Jira API token.

### Queue work

Ask your agent:

> In the trajectory project, queue a feature that shows the current task status.

The skill then:

1. Clarifies material gaps and writes the goal, context, acceptance criteria, and constraints.
2. Creates a Task in LTRA with the fixed project label.
3. Confirms that the ticket is in **To Do**.
4. Adds `agentic-managed` to make the ticket available to the worker.

The final label edit prevents the worker from claiming a ticket before intake finishes. Manual tickets without the management label do not consume slots or start agents.

### Project labels

The registry in `skills/queue-project-task/projects.json` fixes the allowed labels:

| Project | Project label |
| --- | --- |
| `trajectory` | `proj-trajectory` |
| `dd-trace-py` | `dd-trace-py` |

The skill selects from this registry. The worker accepts exactly one project label plus `agentic-managed` and ignores tickets with missing, unknown, or conflicting labels.

Atlassian MCP allows arbitrary Jira labels. The skill instructions constrain label selection, and the worker validates labels before execution; Atlassian MCP itself does not enforce this restriction.

**When adding a project, update both the installed skill copies and the remote checkout registry.**

### Duplicate protection

The skill records a UUID in the description before creation. If a creation response is uncertain, it searches for that exact request before another write and stops if the search finds no unique match.

It never resets a queued ticket to To Do.

## Remote workspace

### Configure one workspace per project

Run **one workspace and one worker daemon per project**, with one persistent state directory for that workspace.

Two daemons with different state directories must never manage the same project. The process lock prevents concurrent workers from sharing a state directory.

Copy `examples/workspace.json` to `workspace.local.json`, then configure each repository:

- Set its unique name, path, GitHub repository, and base branch.
- Ensure its origin points to the configured GitHub repository.
- Set validation commands as direct argument arrays, such as `["yarn", "test"]`.
- Use `[]` explicitly if it has no automated checks.

Paths can be absolute or relative to the configuration file. Add more repository entries if the project spans several repositories; every entry needs a unique name, path, and GitHub repository.

### Authenticate and configure Codex

1. Configure Git identity and authentication in the remote account.
2. Run `gh auth login` and `codex login`. The GitHub account needs write access to each repository.
3. Copy `examples/agentic-worker.config.toml` to `~/.codex/agentic-worker.config.toml`. If you set `CODEX_HOME`, put the file there instead.
4. Set the model in that profile if needed. Keep legacy `sandbox_mode` and `sandbox_workspace_write` settings out of it.

The worker skips the base Codex configuration and loads the selected profile file. For each session, it sets task write access, network access, and unattended execution, and grants Git metadata writes for the configured repositories so Codex can commit changes.

Jira credentials and the Slack webhook URL are removed from child process environments.

### Check setup and start the worker

Check prerequisites and list managed tickets without Jira writes or agent launches:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json --dry-run
```

Start the foreground worker in tmux or your existing process supervisor:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json
```

| Setting | Default | Configuration key |
| --- | --- | --- |
| Poll interval | 5 seconds | `interval` |
| Run timeout | 2 hours | `timeout` |

Set these keys in the workspace configuration to change the defaults.

The worker admits tasks oldest first. It reserves a slot, confirms In Progress in Jira, and launches Codex. Each task has separate worktrees and a branch across the configured repositories.

Up to five tasks can run concurrently if no tasks await review.

### Validation and handoff

Codex reads the task and repository instructions, implements the change, runs checks, and publishes draft PRs.

Before handing off, the worker:

- Verifies draft status, branch ownership, pushed commits, and coverage of changed repositories.
- Runs configured checks and rejects uncommitted changes.
- Restores draft status and pauses the ticket if Codex publishes a non-draft PR.

After verification, it adds a Jira comment, moves the ticket to **In Review**, and sends the Slack webhook. The workflow receives:

| Field | Content |
| --- | --- |
| `jira_ticket_name` | Jira ticket name. |
| `project_label` | Project label. |
| `jira_ticket_url` | Jira ticket URL. |
| `prs` | Newline-separated PRs. |

It sends one notification per ticket, even when the task produces several PRs.

**Review comments, merging, ready status, and Jira completion remain manual.** Move a finished ticket out of In Review to release its slot.

## Recovery and inspection

### Inspect saved runs

Run this while the worker is running:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json status
```

Runs, worktrees, events, results, and validation logs live under the configured state directory. **Keep this directory across restarts.**

### Restart the worker

Stopping the worker leaves existing supervisors running. Independent supervisors save Codex exit results even if the worker stops.

Restart with the same configuration and state directory to collect their results. The worker saves completion stages so retries of Jira operations do not rerun implementation.

### Retry a failed task

Failed work stays **In Progress** and keeps its slot. The worker records the error in Jira and waits for intervention.

1. Inspect the logs and resolve the error.
2. Stop the worker.
3. Retry the failed ticket:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json retry LTRA-123
```

The retry command starts the saved task, then continues the worker loop. It reuses worktrees, branches, and existing PRs.

A retry requires a failed run whose ticket is In Progress. If admission failed before the transition, resolve the error and move that ticket to In Progress before retrying.

This command does not handle review follow-up.

### Resend a Slack notification

If Slack delivery is uncertain, the worker records that state and does not resend automatically.

1. Check workflow activity first.
2. Stop the worker.
3. Resend explicitly:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json resend LTRA-123
```

### Capacity after a restart

On restart, the worker counts managed active tickets from Jira and saved reservations. It pauses unknown In Progress tickets rather than starting duplicate work.

Change managed ticket statuses through this worker when starting new work. Manual status changes can exceed the limit before the worker sees them. The worker admits no further tickets while capacity is full.

## Read-only watcher

The original command remains available:

```sh
python3 watch_jira.py --label proj-trajectory
python3 watch_jira.py --label proj-trajectory --once
```

- Prints JSON events to stdout and logs to stderr.
- Reads Jira without claiming tickets or launching agents.
- Deduplicates events until the watcher stops.

Use `--interval`, `--board-url`, and `--column` to change its settings.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests use mocked services and temporary Git repositories. They do not create live Jira tickets, GitHub PRs, or Slack messages.

The worker uses [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [GitHub CLI](https://cli.github.com/manual/gh_pr_view).
