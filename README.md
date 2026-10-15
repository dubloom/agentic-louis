# Jira-driven agent development

Queue work from your local agent and run Codex on remote project workspaces.
Jira connects the local and remote machines.
Each project allows five managed tickets across In Progress and In Review.
A draft PR keeps its slot until its ticket leaves those columns.

The registry in `skills/queue-project-task/projects.json` fixes the allowed labels.
`trajectory` uses `proj-trajectory`.
`dd-trace-py` uses `dd-trace-py`.
The skill selects from this registry.
The worker accepts exactly one project label plus `agentic-managed`.
It ignores tickets with missing, unknown, or conflicting labels.

## Remote setup

Use Python 3.10 or later on macOS or Linux.
Remote workspaces also need Git, GitHub CLI, and Codex CLI.
The worker uses the profile file format supported by Codex CLI 0.162.1.

```sh
bash setup.sh
source .venv/bin/activate
cp .env.example .env
```

Set `JIRA_EMAIL` and `JIRA_API_TOKEN` in `.env` or your environment.
Environment values take precedence over `.env`.
Scripts load `.env` from this repository.
Set `SLACK_WORKFLOW_URL` to the Slack Workflow Builder webhook URL.
Its URL must start with `https://hooks.slack.com/triggers/`.
For a scoped Jira token, set `JIRA_API_BASE_URL` to your Atlassian API gateway URL.
For a site token, leave it empty.

The default board is [LTRA](https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049).
It must have To Do, In Progress, and In Review columns with distinct status mappings.
The LTRA project must have a Task issue type.
The remote token needs board and issue read access, issue transitions, and comments.
Your Jira account also needs the corresponding project permissions.

## Local intake through Atlassian MCP

Install `skills/queue-project-task/` in your local agent skill directory and connect Atlassian MCP.
The skill contains its own project registry and has no dependency on this checkout.
Copy the whole folder rather than linking to the source:

```sh
mkdir -p ~/.agents/skills
cp -R skills/queue-project-task ~/.agents/skills/
```

This is a one-time copy from the source checkout or a copy you receive elsewhere.
After installation, use the skill from any directory, even on a machine without this Python project.
Local intake needs no Python runtime, `.env` file, or Jira API token.
It uses your Atlassian MCP connection.

Ask your agent: “In the trajectory project, queue a feature that shows the current task status.”
The skill clarifies material gaps and writes the goal, context, acceptance criteria, and constraints.
It creates a Task in LTRA with the fixed project label.
After it confirms To Do, it adds `agentic-managed` to make the ticket available to the worker.
This final label edit prevents the worker from claiming a ticket before intake finishes.
Manual tickets without the management label do not consume slots or start agents.

The skill records a UUID in the description before creation.
If a creation response is uncertain, it searches for that exact request before another write.
It stops if the search finds no unique match.
It never resets a queued ticket to To Do.

Atlassian MCP allows arbitrary Jira labels.
The skill instructions constrain label selection, and the worker validates labels before execution.
This approach does not enforce a label restriction within Atlassian MCP itself.
When adding a project, update both installed skill copies and the remote checkout registry.

## Remote workspace

Run one workspace and one worker daemon per project.
Use one persistent state directory for that workspace.
Two daemons with different state directories must never manage the same project.
The process lock prevents concurrent workers from sharing a state directory.

Copy `examples/workspace.json` to `workspace.local.json` and set its values.
Paths can be absolute or relative to the configuration file.
Set each repository path, GitHub name, base branch, and validation commands.
Add more repository entries if the project spans several repositories.
Each entry needs a unique name, path, and GitHub repository.
Every origin must point to its configured GitHub repository.
Use direct argument arrays for checks, such as `["yarn", "test"]`.
Use `[]` explicitly if a repository has no automated checks.

Configure Git identity and authentication in the remote account.
Run `gh auth login` and `codex login` before starting the worker.
The GitHub account needs write access to each repository.
Copy `examples/agentic-worker.config.toml` to `~/.codex/agentic-worker.config.toml`.
If you set `CODEX_HOME`, put the file there instead.
You can set the model in that profile.
The worker sets task write access, network access, and unattended execution for each session.
It grants Git metadata writes for the configured repositories so Codex can commit changes.
The worker skips the base Codex configuration and loads the selected profile file.
Keep legacy `sandbox_mode` and `sandbox_workspace_write` settings out of that profile.
It removes Jira credentials and the Slack webhook URL from child process environments.

Check prerequisites and list managed tickets without Jira writes or agent launches:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json --dry-run
```

Start the foreground worker:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json
```

Run this command in tmux or your existing process supervisor.
The worker polls every five seconds and admits tasks oldest first.
It reserves a slot, confirms In Progress in Jira, and launches Codex.
Each task has separate worktrees and a branch across the configured repositories.
Up to five tasks can run concurrently if no tasks await review.
The default run timeout is two hours.
Set `interval` and `timeout` in the workspace configuration to change these values.

Codex reads the task and repository instructions, implements the change, runs checks, and publishes draft PRs.
The worker verifies draft status, branch ownership, pushed commits, and coverage of changed repositories.
It also runs configured checks and rejects uncommitted changes.
If Codex publishes a non-draft PR, the worker restores draft status and pauses the ticket.

After verification, the worker adds a Jira comment, moves the ticket to In Review, and sends the Slack webhook.
The workflow receives `jira_ticket_name`, `project_label`, `jira_ticket_url`, and newline-separated `prs`.
It sends one notification for the ticket, even when the task produces several PRs.
Review comments, merging, ready status, and Jira completion remain manual.
Move a finished ticket out of In Review to release its slot.

## Recovery and inspection

Inspect saved runs while the worker runs:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json status
```

Runs, worktrees, events, results, and validation logs live under the configured state directory.
Keep this directory across restarts.
The worker saves completion stages so retries of Jira operations do not rerun implementation.
Independent supervisors save Codex exit results even if the worker stops.
Stopping the worker leaves existing supervisors running.
Restart with the same configuration and state directory to collect their results.

Failed work stays In Progress and keeps its slot.
The worker records the error in Jira and waits for intervention.
Inspect its logs, stop the worker, and retry the failed ticket:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json retry LTRA-123
```

The retry command starts the saved task, then continues the worker loop.
It reuses worktrees, branches, and existing PRs.
A retry requires a failed run whose ticket is In Progress.
If admission failed before the transition, resolve the error and move that ticket to In Progress before retrying.
This command does not handle review follow-up.

If Slack delivery is uncertain, the worker records that state and does not resend automatically.
Check workflow activity first, then stop the worker and resend explicitly:

```sh
python3 -m agentic_louis.worker --project trajectory --config workspace.local.json resend LTRA-123
```

On restart, the worker counts managed active tickets from Jira and saved reservations.
It pauses unknown In Progress tickets rather than starting duplicate work.
Change managed ticket statuses through this worker when starting new work.
Manual status changes can exceed the limit before the worker sees them.
The worker admits no further tickets while capacity is full.

## Read-only watcher

The original command remains available:

```sh
python3 watch_jira.py --label proj-trajectory
python3 watch_jira.py --label proj-trajectory --once
```

It prints JSON events to stdout and logs to stderr.
It reads Jira and does not claim tickets or launch agents.
Deduplication lasts until the watcher stops.
Use `--interval`, `--board-url`, and `--column` to change its settings.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests use mocked services and temporary Git repositories.
They do not create live Jira tickets, GitHub PRs, or Slack messages.

The worker uses [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [GitHub CLI](https://cli.github.com/manual/gh_pr_view).
