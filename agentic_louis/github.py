import json
from urllib.parse import urlsplit

from .process import command


class GitHub:
    def for_branch(self, repository, branch):
        return json.loads(command(["gh", "pr", "list", "--repo", repository, "--head", branch,
                                   "--state", "open", "--json", "url"]))

    def check(self, repository):
        data = json.loads(command(["gh", "repo", "view", repository, "--json", "nameWithOwner,viewerPermission"]))
        if data.get("nameWithOwner", "").casefold() != repository.casefold():
            raise ValueError(f"GitHub repository mismatch: {repository}")
        if data.get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}:
            raise ValueError(f"GitHub write access is required for {repository}.")

    def view(self, url, repository):
        parts = urlsplit(url)
        expected = f"/{repository}/pull/"
        if (parts.scheme != "https" or parts.netloc != "github.com" or parts.query or parts.fragment
                or not parts.path.startswith(expected) or not parts.path[len(expected):].isdigit()):
            raise ValueError(f"Invalid PR URL for {repository}.")
        return json.loads(command(["gh", "pr", "view", url, "--repo", repository, "--json",
                                   "url,state,isDraft,headRefName,baseRefName,headRefOid,headRepository,headRepositoryOwner"]))

    def restore_draft(self, url, repository):
        command(["gh", "pr", "ready", url, "--repo", repository, "--undo"])
        if not self.view(url, repository).get("isDraft"):
            raise ValueError("GitHub did not confirm that the PR is draft.")
