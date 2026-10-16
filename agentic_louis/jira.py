import base64
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .comments import reference




DEFAULT_BOARD = "https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049"
RUN_PROPERTY = "agentic-louis-run"


class JiraError(Exception):
    def __init__(self, message, *, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("Use an HTTPS URL without credentials, a query, or a fragment.")
    return value.rstrip("/")


def parse_board(value):
    parts = urlsplit(value)
    site = https_url(f"{parts.scheme}://{parts.netloc}")
    match = re.search(r"/boards/(\d+)(?:/|$)", parts.path)
    if not match:
        raise ValueError("The board URL must contain /boards/<board-id>.")
    return site, int(match.group(1))


def quote_jql(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def column_statuses(config, name):
    normalize = lambda text: re.sub(r"[\s_-]+", "", text).casefold()
    columns = config.get("columnConfig", {}).get("columns", [])
    matches = [c for c in columns if normalize(c["name"]) == normalize(name)]
    if len(matches) != 1:
        available = ", ".join(c["name"] for c in columns) or "none"
        raise JiraError(f"Cannot select column {name!r}. Available columns: {available}")
    statuses = [str(s["id"]) for s in matches[0].get("statuses", [])]
    if not statuses:
        raise JiraError(f"Column {name!r} has no mapped statuses.")
    return statuses


def retry_delay(value):
    if not value:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0



def document(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        if line else {"type": "paragraph", "content": []}
        for line in text.splitlines()
    ]}


def plain_text(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "text":
        return value.get("text", "")
    if value.get("type") == "hardBreak":
        return "\n"
    text = "".join(plain_text(child) for child in value.get("content", []))
    return text + ("\n" if value.get("type") in {"paragraph", "heading", "listItem", "codeBlock"} else "")


class ApiError(JiraError):
    def __init__(self, message, *, status=None, retryable=False, uncertain=False, retry_after=0):
        super().__init__(message, retryable=retryable, retry_after=retry_after)
        self.status = status
        self.uncertain = uncertain


class Jira:
    def __init__(self, board_url=DEFAULT_BOARD, timeout=20):
        self.site, self.board_id = parse_board(board_url)
        email, token = os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")
        if not email or not token:
            raise ValueError("Set JIRA_EMAIL and JIRA_API_TOKEN.")
        self.api_base = https_url(os.getenv("JIRA_API_BASE_URL") or self.site)
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {credentials}", "Accept": "application/json",
                        "User-Agent": "agentic-louis/0.1"}
        self.timeout = timeout
        self.opener = build_opener(NoRedirects())

    def request(self, method, path, params=None, payload=None):
        url = self.api_base + path + ("?" + urlencode(params) if params else "")
        headers = {**self.headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                result = json.loads(body) if body else {}
        except HTTPError as error:
            code = error.code
            delay = retry_delay(error.headers.get("Retry-After"))
            error.close()
            raise ApiError(f"Jira {method} failed (HTTP {code}). Check permissions and workflow configuration.",
                           status=code, retryable=code == 429 or code >= 500,
                           uncertain=method != "GET" and code >= 500, retry_after=delay) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError("Cannot reach Jira.", retryable=True, uncertain=method != "GET") from None
        except (ValueError, UnicodeError):
            raise ApiError("Jira returned invalid JSON.", uncertain=method != "GET") from None
        if not isinstance(result, dict):
            raise ApiError("Jira returned an unexpected response.", uncertain=method != "GET")
        return result

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def columns(self):
        config = self.get(f"/rest/agile/1.0/board/{self.board_id}/configuration")
        result = {name: column_statuses(config, name) for name in ("To Do", "In Progress", "In Review")}
        all_statuses = [s for values in result.values() for s in values]
        if len(all_statuses) != len(set(all_statuses)):
            raise ValueError("Workflow columns must map to distinct status IDs.")
        return result

    def issue(self, key):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key):
            raise ValueError("Invalid Jira issue key.")
        issue = self.get(f"/rest/api/3/issue/{key}", {
            "fields": "summary,description,status,labels,created", "properties": RUN_PROPERTY,
        })
        fields = issue.get("fields", {})
        if (not isinstance(fields, dict) or not isinstance(fields.get("summary"), str)
                or not isinstance(fields.get("status"), dict) or not isinstance(fields["status"].get("id"), str)
                or not isinstance(fields.get("labels"), list)):
            raise ApiError(f"Jira returned malformed issue data for {key}.")
        return issue

    def run(self, key):
        value = self.issue(key).get("properties", {}).get(RUN_PROPERTY)
        if value is not None and not isinstance(value, dict):
            raise ApiError(f"Invalid execution checkpoint on {key}.")
        return value

    def save_run(self, key, run):
        self.request("PUT", f"/rest/api/3/issue/{key}/properties/{RUN_PROPERTY}", payload=run)

    def candidates(self, label, statuses):
        jql = f"labels = {quote_jql(label)}"
        if statuses is not None:
            jql += f" AND status IN ({', '.join(quote_jql(s) for s in statuses)})"
        jql += " ORDER BY created ASC"
        params = {"jql": jql, "fields": "summary,status,labels", "maxResults": 100}
        issues = []
        tokens = set()
        while True:
            page = self.get(f"/rest/software/1.0/board/{self.board_id}/issue", params)
            if not isinstance(page.get("issues"), list):
                raise ApiError("Jira response is missing the issues list.")
            issues.extend(page["issues"])
            token = page.get("nextPageToken")
            if page.get("isLast") is True:
                return issues
            if not token:
                if page.get("isLast") is False:
                    raise ApiError("Jira response is missing the next page token.")
                return issues
            if token in tokens:
                raise ApiError("Jira repeated a page token. Poll stopped to avoid an incomplete result.")
            tokens.add(token)
            params["nextPageToken"] = token

    def transition(self, key, destination):
        current = self.issue(key)
        if current["fields"]["status"]["id"] in destination:
            return current
        transitions = self.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        matching = [t for t in transitions if str(t.get("to", {}).get("id")) in destination]
        if len(matching) != 1:
            raise ApiError(f"Expected one available transition for {key}, found {len(matching)}.")
        self.request("POST", f"/rest/api/3/issue/{key}/transitions",
                     payload={"transition": {"id": matching[0]["id"]}})
        current = self.issue(key)
        if current["fields"]["status"]["id"] not in destination:
            raise ApiError(f"Jira did not confirm the transition for {key}.", retryable=True)
        return current

    @staticmethod
    def is_blocked(issue):
        return issue["fields"]["status"].get("name", "").strip().casefold() == "blocked"

    def block(self, key):
        current = self.issue(key)
        if self.is_blocked(current):
            return current
        transitions = self.get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
        matching = [t for t in transitions if t.get("to", {}).get("name", "").strip().casefold() == "blocked"]
        if len(matching) != 1:
            raise ApiError(f"Expected one available transition to Blocked for {key}, found {len(matching)}.")
        return self.transition(key, [str(matching[0]["to"]["id"])])

    def comment_once(self, key, token, text):
        start = 0
        while True:
            page = self.get(f"/rest/api/3/issue/{key}/comment", {"startAt": start, "maxResults": 100})
            comments = page.get("comments", [])
            if any(token in plain_text(c.get("body")) for c in comments):
                return
            start += len(comments)
            if start >= page.get("total", start):
                break
            if not comments:
                raise ApiError("Jira comment pagination stopped early.")
        body = deepcopy(text) if isinstance(text, dict) else document(text)
        body["content"].append(reference(token))
        self.request("POST", f"/rest/api/3/issue/{key}/comment", payload={"body": body})
