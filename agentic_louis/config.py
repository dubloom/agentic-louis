from dataclasses import dataclass
import json
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
    setup: tuple = ()
    folder: str | None = None


@dataclass(frozen=True)
class Workspace:
    project: str
    state_dir: Path
    profile: str | None
    repositories: tuple
    interval: float = 5
    timeout: float = 7200
    board_url: str = "https://datadoghq.atlassian.net/jira/software/projects/LTRA/boards/37049"
    cursor_remote: str | None = None

    @classmethod
    def load(cls, path, project=None, *, state_dir=None, profile=None):
        """Load an explicit repository allowlist; resolve paths beside the file."""
        path = Path(path).expanduser().resolve()
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Workspace configuration must be a JSON object.")
        unknown = data.keys() - {"project", "state_dir", "profile", "cursor_remote", "repositories"}
        if unknown:
            raise ValueError(f"Unknown workspace settings: {', '.join(sorted(unknown))}")
        configured_project = data.get("project")
        if project and configured_project and project != configured_project:
            raise ValueError("--project conflicts with the workspace configuration.")
        project = project or configured_project
        project_label(project)

        def local_path(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Configured paths must be nonempty strings.")
            return (path.parent / Path(value).expanduser()).resolve()

        def commands(value, field):
            if (not isinstance(value, list) or any(
                    not isinstance(args, list) or not args or any(
                        not isinstance(arg, str) or not arg or "\x00" in arg for arg in args)
                    for args in value)):
                raise ValueError(f"{field} must be an array of nonempty command argument arrays.")
            return tuple(tuple(args) for args in value)

        entries = data.get("repositories")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Workspace configuration needs at least one repositories entry.")
        repos = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.keys() - {"path", "folder", "base", "setup", "checks"}:
                raise ValueError("Repository entries accept only path, folder, base, setup, and checks.")
            repo_path = local_path(entry.get("path"))
            base = entry.get("base")
            if base is not None and (not isinstance(base, str) or not base.strip()):
                raise ValueError("Repository base must be a nonempty branch name.")
            repos.append(cls.repository(repo_path, base=base,
                                        checks=commands(entry.get("checks", []), "checks"),
                                        setup=commands(entry.get("setup", []), "setup"),
                                        folder=entry.get("folder")))
        cls.validate_repositories(repos)
        for field in ("profile", "cursor_remote"):
            if field in data and (not isinstance(data[field], str) or not data[field].strip()
                                  or data[field].startswith("-") or "\x00" in data[field]):
                raise ValueError(f"{field} must be a nonempty value that does not start with '-'.")
        directory = (Path(state_dir).expanduser().resolve() if state_dir else
                     local_path(data["state_dir"]) if "state_dir" in data else default_state_dir(project))
        return cls(project, directory, profile if profile is not None else data.get("profile"),
                   tuple(repos), cursor_remote=data.get("cursor_remote"))

    @staticmethod
    def repository(path, *, base=None, checks=(), setup=(), folder=None):
        if folder is not None:
            if not isinstance(folder, str) or not folder.strip() or Path(folder).is_absolute():
                raise ValueError("Repository folder must be a nonempty relative directory path.")
            target = (path / folder).resolve()
            if not target.is_relative_to(path) or not target.is_dir():
                raise ValueError("Repository folder must be an existing directory inside its repository.")
            folder = target.relative_to(path).as_posix()
        if Path(command(["git", "rev-parse", "--show-toplevel"], cwd=path)).resolve() != path:
            raise ValueError(f"Not a repository root: {path}")
        github = github_repository(command(["git", "remote", "get-url", "origin"], cwd=path))
        if base is None:
            head = command(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=path)
            bases = re.findall(r"^ref: refs/heads/(.+)\s+HEAD$", head, flags=re.MULTILINE)
            if len(bases) != 1:
                raise ValueError(f"Cannot determine the default branch for {path.name}. Check origin HEAD.")
            base = bases[0]
        command(["git", "check-ref-format", "--branch", base])
        name = re.sub(r"[^A-Za-z0-9_-]", "-", path.name).strip("-_") or "repository"
        return Repository(name, path, github, base, checks, setup, folder)

    @staticmethod
    def validate_repositories(repos):
        for attr in ("path", "name", "github"):
            values = [str(getattr(repo, attr)).casefold() if attr == "github" else getattr(repo, attr)
                      for repo in repos]
            if len(set(values)) != len(repos):
                raise ValueError(f"Discovered repositories have duplicate {attr} values.")

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
        repos = [cls.repository(path) for path in paths]
        cls.validate_repositories(repos)
        return cls(project, Path(state_dir).expanduser().resolve() if state_dir else default_state_dir(project),
                   profile, tuple(repos))

