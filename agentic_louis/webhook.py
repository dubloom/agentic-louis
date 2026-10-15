import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .jira import NoRedirects


def validate_webhook(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "hooks.slack.com"
            or not parts.path.startswith("/triggers/") or not parts.path.removeprefix("/triggers/")
            or parts.query or parts.fragment):
        raise ValueError("Set SLACK_WORKFLOW_URL to the https://hooks.slack.com/triggers/... URL from Slack Workflow Builder.")


def send(url, payload):
    validate_webhook(url)
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with build_opener(NoRedirects()).open(request, timeout=20) as response:
            body = response.read()
            if not 200 <= response.status < 300:
                raise ValueError("Slack did not accept the notification.")
    except HTTPError as error:
        status = error.code
        error.close()
        raise ValueError(f"Slack returned HTTP {status}. Check workflow activity before resending.") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("Slack delivery is uncertain. Check workflow activity before resending.") from None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        result = None
    if isinstance(result, dict) and result.get("ok") is False:
        raise ValueError("Slack returned ok=false. Check workflow activity before resending.")
