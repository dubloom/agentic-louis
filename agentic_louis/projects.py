import json
from importlib.resources import files
from types import MappingProxyType

REGISTRY = json.loads(files("agentic_louis").joinpath("projects.json").read_text())
PROJECTS = MappingProxyType(REGISTRY["projects"])
MANAGED_LABEL = REGISTRY["managed_label"]
JIRA_PROJECT = REGISTRY["jira_project"]
LIMIT = 5


def project_label(project):
    if not isinstance(project, str) or project not in PROJECTS:
        raise ValueError(f"Unknown project {project!r}. Choose: {', '.join(PROJECTS)}")
    return PROJECTS[project]


def is_managed(issue, project):
    labels = issue.get("fields", {}).get("labels", [])
    return (isinstance(labels, list) and all(isinstance(label, str) for label in labels)
            and issue.get("key", "").startswith(JIRA_PROJECT + "-")
            and set(labels) == {project_label(project), MANAGED_LABEL})
