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
- [Verify](#verify)

## Remote setup

### Prerequisites

The remote machine needs:

- **Python 3.10 or later** on macOS or Linux.
- **Git**, **GitHub CLI**, and **Codex CLI**.
- **Codex CLI 0.162.1 or later** with support for permission profiles. A separate worker profile is optional.

### Install and configure credentials

Install the worker package with pipx:

```sh
pipx install "git+https://github.com/YOUR_ACCOUNT/agentic-louis.git"
```

Replace the GitHub URL with your repository URL. The package exposes the `agentic-louis` command.
The local intake skill is a separate installation and is not included in this package.
For local development, run `bash setup.sh` and activate `.venv` instead.

Set the following values in your exported environment or a `.env` file in the directory where you run `agentic-louis`:

| Variable | Value |
| --- | --- |
| `JIRA_EMAIL` | Your Jira account email. |
| `JIRA_API_TOKEN` | Your Jira API token. |
| `SLACK_WORKFLOW_URL` | Your Slack Workflow Builder webhook URL. It must start with `https://hooks.slack.com/triggers/`. |
| `JIRA_API_BASE_URL` | Your Atlassian API gateway URL for a scoped Jira token. Leave empty for a site token. |

The worker loads only `.env` in the current working directory. Exported environment variables take precedence.
It also works with exported variables alone, without a `.env` file.

### Jira requirements

The default board is [LTRA](https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049).

- The board must have **To Do**, **In Progress**, and **In Review** columns with distinct status mappings.
- The LTRA project must have a **Task** issue type.
- The remote token needs board and issue read access, issue transitions, comments, and issue property read/write access.
- The workflow must offer a transition to **Blocked** for failed work. A separate Blocked board column is optional.
- The **Ready** status queues retries after manual intervention. It must be visible on the board and offer a transition to In Progress; a separate Ready column is optional.
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

**When adding a project, update the separate skill registry and the worker registry in `agentic_louis/projects.json`.**
Install the updated skill and worker package on the machines that use them.

### Duplicate protection

The skill records a UUID in the description before creation. If a creation response is uncertain, it searches for that exact request before another write and stops if the search finds no unique match.

It never resets a queued ticket to To Do.

## Remote workspace

### Run from the repository folder

Run one worker per project. No workspace file is required.

If the current directory is a Git repository root, the worker uses that repository.
Otherwise it discovers Git repository roots in immediate child directories.
It ignores hidden directories, symlink directories, and repositories nested more deeply.
All discovered repositories belong to the selected project workspace.

For each repository, the worker reads the GitHub repository from `origin` and resolves its default branch from the remote HEAD.
It fails with an error if either value cannot be resolved.
Codex chooses validation from each repository's `AGENTS.md` and build or test configuration.

Configure Git identity and authentication in the remote account, then run `gh auth login` and `codex login` once.
The GitHub account needs write access to each repository.

```sh
cd /path/to/folder-containing-your-repositories
agentic-louis --project trajectory --dry-run
agentic-louis --project trajectory
```

The dry run prints the discovered repositories, base branches, state directory, and managed Jira tickets.
It does not change Jira or launch agents.
The regular command runs in the foreground and can be used in tmux or a process supervisor.

The worker stores logs, process metadata, and Git worktrees under `~/.local/state/agentic-louis/trajectory`.
If `XDG_STATE_HOME` is set, it uses `$XDG_STATE_HOME/agentic-louis/trajectory` instead.
Each project has a separate state directory and process lock.
Jira is the task tracker. Execution checkpoints live in Jira issue properties, including attempts and notification delivery.
The worker does not create a local task database. Existing database checkpoints are copied to Jira without overwriting newer Jira metadata.
Keep that directory across restarts and package upgrades.

The worker admits tasks oldest first, with five slots shared by running tasks and tasks awaiting review.
It polls every five seconds and uses a two-hour run timeout.
Each task gets native Git worktrees and a `dubloom/<ticket-key>` branch across the discovered repositories.
Retries reuse existing branches, including branches created with the previous prefix.

### Optional overrides

Use `--state-dir /path/to/state` to override the default state directory.
Use `--profile NAME` to load `$CODEX_HOME/NAME.config.toml` if you want a specific model or additional Codex settings.
Without `--profile`, no worker profile file is needed.
The worker skips the base Codex configuration and applies task permissions itself.
It grants Git metadata writes for discovered repositories so Codex can commit changes.
Jira credentials and the Slack webhook URL are removed from child process environments.

Keep legacy `sandbox_mode` and `sandbox_workspace_write` settings out of an optional Codex profile.

### Validation and handoff

Codex reads the task and repository instructions, implements the change, runs checks, and publishes draft PRs.

Agent instructions require all necessary tests and validation to pass across changed repositories before opening any PR. Failed, skipped, or unexecuted checks (including tests that cannot start because Docker is unavailable) must produce a failed result without creating or reopening a PR or updating an existing PR. The worker moves failed work to Blocked.

Before handing off, the worker:

- Verifies draft status, branch ownership, pushed commits, and coverage of changed repositories.
- Runs configured checks and rejects uncommitted changes.
- Restores draft status and blocks the ticket if Codex publishes a non-draft PR.

After verification, it adds a Jira comment, moves the ticket to **In Review**, and sends the Slack webhook. The workflow receives:

| Field | Content |
| --- | --- |
| `jira_ticket_name` | Jira ticket name. |
| `project_label` | Project label. |
| `jira_ticket_url` | Jira ticket URL. |
| `blocked` | Boolean: `false` for review, `true` for failed work. |
| `prs` | Newline-separated PRs for review notifications. Omitted for blocked notifications. |

It sends one review notification per completed attempt, even when the task produces several PRs.
If work fails, the worker records the error in Jira, moves the ticket to **Blocked**, and sends `blocked: true` without PRs.
The slot becomes available after Jira confirms Blocked.

**Review comments, merging, ready status, and Jira completion remain manual.** Move a finished ticket out of In Review to release its slot.

## Recovery and inspection

### Inspect saved runs

Run this while the worker is running:

```sh
agentic-louis --project trajectory status
```

Git worktrees, process metadata, events, results, and validation logs live under the project artifact directory. Task tracking stays in Jira. **Keep this directory across restarts.**

### Restart the worker

Stopping the worker leaves existing supervisors running. Independent supervisors save Codex exit results even if the worker stops.

Restart from the same repository folder to collect their results. Use the same overrides if you supplied any. The worker saves completion stages so retries of Jira operations do not rerun implementation.

### Retry a failed task

Failed work moves to **Blocked** and releases its slot. The worker records the error in Jira and sends a blocked notification. Blocked tickets wait for manual intervention. Move a blocked ticket to **Ready** when it should be retried.

Both **To Do** and **Ready** queue work. The running worker starts another attempt when a slot is free, reusing its branch, worktrees, and existing PRs. Saved execution checkpoints never prevent these tickets from being queued again. Moving a ticket to Blocked stops implementation; moving it to In Review stops implementation and holds its review slot.

Alternatively, use the explicit retry command:

1. Inspect the logs and resolve the error.
2. Stop the worker.
3. Retry the failed ticket:

```sh
agentic-louis --project trajectory retry LTRA-123
```

The retry command starts the saved task, then continues the worker loop. It reuses worktrees, branches, and existing PRs.

A retry requires a failed run whose ticket is Blocked and a free project slot.
The worker moves it to In Progress before starting the next attempt.

This command does not handle review follow-up.

### Resend a Slack notification

If Slack delivery is uncertain, the worker records that state and does not resend automatically.

1. Check workflow activity first.
2. Stop the worker.
3. Resend explicitly:

```sh
agentic-louis --project trajectory resend LTRA-123
```

### Capacity after a restart

On restart, the worker reads Jira statuses and execution checkpoints to recover active attempts and notification delivery.
It moves unknown In Progress work to Blocked rather than starting duplicate work.

Change managed ticket statuses through this worker when starting new work. Manual status changes can exceed the limit before the worker sees them. The worker admits no further tickets while capacity is full.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests use mocked services and temporary Git repositories. They do not create live Jira tickets, GitHub PRs, or Slack messages.

Installation uses [pipx](https://pipx.pypa.io/latest/how-to/install-pipx.html).
The worker uses [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [GitHub CLI](https://cli.github.com/manual/gh_pr_view).
