"""Host-side credential acquisition through the workstation's ddtool CLI."""

import re
import subprocess

from .process import child_environment


def gitlab_project_token(owner: str, repository: str, *, timeout: float = 60) -> str:
    """Return a project token in memory; never log command output or credentials.

    Call from the host launcher, where ddtool can access its secure store.
    Calling this function inside a sandbox retains that sandbox's restrictions.
    """
    for name, value in (("owner", owner), ("repository", repository)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
            raise ValueError(f"Invalid GitLab project {name}.")
    if timeout <= 0:
        raise ValueError("ddtool timeout must be positive.")
    try:
        result = subprocess.run(
            ["ddtool", "auth", "gitlab", "project-token", owner, repository],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False, env=child_environment())
    except FileNotFoundError:
        raise ValueError("ddtool is not installed or is unavailable on PATH.") from None
    except subprocess.TimeoutExpired:
        raise ValueError("ddtool GitLab authentication timed out. Authenticate in your host terminal and retry.") from None
    except OSError:
        raise ValueError("Could not execute ddtool for GitLab authentication.") from None
    if result.returncode:
        # Even failed commands may include credentials in stdout or stderr.
        raise ValueError("ddtool could not obtain a GitLab project token. Check authentication in your host terminal.")
    token = result.stdout.strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("ddtool returned an empty or malformed GitLab project token.")
    return token
