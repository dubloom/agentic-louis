---
name: queue-project-task
description: Queue a feature or fix for a remote agent in the trajectory or dd-trace-py project through Atlassian MCP. Use when the user asks to delegate work to a project workspace. Do not use for direct local implementation.
---

# Queue a project task

Create a Jira task through the connected Atlassian MCP tools.
This skill works from any directory and needs no project checkout or local Python command.
Read `projects.json` beside this file for the site, Jira project, management label, and allowed project labels.
Resolve that file relative to this skill, regardless of the current working directory.
Treat the registry as read-only configuration.

## Prepare the request

Resolve the project from the user request and the registry.
Ask if the project is unclear or unknown.
Never invent a project or label, or change the registry to accept a request.
Capture the goal, context, acceptance criteria, and constraints.
Clarify missing details that would materially change the implementation.
Include relevant links supplied by the user.
Do not invent requirements or implementation facts.

Prepare a short summary and a Markdown description with these sections:

- Goal
- Context
- Acceptance criteria
- Constraints
- Request ID

Require at least one acceptance criterion.
Use “None specified” for constraints if the user supplies none.
Generate a UUID for this request and include `Agentic request: <UUID>` in the description.
Keep that UUID throughout creation and recovery.
If the user asks only for a draft, show the prepared task without creating it.

## Create the ticket

The request to queue work authorizes ticket creation.
Use the connected tools with these names or their server-prefixed equivalents.
If Atlassian MCP is unavailable, report the missing connection and stop.
Do not fall back to local scripts or request API tokens.

Call `createJiraIssue` with the registry cloud ID, Jira project, and issue type `Task`.
Set `contentFormat` to `markdown` and provide the prepared summary and description.
Set `additional_fields.labels` to an array containing only the selected project label.
The management label stays absent until the task is ready for the worker.
Do not put issue properties inside `additional_fields`.

Read the returned ticket with `getJiraIssue` and request summary, description, labels, and status.
Confirm its project label and complete description, including acceptance criteria and request ID.
If the management label is already present, verify its allowed labels and return its link without changing status.
If its status is not To Do, call `getTransitionsForJiraIssue`.
Choose the unique available transition whose destination status is To Do.
Match status names without case, spaces, hyphens, or underscores.
Do not choose a transition by its display name or invent a transition ID.
If there is no unique match, report the ticket link and missing transition, then stop.
Call `transitionJiraIssue` and read the ticket again to confirm To Do.

Enqueue it with `editJiraIssue`.
Set `fields.labels` to exactly the selected project label and the registry management label.
Read the issue again and verify both labels.
A worker may already have moved it to In Progress or In Review.
Do not move an already queued ticket back to To Do.
Report the ticket link, selected project, and a brief description of the queued work.

## Recover uncertain operations

If creation returns no clear result, do not create another ticket.
Search with `searchJiraIssuesUsingJql` in the registry Jira project and selected project label.
Use the UUID in the description search and request summary, description, labels, and status.
Follow result pages and confirm the exact `Agentic request: <UUID>` text in each candidate description.
If exactly one ticket matches, reuse it and continue from its current state.
If zero or several tickets match, report the uncertainty and stop.
Jira search can take time to show a new ticket.
Never generate a new request ID to bypass this check.

If the management label is already present, verify both allowed labels and return the existing ticket link.
Do not repeat creation, reset its status, or enqueue another ticket for the same request.
If a transition or label edit has an uncertain result, read the current ticket before another write.
For an unfinished ticket without the management label, resume the readiness checks above.
Never copy credentials into the task or chat.
