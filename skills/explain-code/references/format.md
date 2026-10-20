# Walkthrough format, version 1

The UI accepts a UTF-8 JSON object smaller than 2 MB. It includes source snapshots so the viewer does not need access to the original repository. Paths are POSIX paths relative to the repository root. Source ranges are 1-based, inclusive, and point to the included snapshot. All explanation fields are text; backticks, bold, paragraphs and fenced code blocks are supported. HTML is displayed as text.

Author a plan like this (line numbers are illustrative; replace them with verified ranges):

```json
{
  "version": 1,
  "title": "How a request becomes a response",
  "summary": "A request enters the router, is validated, and reaches the handler. This walkthrough follows a successful request and explains the validation failure branch.",
  "files": [
    {"path": "src/router.py", "language": "python"},
    {"path": "src/handler.py", "language": "python"}
  ],
  "steps": [
    {
      "id": "route",
      "title": "Choose the handler",
      "file": "src/router.py",
      "start_line": 10,
      "end_line": 18,
      "explanation": "Explain the observed behavior and why it matters. Show how a concrete request moves to the next step.",
      "inputs": "A request with a URL and body.",
      "outputs": "A matching handler, or an error if no route matches.",
      "watch_for": "Describe the actual failure behavior observed in the source."
    },
    {
      "id": "handle",
      "title": "Build the response",
      "file": "src/handler.py",
      "start_line": 1,
      "end_line": 8,
      "explanation": "Continue the same concrete example across the file boundary."
    }
  ],
  "glossary": [
    {"term": "Handler", "definition": "The function called to process this kind of request."}
  ]
}
```

Required document keys: `version` (integer 1), `title`, `summary`, `files` (1–100), `steps` (1–300). Required file keys after bundling: `path`, `language`, `content`, `sha256`. Content is exact, nonempty UTF-8 source text; the lowercase SHA-256 hex digest is computed from `content.encode('utf-8')`. Empty or whitespace-only files are not accepted. Each step requires a unique `id`, `title`, `explanation`, `file`, `start_line`, and `end_line`. `inputs`, `outputs`, `watch_for` and `glossary` are optional. File paths are unique. Multiple steps may reference the same file and overlapping ranges. Use a concise name such as `python`, `typescript`, `go` or `rust` for `language`; highlighting is lightweight and language-independent.

Use the pipx-installed `agentic-louis` command from any directory:

```sh
agentic-louis code-walk bundle /path/to/topic.plan.json --repo /path/to/target-repository
agentic-louis code-walk validate ~/.agentic-louis/walkthroughs/target-repository-topic.walk.json
agentic-louis code-walk serve ~/.agentic-louis/walkthroughs/target-repository-topic.walk.json --repo /path/to/target-repository
```

Run the final `serve` command with the generated artifact path as part of completing the request, keeping the
server alive through a persistent process/session or a background process with
a log. Verify startup and return the live URL. A printed command alone does not
complete the workflow. If the standard port is occupied, use `--port 0` and read
the chosen URL from the server output.

Passing the file opens only that walkthrough, immediately selected in the UI.
Omitting it opens the shared library. `--repo` is local context for source checks
and questions; it does not choose the walkthrough.

No source checkout or activated virtual environment is needed after installation. `agentic-louis code-walk --help` lists the commands. The bundler defaults to `~/.agentic-louis/walkthroughs/<repository-name>-<topic>.walk.json`, creating the directory as needed; `--output` chooses another destination. Starting the viewer automatically opens it in the default browser at `http://127.0.0.1:7432`; use `--no-open` only for remote or headless use. The library lists `*.walk.json` from `~/.agentic-louis/walkthroughs` by default; `--walkthroughs` changes that directory. `--repo` still selects the local repository for source checks and questions. Any valid artifact can also be opened with **Open walkthrough** in the UI.

The viewer compares snapshot hashes with files in `--repo`. Changed or missing files remain readable as snapshots. Chat gets the snapshots, current step, selected line range, and the last 20 conversation messages. Its agent can inspect the selected local repository read-only; when the local code differs it should explain which version it is discussing.
