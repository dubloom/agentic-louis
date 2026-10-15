import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agentic_louis.state import State
from test_workflow import FakeJira, issue


class JiraStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jira = FakeJira([issue("LTRA-1", "2")])
        self.state = State(self.root, "trajectory", self.jira)
        self.addCleanup(self.state.close)

    def test_checkpoints_survive_restart_through_jira_without_local_database(self):
        self.state.save({"key": "LTRA-1", "phase": "running", "attempt": 1})
        self.state.close()
        restarted = State(self.root, "trajectory", self.jira)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get("LTRA-1")["phase"], "running")
        self.assertEqual([r["key"] for r in restarted.all()], ["LTRA-1"])
        self.assertFalse((self.root / "worker.sqlite").exists())

    def test_legacy_migration_preserves_branch_and_never_overwrites_jira(self):
        with closing(sqlite3.connect(self.root / "worker.sqlite")) as db, db:
            db.execute("CREATE TABLE settings (project TEXT)")
            db.execute("INSERT INTO settings VALUES ('trajectory')")
            db.execute("CREATE TABLE runs (data TEXT)")
            db.execute("INSERT INTO runs VALUES (?)", (json.dumps({"key": "LTRA-1", "phase": "failed", "attempt": 1,
                                                                   "branch": "agentic/ltra-1"}),))
        self.state.migrate_legacy()
        self.assertEqual(self.state.get("LTRA-1")["branch"], "agentic/ltra-1")
        self.state.save({"key": "LTRA-1", "phase": "blocked"})
        self.state.migrate_legacy()
        self.assertEqual(self.state.get("LTRA-1")["phase"], "blocked")


if __name__ == "__main__":
    unittest.main()
