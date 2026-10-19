import json
from urllib.parse import quote, urlsplit

from .process import command


def latest_checks(checks):
    """Choose an attempt before filtering failures, so successful retries win."""
    groups = {}
    for check in checks:
        groups.setdefault(check["key"], []).append(check)
    result = []
    for key in sorted(groups):
        attempts = groups[key]
        # An existing check run can be retried without acquiring a newer ID.
        # Fall back to IDs when timestamps are missing (e.g. a queued retry).
        timed = all(check.get("attempted_at") for check in attempts)
        result.append(max(attempts, key=lambda check: (
            check["attempted_at"] if timed else "", check["id"])))
    return result


class GitHub:
    def api_pages(self, endpoint):
        return json.loads(command(["gh", "api", "--paginate", "--slurp", endpoint]))

    def watched_prs(self, repository):
        # REST pagination avoids silently dropping PRs after gh pr list's limit.
        viewer = json.loads(command(["gh", "api", "user"]))["login"]
        return [pr for page in self.api_pages(f"repos/{repository}/pulls?state=open&per_page=100")
                for pr in page if pr["user"]["login"].casefold() == viewer.casefold()
                and (pr["head"].get("repo") or {}).get("full_name", "").casefold() == repository.casefold()]

    def watch_pr(self, repository, number):
        pr = json.loads(command(["gh", "api", f"repos/{repository}/pulls/{number}"]))
        if (pr["head"].get("repo") or {}).get("full_name", "").casefold() != repository.casefold():
            raise ValueError("Watching fork PRs is not supported; the head branch must live in the same repository.")
        return pr

    def commit_checks(self, repository, ref):
        endpoint = f"repos/{repository}/commits/{quote(ref, safe='')}"
        runs = [run for page in self.api_pages(endpoint + "/check-runs?per_page=100&filter=latest")
                for run in page["check_runs"]]
        statuses = [status for page in self.api_pages(endpoint + "/statuses?per_page=100") for status in page]
        checks = []
        for run in runs:
            key = f"{run['app']['id']}:{run['name']}"
            checks.append({"key": key, "name": run["name"],
                           "state": (run.get("conclusion") or run["status"]) if run["status"] == "completed" else run["status"],
                           "url": run.get("html_url"), "id": run["id"],
                           "attempted_at": run.get("started_at") or run.get("completed_at")})
        for status in statuses:
            key = "status:" + status["context"]
            checks.append({"key": key, "name": status["context"], "state": status["state"],
                           "url": status.get("target_url"), "id": status["id"],
                           "attempted_at": status.get("created_at")})
        return latest_checks(checks)

    def review_threads(self, repository, number):
        owner, name = repository.split("/")
        query = '''query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$endCursor){
            pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated
              comments(first:100){pageInfo{hasNextPage} nodes{id databaseId body url updatedAt author{login __typename}}}}
          }}}}'''
        pages = json.loads(command(["gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + query,
                                    "-f", "owner=" + owner, "-f", "name=" + name, "-F", f"number={number}"]))
        threads = [thread for page in pages
                   for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]]
        # Fetch complete long conversations rather than handing an agent truncated context.
        for thread in threads:
            if thread["comments"]["pageInfo"]["hasNextPage"]:
                query = '''query($id:ID!,$endCursor:String){node(id:$id){... on PullRequestReviewThread{
                  comments(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}
                    nodes{id databaseId body url updatedAt author{login __typename}}}}}}'''
                pages = json.loads(command(["gh", "api", "graphql", "--paginate", "--slurp",
                                            "-f", "query=" + query, "-f", "id=" + thread["id"]]))
                thread["comments"]["nodes"] = [c for page in pages for c in page["data"]["node"]["comments"]["nodes"]]
        return threads

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
