"""Remove local Git resources owned by agentic-louis, without remote services."""

from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .config import default_state_dir
from .process import CommandError, atomic_json, command, identity
from .projects import PROJECTS


def record_worktree(state_dir, repository, worktree, branch, *, created_branch=True):
    """Keep ownership evidence even after a worktree is removed manually."""
    worktree = Path(worktree).resolve()
    key = hashlib.sha256(str(worktree).encode()).hexdigest()
    path = Path(state_dir) / "resources" / f"{key}.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, {"repository": str(Path(repository).resolve()), "worktree": str(worktree),
                       "branch": branch, "created_branch": created_branch or (
                           previous.get("branch") == branch and previous.get("created_branch", False))})


def _worktrees(repository):
    output = command(["git", "worktree", "list", "--porcelain", "-z"], cwd=repository)
    result = {}
    for record in output.split("\0\0"):
        fields = {field.partition(" ")[0]: field.partition(" ")[2]
                  for field in record.split("\0") if field}
        if "worktree" in fields:
            result[Path(fields["worktree"]).resolve()] = fields
    return result


def _legacy_branch(path, root):
    relative = path.relative_to(root).parts
    if (len(relative) == 5 and relative[0] == "watch-prs" and relative[2].isdigit()
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", relative[3])
            and relative[4] == "repository"):
        return f"watch-prs/{relative[2]}/{relative[3]}"
    if (len(relative) == 4 and relative[0] == "runs" and relative[2] == "repositories"
            and re.fullmatch(r"[A-Z][A-Z0-9]*-[0-9]+", relative[1])):
        return f"dubloom/{relative[1].lower()}"
    return None


def _candidates(repository, root, registered):
    candidates = {}
    for path, fields in registered.items():
        if path.is_relative_to(root):
            branch = _legacy_branch(path, root)
            if branch and fields.get("branch") == "refs/heads/" + branch:
                candidates[path] = {"worktree": str(path), "branch": branch, "created_branch": True}
    # Older watcher launches recorded the Git common directory in their permissions.
    # This also identifies orphan branches after their worktrees were removed.
    common = command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repository)
    permission = "permissions.agentic-pr.filesystem={" + json.dumps(common) + '="write"}'
    for launch_path in root.glob("watch-prs/*/*/*/launch.json"):
        path = launch_path.parent / "repository"
        branch = _legacy_branch(path, root)
        launch = json.loads(launch_path.read_text())
        if branch and permission in launch.get("args", []):
            candidates[path] = {"worktree": str(path), "branch": branch, "created_branch": True}
    for path in root.glob("resources/*.json"):
        resource = json.loads(path.read_text())
        if Path(resource["repository"]).resolve() != repository:
            continue
        worktree = Path(resource["worktree"]).resolve()
        if not worktree.is_relative_to(root) or worktree == root:
            raise ValueError(f"Invalid managed worktree path: {worktree}")
        candidates[worktree] = resource
    return candidates


def _check_agents(root):
    for pattern in ("watch-prs/*/*/*", "runs/*/attempt-*"):
        for attempt in root.glob(pattern):
            for name in ("supervisor.json", "child.json"):
                path = attempt / name
                if not path.exists():
                    continue
                saved = json.loads(path.read_text())
                if saved.get("pid") and saved.get("identity") and identity(saved["pid"]) == saved["identity"]:
                    raise ValueError(f"An agent (PID {saved['pid']}) is still running in {attempt}. "
                                     "Stop it before cleaning.")


def _lock_processes(lock_path):
    """Find existing lock users, excluding this cleanup's own open descriptor."""
    try:
        output = command(["lsof", "-nP", "-t", "--", str(lock_path)], timeout=5)
    except CommandError:
        return "PID unavailable"
    pids = sorted({int(line) for line in output.splitlines()
                   if line.isdigit() and int(line) != os.getpid()})
    return "PID " + ", ".join(map(str, pids)) if pids else "PID unavailable"


def clean(directory, *, config=None, project=None, state_dir=None, dry_run=False):
    """Clean only selected local repositories and state roots; keep logs/checkpoints."""
    if config:
        repositories = [repo.path.resolve() for repo in config.repositories]
        roots = [config.state_dir.resolve()]
    else:
        directory = Path(directory).resolve()
        repositories = ([directory] if (directory / ".git").exists() else
                        sorted(path.resolve() for path in directory.iterdir()
                               if path.is_dir() and not path.is_symlink() and (path / ".git").exists()))
        roots = ([Path(state_dir).expanduser().resolve()] if state_dir else
                 [default_state_dir(name) for name in ([project] if project else PROJECTS)])
    if not repositories:
        raise ValueError("No local Git repositories found to clean.")
    roots = [root for root in roots if root.is_dir()]
    worktree_count = branch_count = 0
    with ExitStack() as stack:
        for root in roots:
            for service, lock_path in (("worker", root / "worker.lock"),
                                       ("watcher (watch-prs)", root / "watch-prs" / "watch.lock")):
                if dry_run and not lock_path.exists():
                    continue
                if not lock_path.parent.exists():
                    continue
                lock = stack.enter_context(lock_path.open("r+" if dry_run else "a+"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ValueError(
                        f"A {service} or cleanup is running ({_lock_processes(lock_path)}). "
                        "Stop it before cleaning."
                    ) from None
            if identity(os.getpid()) is None:
                raise ValueError("Cleanup needs permission to inspect process identities through ps.")
            _check_agents(root)
        plans = []
        for repository in repositories:
            registered = _worktrees(repository)
            candidates = {}
            for root in roots:
                candidates.update(_candidates(repository, root, registered))
            branches = set(command(["git", "for-each-ref", "--format=%(refname)", "refs/heads"],
                                   cwd=repository).splitlines())
            for path, resource in candidates.items():
                branch = resource["branch"]
                if path == repository:
                    raise ValueError("Refusing to remove the repository's primary worktree.")
                entry = registered.get(path)
                if entry and entry.get("branch") != "refs/heads/" + branch:
                    raise ValueError(f"Managed worktree has changed branches: {path}")
                if entry and "locked" in entry:
                    raise ValueError(f"Managed worktree is locked: {path}")
                if not entry and path.exists():
                    raise ValueError(f"Managed path is no longer a registered Git worktree: {path}")
                delete_branch = resource.get("created_branch", False) and "refs/heads/" + branch in branches
                if delete_branch and any(other != path and fields.get("branch") == "refs/heads/" + branch
                                         for other, fields in registered.items()):
                    raise ValueError(f"Managed branch is checked out in another worktree: {branch}")
                plans.append((repository, path, branch, bool(entry), delete_branch))
        for repository, path, branch, remove_worktree, remove_branch in plans:
            if remove_worktree:
                print(f"{'Would remove' if dry_run else 'Removing'} worktree: {path}")
                if not dry_run:
                    command(["git", "worktree", "remove", "--force", str(path)], cwd=repository, timeout=120)
                worktree_count += 1
            if remove_branch:
                print(f"{'Would delete' if dry_run else 'Deleting'} local branch: {branch}")
                if not dry_run:
                    command(["git", "branch", "-D", "--", branch], cwd=repository)
                branch_count += 1
    print(f"{'Would remove' if dry_run else 'Removed'} {worktree_count} worktree(s) and {branch_count} local branch(es).")
    return 0
