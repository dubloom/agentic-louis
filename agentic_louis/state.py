import fcntl
from contextlib import closing
from pathlib import Path
import time

from .projects import is_managed, project_label


class State:
    """Keep execution checkpoints on Jira and a local process lock."""

    def __init__(self, directory, project, jira):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.project, self.jira = project, jira
        self.known_keys = set()
        self.lock = (self.directory / "worker.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("Another worker owns this artifact directory.") from None

    def get(self, key):
        run = self.jira.run(key)
        if run is not None and (run.get("key") != key or run.get("project") != self.project):
            raise ValueError("Jira execution checkpoint belongs to another task or project.")
        if run is not None:
            self.known_keys.add(key)
        return run

    def all(self):
        keys = {i["key"] for i in self.jira.candidates(project_label(self.project), None) if is_managed(i, self.project)}
        runs = []
        for key in keys | self.known_keys:
            run = self.get(key)
            if run is not None:
                runs.append(run)
        return sorted(runs, key=lambda run: run["key"])

    def save(self, run):
        run.update(project=self.project, updated_at=time.time())
        self.jira.save_run(run["key"], run)
        self.known_keys.add(run["key"])

    def migrate_legacy(self):
        database = self.directory / "worker.sqlite"
        if not database.is_file():
            return
        # Import legacy checkpoints. Jira checkpoints always take precedence.
        import json
        import sqlite3
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            project = db.execute("SELECT project FROM settings").fetchone()
            if not project or project[0] != self.project:
                raise ValueError("Legacy database belongs to another project.")
            for row in db.execute("SELECT data FROM runs"):
                run = json.loads(row[0])
                if self.get(run["key"]) is None and is_managed(self.jira.issue(run["key"]), self.project):
                    self.save(run)

    def close(self):
        if not self.lock.closed:
            self.lock.close()
