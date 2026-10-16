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
- **Git**, **GitHub CLI**, **tmux 3.2 or later**, and **Codex CLI**.
- **Codex CLI** with support for permission profiles, `--no-daemon`, and `--no-alt-screen` (tested with 0.163.0). A separate worker profile is optional.

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

Run one worker per project. You can explicitly configure its repository group or use directory discovery.

### Configure a repository group

Create `agentic-louis.json` where you launch the worker, or pass
`--config /path/to/workspace.json`. The file only needs repository locations and,
optionally, a folder to focus on. See [agentic-louis.example.json](agentic-louis.example.json).

```json
{
  "repositories": [
    {"path": "./backend", "folder": "src/api"},
    {"path": "./frontend"}
  ]
}
```

Only listed repositories are included. `path` must identify an existing Git
repository root with a GitHub origin. It can be absolute or relative to the JSON
file. `folder`, when supplied, must be an existing directory relative to that
repository; omit it to focus on the whole repository. Absolute folder paths and
paths or symlinks escaping the repository are rejected.

The AI receives the focus folder inside its isolated ticket worktree, rather than
the original checkout. It starts investigating there and can consult repository
instructions, supporting code, and tests elsewhere in the worktree as needed.
The folder is a focus hint, not a filesystem access restriction. If it is missing
from the worktree (for example, an untracked local directory), the ticket is
blocked before Codex starts.

```sh
agentic-louis --project trajectory --config /path/to/workspace.json --dry-run
agentic-louis --project trajectory --config /path/to/workspace.json
```

With `agentic-louis.json` in the current directory, `--config` can be omitted.
Select the queue using `--project`; optionally set a top-level `"project"` in the
JSON file instead. If both are supplied, they must agree. The existing project
registry controls allowed project names.

Additional settings are optional:

- Top-level `state_dir` and `profile` set the artifact directory and Codex profile;
  `--state-dir` and `--profile` override them. Relative state paths resolve beside
  the JSON file.
- Slack commands automatically target `workspace-dubloom-<project-name>`.
  Optional top-level `cursor_remote` overrides the Cursor SSH authority if needed.
- Per-repository `base` overrides the remote's default branch.
- Per-repository `setup` and `checks` accept argument arrays such as
  `"setup": [["npm", "ci"]]` and `"checks": [["npm", "test"]]`. No shell is used.
  Setup runs in every repository worktree before Codex starts, including retries,
  so it must be safe to repeat. Failures block the ticket; logs are saved with the
  attempt. Codex runs configured checks before publishing, and the worker runs
  them again for changed repositories before moving the ticket to In Review.

Duplicate repositories and unknown settings are rejected. Dry runs display the
selected repositories, focus folders, and commands without executing setup or checks.
Automatic config discovery applies to run, check, retry, and resend; pass `--config`
explicitly for status or blocked commands that should use the file. Credentials
still come from exported variables or `.env` in the launch directory.

### Discover repositories from the launch directory

When no config file is selected, directory discovery works as before.

If the current directory is a Git repository root, the worker uses that repository.
Otherwise it discovers Git repository roots in immediate child directories.
It ignores hidden directories, symlink directories, and repositories nested more deeply.
All discovered repositories belong to the selected project workspace.

For each repository, the worker reads the GitHub repository from `origin` and resolves its base branch from the remote HEAD unless `base` is configured.
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

Each attempt runs interactive Codex inside a private tmux server. The next
worker poll adds a short Jira comment with commands to attach to that exact
terminal from your laptop over SSH or directly on the worker workspace. You can
watch progress and give instructions in the same conversation. Detach with
**Ctrl-B, then d** to leave the agent running. Ctrl-C goes to Codex and may
interrupt its active turn; exiting Codex before it submits a result blocks the
attempt. Attaching never starts a second Codex client. Each attempt gets one
start comment, including retries.

Codex runs with `--no-daemon`, so its runtime stays owned by the supervised
terminal. At completion, the agent atomically writes `result.json`, and Codex's
turn-completion notification records the conversation ID and signals the
supervisor to close the terminal. The worker then validates the result and PRs
as before. A timeout, cancellation, or abandoned terminal triggers cleanup of
that attempt's tmux server and process group. A terminal snapshot is saved as
`terminal.log` when available.

If an attempt stops before its first turn completes, resume discovery also
checks the launch account's local Codex state database for an exact match to
the task directory and initial prompt. Ambiguous matches or an unsupported
database schema leave resume unavailable rather than opening another task.

The Blocked and In Review comments provide
`agentic-louis --project PROJECT --state-dir PATH resume TICKET --attempt NUMBER`
to open the saved Codex conversation after the worker finishes, when a saved
conversation is available. This wrapper checks that the supervisor and Codex
child have stopped, the worker has finished
validation and publication, and the ticket is Blocked or In Review. It rejects
commands from older attempts after a retry. Use the worker's Codex account and
`CODEX_HOME`. Run local resume commands from the worker's launch directory so
its `.env` is loaded; the SSH command does this automatically. Avoid direct
`codex resume` while the worker is running, since another client can interrupt
the active conversation. Existing Jira comments retain their original commands;
these attach and guarded resume commands appear on new attempts. Existing
non-interactive attempts remain collectable across an upgrade; new attempts
use tmux.

Worker comments use native Jira headings, code blocks for commands and diagnostics,
and labeled branch and PR links. Automation references and detailed failure logs
are collapsed so the outcome and next steps stay easy to scan.

### Optional overrides

Use `--state-dir /path/to/state` to override the default state directory.
New agent runs explicitly use `gpt-6.1-sol`, overriding any model set in a profile.
Use `--profile NAME` to load `$CODEX_HOME/NAME.config.toml` for additional Codex settings.
Without `--profile`, no worker profile file is needed.
Interactive Codex loads the worker account's base configuration and optional
profile. The worker overrides the model, task permissions, and `notify` command
for its result handoff, trusts the generated task directory, and disables the
shared Codex daemon.
It grants Git metadata writes for discovered repositories so Codex can commit changes.
Jira credentials and the Slack webhook URL are removed from child process environments.

Keep legacy `sandbox_mode` and `sandbox_workspace_write` settings out of an optional Codex profile.

### Validation and handoff

Codex reads the task and repository instructions, implements the change, runs checks, and publishes draft PRs.

Agent instructions require all necessary tests and validation to pass across changed repositories before opening any PR. If checks fail, are skipped, or cannot run (including when Docker is unavailable), the agent commits and pushes any code changes to the task branch and returns a failed result without creating or reopening a PR or editing PR metadata. The worker moves failed work to Blocked.

On failure, the worker also stops the agent and saves any remaining uncommitted or
unpushed changes to the task branch in each changed repository. This also covers
agent crashes, timeouts, and worker verification failures. The Jira failure comment
includes links to the published branches alongside the failure reason. Unchanged
repositories are skipped. If a commit or push fails, the comment reports the error
and local changes remain available for recovery. Pushes never force remote history.

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
| `resume_command` | Run on your laptop: SSH into `workspace-dubloom-<project-name>` with a terminal and resume the blocked ticket's saved Codex conversation. Preserves the worker's launch directory, state directory, and optional profile. Omitted for review notifications. |
| `cursor_command` | Run on your laptop: open the blocked ticket's worktrees in Cursor over Remote SSH. Defaults to `workspace-dubloom-<project-name>`; `cursor_remote` can override Cursor's authority. Empty if worktrees were never created; omitted for review notifications. |

It sends one review notification per completed attempt, even when the task produces several PRs.
If work fails, the worker records the error in Jira, moves the ticket to **Blocked**, and sends `blocked: true` without PRs.
The slot becomes available after Jira confirms Blocked.

In Slack Workflow Builder, add **Text** inputs named `resume_command` and
`cursor_command` to the webhook trigger, then insert those variables into code
blocks in the blocked-message step. This template update is required to display
the commands; changing the worker alone does not change Slack's message. For example:

```text
🟠 Blocked · [project_label]
[jira_ticket_name]

Open ticket: [jira_ticket_url]

Open worktrees in Cursor from your laptop:
[cursor_command]

Resume Codex from your laptop:
[resume_command]
```

Replace the bracketed placeholders with Workflow Builder's variable chips. Format
the commands as code so they are easy to copy. Both commands run on your laptop;
they do not require a local installation of `agentic-louis` or Codex.

For project `dd-trace-py` and ticket `LTRA-10`, the resume command has this form:

```sh
ssh -t workspace-dubloom-dd-trace-py 'cd /path/where/worker/was/launched && agentic-louis --project dd-trace-py --state-dir /home/bits/.local/state/agentic-louis/dd-trace-py blocked LTRA-10'
```

SSH allocates a terminal for Codex. The remote command restores the worker's
launch directory so its `.env` credentials can be loaded, then resumes the exact
saved conversation in its task directory. Keep the worker's Codex account and
`CODEX_HOME` available on that workspace. Jira stays Blocked until you move it to Ready.

The Cursor command opens `runs/<ticket>/repositories` under the remote state
directory, which contains that ticket's worktrees across all its repositories.
Your laptop needs Cursor's `cursor` command and Remote SSH access to the
`workspace-dubloom-<project-name>` host alias. No Cursor executable is needed on
the worker. These commands assume your laptop's workspace SSH configuration is set up.
If setup failed before Codex started, worktrees can still be opened, but no Codex
conversation exists yet; the resume command reports that condition.

**Review comments, merging, ready status, and Jira completion remain manual.** Move a finished ticket out of In Review to release its slot.

## Recovery and inspection

### Inspect saved runs

Run this while the worker is running:

```sh
agentic-louis --project trajectory status
```

Git worktrees, process metadata, results, terminal snapshots, and validation logs live under the project artifact directory. Task tracking stays in Jira. **Keep this directory across restarts.**

### Restart the worker

If startup reports `Another worker owns this artifact directory.`, an existing worker holds the project's `worker.lock`. On the worker machine, find its PID and inspect the process (this example uses the default state directory for `dd-trace-py`):

```sh
lsof /home/bits/.local/state/agentic-louis/dd-trace-py/worker.lock
ps -p 12345 -o pid,ppid,lstart,args
```

Replace `12345` with the PID from `lsof`. After confirming it is the worker you want to stop, send Ctrl+C in its terminal or run:

```sh
kill -INT 12345
```

The lock releases when the worker exits. Do not delete `worker.lock` while a worker is running: doing so could allow two workers to use the same artifact directory. If you configured a different state directory, use its `worker.lock` path instead.

Stopping the worker leaves existing supervisors running. Independent supervisors save Codex exit results even if the worker stops.

Restart from the same repository folder to collect their results. Use the same overrides if you supplied any. The worker saves completion stages so retries of Jira operations do not rerun implementation.

### Retry a failed task

Failed work moves to **Blocked** and releases its slot. The worker records the error in Jira and sends a blocked notification. Blocked tickets wait for manual intervention. Move a blocked ticket to **Ready** when it should be retried.

To intervene, run this in an interactive terminal on the worker machine:

```sh
agentic-louis blocked
```

The command shows managed Blocked tickets across projects in an interactive list, with task titles and a blocker preview for the highlighted ticket. The layout adapts to narrow terminals.
Use **↑/↓** to highlight a ticket, then **Enter** to open its latest Codex session in the original task workspace, including its repository worktrees and conversation history. Tickets without a saved session are disabled. Select **Cancel** or press **Ctrl+C** to exit.
It works from any directory while the worker is running. It uses the same Jira credentials and `.env` loading as other commands; keep the worker's Codex account and `CODEX_HOME` so its sessions are available.

Use `agentic-louis --project trajectory blocked` to filter by project, or `agentic-louis blocked LTRA-123` to open a specific ticket directly.
If the worker uses a custom state directory, supply both `--project` and `--state-dir`.
Add `--dry-run` to print a table of tickets without opening Codex. Redirected output has no colors.
Tickets that failed before Codex started, or whose local artifacts are missing, show why their sessions are unavailable.

Opening a session leaves the ticket Blocked. Resolve the blocker, exit Codex, then move the ticket to **Ready** for the worker to retry and verify the task.

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
The worker uses [Codex CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli) in tmux and [GitHub CLI](https://cli.github.com/manual/gh_pr_view).
