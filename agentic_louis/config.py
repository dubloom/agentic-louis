from dataclasses import dataclass
import os
from pathlib import Path
import re

from .projects import project_label
from .process import command


def github_repository(remote):
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
    if not match:
        raise ValueError("Each origin must point to a GitHub repository without embedded credentials.")
    return match[1]


def default_state_dir(project):
    project_label(project)
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser()
    return (root / "agentic-louis" / project).resolve()


@dataclass(frozen=True)
class Repository:
    name: str
    path: Path
    github: str
    base: str
    checks: tuple


@dataclass(frozen=True)
class Workspace:
    project: str
    state_dir: Path
    profile: str | None
    repositories: tuple
    interval: float = 5
    timeout: float = 7200
    board_url: str = "https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049"

    @classmethod
    def discover(cls, directory, project, *, state_dir=None, profile=None):
        project_label(project)
        directory = Path(directory).resolve()
        if (directory / ".git").exists():
            paths = [directory]
        else:
            paths = sorted((path for path in directory.iterdir()
                            if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
                            and (path / ".git").exists()), key=lambda p: p.name)
        if not paths:
            raise ValueError("No Git repositories found. Run from a repository root or a folder containing repository roots.")
        repos = []
        for path in paths:
            if Path(command(["git", "rev-parse", "--show-toplevel"], cwd=path)).resolve() != path:
                raise ValueError(f"Not a repository root: {path}")
            github = github_repository(command(["git", "remote", "get-url", "origin"], cwd=path))
            head = command(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=path)
            bases = re.findall(r"^ref: refs/heads/(.+)\s+HEAD$", head, flags=re.MULTILINE)
            if len(bases) != 1:
                raise ValueError(f"Cannot determine the default branch for {path.name}. Check origin HEAD.")
            name = re.sub(r"[^A-Za-z0-9_-]", "-", path.name).strip("-_") or "repository"
            repos.append(Repository(name, path, github, bases[0], ()))
        for attr in ("name", "github"):
            if len({getattr(repo, attr) for repo in repos}) != len(repos):
                raise ValueError(f"Discovered repositories have duplicate {attr} values.")
        return cls(project, Path(state_dir).expanduser().resolve() if state_dir else default_state_dir(project),
                   profile, tuple(repos))

