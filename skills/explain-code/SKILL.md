---
name: explain-code
description: Create source-linked, step-by-step code walkthroughs and launch the Code Walk web UI to read them. Use when a user wants to understand unfamiliar code, follow a feature through multiple files, or learn what agent-generated code does.
---

# Explain code with Code Walk

Produce a `.walk.json` walkthrough for a developer who does not know this codebase. Use the installed `agentic-louis code-walk` CLI for bundling, validation and viewing; it works from any repository without activating a virtual environment. This skill does not require a particular language in the repository being explained.

Read [references/format.md](references/format.md) for the file contract and bundling commands.

Start from the requested function, file, feature, or change. Read the actual implementation, relevant callers, configuration and tests until you can explain a concrete path through it. If no scope is supplied, choose a useful entry point and state the scope. Avoid trying to cover a large repository in one walkthrough.

Keep the summary concise and include an `introduction` before the detailed steps. Its `request` states the question being explored, and its `body` explains what the thing does, why it exists, what starts it, the main components involved, and the result. Ground this context in the code you inspected and the user's actual scope. The UI displays the introduction under the title, expanded by default with a hide/show toggle.

When a visual overview helps, add `introduction.flow`: a short sequence of conceptual nodes with titles and optional descriptions. Link nodes to relevant step IDs so the reader can jump from the diagram to the code. Show the overall path rather than reproducing every step; mention meaningful branches in the introduction so a simplified flow does not imply they always execute. Diagrams use structured data, not HTML or embedded scripts; see the format reference.

Organize steps in execution or data-flow order, including cross-file calls where they help explain behavior. Each step needs a precise source range and an explanation of its purpose, what changes, and why it leads to the next step. Use concrete input/output examples and explain unfamiliar concepts in a small glossary. Avoid merely translating syntax into English. Cover meaningful branches and failure paths; a walkthrough is a curated path, not a claim that every step always executes.

Keep observations distinct from assumptions. Do not claim behavior from function names alone or claim that tests were run unless they were. Explain uncertainty and limitations in the summary or relevant step. Reference only source you inspected; never invent line numbers. Do not include secrets, credentials, generated bundles or unrelated files in source snapshots.

Write an agent-authored plan, then use `agentic-louis code-walk bundle` to capture exact source content and digests. Use the target repository as `--repo`, even if the viewer lives elsewhere. Validate the completed artifact with `agentic-louis code-walk validate`. If the bundler is unavailable, construct the same format directly from exact source text and SHA-256 digests, then validate once the tool is available. A plan without snapshots cannot be opened in the UI.

Default output: `~/.agentic-louis/walkthroughs/<repository-name>-<topic>.walk.json`. Omit `--output` to let the bundler choose this path from the repository and plan filename, or pass an explicit destination when requested. Avoid overwriting an existing walkthrough unless updating it is intended. Leave the implementation unchanged. Creating the walkthrough does not require launching another AI agent or sending code to a service; questions submitted from the UI use the local Codex CLI.

After validation, **launch the exact walkthrough yourself** with `agentic-louis code-walk serve <generated-walkthrough.walk.json> --repo <target-repository>`. Pass the artifact path so the UI opens that explanation immediately, rather than the shared library or demo. `--repo` supplies local source and question context; it does not select the explanation. Starting the server opens the default browser automatically. Do not stop after printing a launch command or ask whether to start it: opening the walkthrough is part of completing the request, unless the user explicitly asks for artifact-only output or not to open it.

Keep the server running using the environment's persistent process/session facility or a background process with output saved to a log. Verify the startup URL, walkthrough path, and repository in that output; confirm `/api/walkthrough` serves the generated artifact before reporting success. If the port is occupied, use `--port 0` to select an available port and read the actual URL from the output; leave existing servers alone. Use `--no-open` only when the user requests it or the environment is remote/headless.

Finish with the artifact location, scope covered, and **running viewer URL**. If launching or opening the browser fails, report the concrete failure and provide a manual command as a fallback; do not present that fallback as a successful launch.
