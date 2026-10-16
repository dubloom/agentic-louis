from copy import deepcopy
import json
import os
import shlex
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentic_louis.config import Repository, Workspace
from agentic_louis.jira import ApiError, Jira, RUN_PROPERTY, document, plain_text
from agentic_louis.process import atomic_json, child_environment, command
from agentic_louis.projects import MANAGED_LABEL, is_managed, project_label
from agentic_louis.state import State
from agentic_louis.worker import Worker
from agentic_louis.jira import column_statuses, quote_jql, retry_delay


COLUMNS = {"To Do": ["1"], "In Progress": ["2"], "In Review": ["3"]}
STATUS_NAMES = {"1": "To Do", "2": "In Progress", "3": "In Review", "4": "Done", "5": "Blocked", "6": "Ready"}


def issue(key, status="1", project="trajectory", managed=True):
    return {"key": key, "fields": {"summary": f"Task {key}", "description": document("Build this feature."),
            "labels": [project_label(project)] + ([MANAGED_LABEL] if managed else []),
            "status": {"id": status, "name": STATUS_NAMES[status]}}}


class FakeJira:
    site = "https://example.atlassian.net"

    def __init__(self, issues=()):
        self.issues = {i["key"]: deepcopy(i) for i in issues}
        self.transitions = []
        self.comments = {}
        self.comment_error = None

    def columns(self):
        return deepcopy(COLUMNS)

    def issue(self, key):
        return deepcopy(self.issues[key])

    def candidates(self, label, statuses):
        return [deepcopy(i) for i in self.issues.values()
                if label in i["fields"]["labels"] and (statuses is None or i["fields"]["status"]["id"] in statuses)]

    def run(self, key):
        return deepcopy(self.issues.get(key, {}).get("properties", {}).get(RUN_PROPERTY))

    def save_run(self, key, run):
        self.issues[key].setdefault("properties", {})[RUN_PROPERTY] = deepcopy(run)

    def transition(self, key, destination):
        self.transitions.append((key, destination))
        self.issues[key]["fields"]["status"]["id"] = destination[0]
        self.issues[key]["fields"]["status"]["name"] = STATUS_NAMES[destination[0]]
        return self.issue(key)

    def block(self, key):
        if Jira.is_blocked(self.issue(key)):
            return self.issue(key)
        return self.transition(key, ["5"])

    def comment_once(self, key, token, text):
        if self.comment_error:
            raise self.comment_error
        self.comments.setdefault(token, (key, text))


class FakeGitHub:
    def __init__(self):
        self.views = {}
        self.restored = []

    def view(self, url, repository):
        return deepcopy(self.views[url])

    def for_branch(self, repository, branch):
        return [{"url": url} for url, view in self.views.items()
                if view.get("headRefName") == branch and url.startswith(f"https://github.com/{repository}/")]

    def restore_draft(self, url, repository):
        self.restored.append(url)
        self.views[url]["isDraft"] = True


class SchedulingWorker(Worker):
    def prepare(self, run, value):
        run.update(phase="running", attempt=run.get("attempt", 0) + 1)
        self.state.save(run)

    def collect(self, run):
        return


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Workspace("trajectory", self.root, "automation", ())
        self.jira = FakeJira()
        self.state = State(self.root, "trajectory", self.jira)
        self.addCleanup(self.state.close)
        self.github = FakeGitHub()
        self.notifications = []
        self.worker = SchedulingWorker(self.config, self.jira, self.github, self.state,
                                       "https://hooks.slack.com/triggers/test", lambda u, p: self.notifications.append(p))
        self.worker.columns = deepcopy(COLUMNS)

    def add(self, count, status="1", start=1, **kwargs):
        for i in range(start, start + count):
            self.jira.issues[f"LTRA-{i}"] = issue(f"LTRA-{i}", status, **kwargs)

    def test_five_running_blocks_sixth_and_duplicate_polls(self):
        self.add(8)
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(len(self.jira.transitions), 5)
        self.assertEqual(len([r for r in self.state.all() if r["phase"] == "running"]), 5)

    def test_review_tickets_hold_all_slots(self):
        self.add(5, "3")
        self.add(2, start=6)
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.jira.transitions, [])

    def test_mixed_running_review_and_manual_work(self):
        self.add(2, "3")
        self.add(2, "2", start=3)
        self.add(3, start=5)
        self.add(6, "2", start=10, managed=False)
        for key in ("LTRA-3", "LTRA-4"):
            self.state.save({"key": key, "summary": "Running task", "phase": "running", "attempt": 1})
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(len(self.jira.transitions), 1)
        self.assertEqual(len(self.state.all()), 5)

    def test_projects_have_independent_limits(self):
        self.add(7, "3", project="dd-trace-py")
        self.add(7, start=20)
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(len(self.jira.transitions), 5)

    def test_manual_ticket_and_ambiguous_project_labels_are_ignored(self):
        self.add(1, managed=False)
        self.add(1, start=2)
        self.jira.issues["LTRA-2"]["fields"]["labels"].append("dd-trace-py")
        self.assertEqual(self.worker.tick(), 0)

    def test_unknown_labels_and_other_jira_projects_are_ignored(self):
        self.add(1)
        self.jira.issues["LTRA-1"]["fields"]["labels"].append("proj-invented")
        self.jira.issues["OTHER-1"] = issue("OTHER-1")
        self.assertEqual(self.worker.tick(), 0)

    def test_management_label_is_the_enqueue_boundary(self):
        self.add(1, managed=False)
        self.assertEqual(self.worker.tick(), 0)
        self.jira.issues["LTRA-1"]["fields"]["labels"].append(MANAGED_LABEL)
        self.assertEqual(self.worker.tick(), 1)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "running")

    def test_done_releases_one_slot(self):
        self.add(5, "3")
        self.add(1, start=6)
        self.worker.tick()
        self.jira.issues["LTRA-1"]["fields"]["status"]["id"] = "4"
        self.worker.tick()
        self.assertEqual(self.state.get("LTRA-1")["phase"], "released")
        self.assertEqual(self.state.get("LTRA-6")["phase"], "running")

    def test_failed_work_moves_to_blocked_releases_slots_and_notifies_once(self):
        self.add(5, "2")
        self.add(1, start=6)
        self.worker.tick()
        self.assertEqual(len(self.jira.comments), 5)
        self.assertEqual(self.state.get("LTRA-6")["phase"], "running")
        self.assertEqual(len(self.notifications), 5)
        for i in range(1, 6):
            self.assertEqual(self.jira.issue(f"LTRA-{i}")["fields"]["status"]["name"], "Blocked")
            self.assertEqual(self.state.get(f"LTRA-{i}")["phase"], "blocked")
        for payload in self.notifications:
            self.assertIs(payload["blocked"], True)
            self.assertNotIn("prs", payload)
            key = payload["jira_ticket_url"].rsplit("/", 1)[1]
            self.assertEqual(shlex.split(payload["resume_command"]), [
                "agentic-louis", "--project", "trajectory", "--state-dir", str(self.root),
                "blocked", key, "--profile", "automation",
            ])

    def test_restart_and_interrupted_claim_never_launch_again(self):
        self.add(1, "2")
        self.state.save({"key": "LTRA-1", "phase": "claiming", "attempt": 0})
        self.worker.tick()
        self.assertEqual(self.state.get("LTRA-1")["phase"], "blocked")
        self.assertEqual(self.jira.transitions, [("LTRA-1", ["5"])])

    def test_to_do_requeues_every_previous_phase_without_replaying_old_results(self):
        for phase in ("released", "blocked", "failed", "blocking", "notifying_blocked",
                      "review", "notifying", "running", "claiming", "preparing"):
            with self.subTest(phase=phase):
                self.jira.issues["LTRA-1"] = issue("LTRA-1")
                self.state.save({"key": "LTRA-1", "phase": phase, "attempt": 2,
                                 "branch": "dubloom/ltra-1", "repositories": {},
                                 "result": {"outcome": "failed"}, "notification": "sent",
                                 "blocked_notification": "sent", "error": "Old failure"})
                self.assertEqual(self.worker.tick(), 1)
                run = self.state.get("LTRA-1")
                self.assertEqual(run["phase"], "running")
                self.assertEqual(run["attempt"], 3)
                self.assertEqual(run["branch"], "dubloom/ltra-1")
                self.assertEqual(run["notification"], "pending")
                self.assertNotIn("result", run)
                self.assertNotIn("blocked_notification", run)
                self.assertNotIn("error", run)
                self.assertEqual(self.worker.tick(), 1)
                self.assertEqual(self.state.get("LTRA-1")["attempt"], 3)
        self.assertEqual(self.notifications, [])

    def test_to_do_waits_for_capacity_then_requeues(self):
        self.add(5, "3")
        self.add(1, start=6)
        self.state.save({"key": "LTRA-6", "phase": "blocked", "attempt": 1})
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.jira.issue("LTRA-6")["fields"]["status"]["id"], "1")
        self.jira.transition("LTRA-1", ["4"])
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.state.get("LTRA-6")["attempt"], 2)

    def test_manual_review_overrides_running_or_failed_checkpoint(self):
        for phase in ("running", "failed", "verifying", "blocked"):
            with self.subTest(phase=phase):
                self.jira.issues["LTRA-1"] = issue("LTRA-1", "3")
                self.state.save({"key": "LTRA-1", "phase": phase, "attempt": 1})
                with patch.object(self.worker, "stop") as stop:
                    self.assertEqual(self.worker.tick(), 1)
                    stop.assert_called_once()
                self.assertEqual(self.state.get("LTRA-1")["phase"], "review")
                self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["id"], "3")
        self.assertEqual(self.notifications, [])

    def test_manual_blocked_stops_old_work_and_waits_for_ready(self):
        self.add(1, "5")
        self.state.save({"key": "LTRA-1", "phase": "running", "attempt": 1})
        with patch.object(self.worker, "stop") as stop:
            self.assertEqual(self.worker.tick(), 0)
            self.assertTrue(stop.called)
        self.assertEqual(self.worker.tick(), 0)
        self.jira.transition("LTRA-1", ["6"])
        self.assertEqual(self.worker.tick(), 1)
        self.assertEqual(self.state.get("LTRA-1")["attempt"], 2)

    def test_blocked_without_checkpoint_waits_for_ready(self):
        self.add(1, "5")
        self.assertEqual(self.worker.tick(), 0)
        self.assertIsNone(self.state.get("LTRA-1"))
        self.jira.transition("LTRA-1", ["6"])
        self.assertEqual(self.worker.tick(), 1)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "running")
        self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["id"], "2")

    def test_new_failure_waits_for_manual_move_to_ready(self):
        self.add(1, "2")
        self.assertEqual(self.worker.tick(), 0)
        self.assertTrue(Jira.is_blocked(self.jira.issue("LTRA-1")))
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.worker.tick(), 0)
        self.jira.transition("LTRA-1", ["6"])
        self.assertEqual(self.worker.tick(), 1)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "running")
        self.assertEqual(len(self.notifications), 1)

    def test_ready_respects_capacity_and_can_share_an_active_board_column(self):
        self.worker.columns["In Progress"].append("6")
        self.add(5, "3")
        self.add(1, "6", start=6)
        self.state.save({"key": "LTRA-6", "phase": "failed", "attempt": 2})
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.jira.issue("LTRA-6")["fields"]["status"]["name"], "Ready")
        self.jira.transition("LTRA-1", ["4"])
        self.assertEqual(self.worker.tick(), 5)
        self.assertEqual(self.state.get("LTRA-6")["phase"], "running")
        self.assertEqual(self.state.get("LTRA-6")["attempt"], 3)

    def test_process_lock_and_project_state_guard(self):
        with self.assertRaisesRegex(ValueError, "Another worker"):
            State(self.root, "trajectory", self.jira)
        self.state.close()
        other = State(self.root, "dd-trace-py", self.jira)
        self.addCleanup(other.close)
        self.add(1)
        self.jira.save_run("LTRA-1", {"key": "LTRA-1", "project": "trajectory"})
        with self.assertRaisesRegex(ValueError, "another task or project"):
            other.get("LTRA-1")

    def test_retry_reclaims_a_slot_and_reuses_existing_branch(self):
        self.add(1, "2")
        self.worker.tick()
        run = self.state.get("LTRA-1")
        run["branch"] = "agentic/ltra-1"
        self.state.save(run)
        self.worker.retry("LTRA-1")
        self.assertEqual(self.state.get("LTRA-1")["phase"], "running")
        self.assertEqual(self.state.get("LTRA-1")["branch"], "agentic/ltra-1")
        self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["name"], "In Progress")
        with self.assertRaises(ValueError):
            self.worker.retry("LTRA-1")

    def completed(self, phase="verifying"):
        self.add(1, "2")
        run = {"key": "LTRA-1", "summary": "Task", "phase": phase, "attempt": 1,
               "notification": "pending", "result": {"outcome": "success", "summary": "Implemented.",
               "validation": "Tests passed.", "prs": [{"repository": "DataDog/example", "url": "https://github.com/DataDog/example/pull/1"}]}}
        self.state.save(run)
        return run

    def test_ready_comment_transition_and_notification(self):
        run = self.completed()
        with patch.object(self.worker, "verify") as verify:
            self.worker.finish(run)
            self.worker.finish(self.state.get("LTRA-1"))
        verify.assert_called_once()
        self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["id"], "3")
        self.assertEqual(len(self.jira.comments), 1)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(set(self.notifications[0]), {"jira_ticket_name", "jira_ticket_url", "project_label", "prs", "blocked"})
        self.assertIs(self.notifications[0]["blocked"], False)

    def test_jira_error_resumes_completion_without_rerunning_agent(self):
        run = self.completed("commenting")
        self.jira.comment_error = ApiError("Rate limited", status=429, retryable=True)
        with self.assertRaises(ApiError):
            self.worker.finish(run)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "commenting")
        self.jira.comment_error = None
        with patch.object(self.worker, "prepare") as prepare, patch.object(self.worker, "verify") as verify:
            self.worker.finish(self.state.get("LTRA-1"))
        prepare.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(len(self.notifications), 1)

    def test_uncertain_slack_requires_explicit_resend(self):
        run = self.completed("notifying")
        def uncertain(url, payload):
            raise ValueError("Delivery unknown")
        self.worker.notifier = uncertain
        self.worker.notify(run)
        self.assertEqual(run["notification"], "uncertain")
        self.worker.notifier = lambda u, p: self.notifications.append(p)
        self.worker.notify(run)
        self.assertEqual(self.notifications, [])
        self.worker.notify(run, resend=True)
        self.assertEqual(run["notification"], "sent")
        self.assertEqual(len(self.notifications), 1)

    def test_restart_during_slack_send_does_not_repeat(self):
        run = self.completed("notifying")
        run["notification"] = "sending"
        self.worker.notify(run)
        self.assertEqual(run["notification"], "uncertain")
        self.assertEqual(self.notifications, [])

    def test_blocked_transition_reconciles_after_uncertain_jira_write(self):
        self.completed("failed")
        block = self.jira.block
        def uncertain(key):
            block(key)
            raise ApiError("Response lost", retryable=True, uncertain=True)
        with patch.object(self.jira, "block", side_effect=uncertain), self.assertRaises(ApiError):
            self.worker.tick(admit=False)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "blocking")
        self.assertEqual(self.notifications, [])
        self.worker.tick(admit=False)
        self.worker.tick(admit=False)
        self.assertEqual(self.state.get("LTRA-1")["phase"], "blocked")
        self.assertEqual(len(self.notifications), 1)
        self.assertIs(self.notifications[0]["blocked"], True)
        self.assertNotIn("prs", self.notifications[0])
        self.assertEqual(len(self.jira.comments), 1)

    def test_uncertain_blocked_webhook_requires_explicit_resend(self):
        self.completed("failed")
        def uncertain(url, payload):
            self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["name"], "Blocked")
            raise ValueError("Delivery uncertain")
        self.worker.notifier = uncertain
        self.worker.tick(admit=False)
        run = self.state.get("LTRA-1")
        self.assertEqual(run["blocked_notification"], "uncertain")
        self.worker.notifier = lambda url, payload: self.notifications.append(payload)
        self.worker.tick(admit=False)
        self.assertEqual(self.notifications, [])
        self.worker.notify(run, blocked=True, resend=True)
        self.assertEqual(len(self.notifications), 1)
        self.assertNotIn("prs", self.notifications[0])
        self.assertIn("LTRA-1", self.notifications[0]["resume_command"])

    def test_retry_cannot_exceed_project_capacity(self):
        self.completed("failed")
        self.worker.tick(admit=False)
        self.add(5, "3", start=2)
        with self.assertRaisesRegex(ValueError, "five project slots"):
            self.worker.retry("LTRA-1")
        self.assertTrue(Jira.is_blocked(self.jira.issue("LTRA-1")))

    def test_failed_agent_result_blocks_even_when_a_pr_exists(self):
        run = self.completed("running")
        run.update(attempt_dir=str(self.root / "attempt"), launch_token="token")
        Path(run["attempt_dir"]).mkdir()
        result = run.pop("result")
        result.update(outcome="failed", validation="Docker daemon socket unavailable.")
        atomic_json(Path(run["attempt_dir"]) / "exit.json", {"token": "token", "exit_code": 0})
        atomic_json(Path(run["attempt_dir"]) / "result.json", result)
        Worker.collect(self.worker, run)
        self.worker.tick(admit=False)
        self.assertTrue(Jira.is_blocked(self.jira.issue("LTRA-1")))
        self.assertIn("Docker daemon", self.state.get("LTRA-1")["error"])
        self.assertNotIn("prs", self.notifications[0])

    def test_blocked_tickets_mapped_in_progress_do_not_consume_slots(self):
        self.worker.columns["In Progress"].append("5")
        self.add(5, "5", managed=False)
        self.add(1, start=6)
        self.assertEqual(self.worker.tick(), 1)
        self.assertEqual(self.state.get("LTRA-6")["phase"], "running")

    def test_running_collection_uses_saved_exit_and_structured_result(self):
        run = self.completed("running")
        run.update(attempt_dir=str(self.root / "attempt"), launch_token="token")
        Path(run["attempt_dir"]).mkdir()
        atomic_json(Path(run["attempt_dir"]) / "exit.json", {"token": "token", "exit_code": 0})
        atomic_json(Path(run["attempt_dir"]) / "result.json", run.pop("result"))
        Worker.collect(self.worker, run)
        self.assertEqual(run["phase"], "verifying")

    def test_live_supervisor_is_not_duplicated_after_restart(self):
        run = self.completed("running")
        run.update(attempt_dir=str(self.root / "attempt"), launch_token="token")
        Path(run["attempt_dir"]).mkdir()
        atomic_json(Path(run["attempt_dir"]) / "supervisor.json", {"token": "token", "pid": 123, "identity": "start"})
        with patch("agentic_louis.worker.identity", return_value="start"):
            Worker.collect(self.worker, run)
        self.assertEqual(run["phase"], "running")


class GitVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = FakeGitHub()
        self.jira = FakeJira([issue("LTRA-1", "2")])
        self.branch = "dubloom/ltra-1"
        self.repos = []
        self.run = {"key": "LTRA-1", "summary": "Task", "phase": "verifying", "branch": self.branch,
                    "attempt_dir": str(self.root), "repositories": {}, "result": {
                        "outcome": "success", "summary": "Added features", "validation": "Tests passed", "prs": []}}
        self.state = State(self.root / "state", "trajectory", self.jira)
        self.addCleanup(self.state.close)

    def repository(self, name, checks=()):
        remote = self.root / f"{name}.git"
        path = self.root / name
        command(["git", "init", "--bare", str(remote)])
        command(["git", "clone", str(remote), str(path)])
        command(["git", "config", "user.name", "Test User"], cwd=path)
        command(["git", "config", "user.email", "test@example.com"], cwd=path)
        command(["git", "config", "commit.gpgsign", "false"], cwd=path)
        command(["git", "config", "core.hooksPath", "/dev/null"], cwd=path)
        command(["git", "checkout", "-b", "main"], cwd=path)
        (path / "feature.txt").write_text("base\n")
        command(["git", "add", "."], cwd=path)
        command(["git", "commit", "-m", "Base"], cwd=path)
        command(["git", "push", "origin", "main"], cwd=path)
        base = command(["git", "rev-parse", "HEAD"], cwd=path)
        worktree = self.root / f"work-{name}"
        command(["git", "worktree", "add", "-b", self.branch, str(worktree), base], cwd=path)
        github = f"DataDog/{name}"
        repo = Repository(name, path, github, "main", checks)
        self.repos.append(repo)
        self.run["repositories"][name] = {"path": str(worktree), "base_oid": base, "github": github, "base": "main"}
        return repo, worktree

    def publish(self, repo, worktree):
        (worktree / "feature.txt").write_text("feature\n")
        command(["git", "add", "."], cwd=worktree)
        command(["git", "commit", "-m", "Feature"], cwd=worktree)
        command(["git", "push", "origin", self.branch], cwd=worktree)
        url = f"https://github.com/{repo.github}/pull/1"
        self.run["result"]["prs"].append({"repository": repo.github, "url": url})
        owner, name = repo.github.split("/")
        self.github.views[url] = {"url": url, "state": "OPEN", "isDraft": True, "headRefName": self.branch,
                                  "baseRefName": "main", "headRefOid": command(["git", "rev-parse", "HEAD"], cwd=worktree),
                                  "headRepository": {"name": name}, "headRepositoryOwner": {"login": owner}}
        return url

    def worker(self):
        config = Workspace("trajectory", self.root / "state", "automation", tuple(self.repos))
        worker = Worker(config, self.jira, self.github, self.state, "https://hooks.slack.com/triggers/test")
        worker.columns = deepcopy(COLUMNS)
        return worker

    def test_multiple_repositories_and_unchanged_repo(self):
        for name in ("first", "second"):
            repo, path = self.repository(name, (("git", "diff", "--exit-code"),))
            self.publish(repo, path)
        self.repository("unchanged")
        self.worker().verify(self.run)
        self.assertEqual(len(self.run["verified_checks"]), 2)

    def test_missing_pr_for_one_changed_repository(self):
        for name in ("first", "second"):
            repo, path = self.repository(name)
            self.publish(repo, path)
        self.run["result"]["prs"].pop()
        with self.assertRaisesRegex(ValueError, "Missing draft PR"):
            self.worker().verify(self.run)

    def test_non_draft_is_restored_and_ticket_pauses(self):
        repo, path = self.repository("first")
        url = self.publish(repo, path)
        self.github.views[url]["isDraft"] = False
        self.worker().finish(self.run)
        self.assertEqual(self.github.restored, [url])
        self.assertEqual(self.run["phase"], "failed")
        self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["id"], "2")

    def test_failed_run_discovers_unreported_pr_and_restores_draft(self):
        repo, path = self.repository("first")
        url = self.publish(repo, path)
        self.github.views[url]["isDraft"] = False
        self.run.update(phase="failed", attempt=1)
        self.run.pop("result")
        self.worker().audit_failure(self.run)
        self.assertEqual(self.github.restored, [url])
        self.assertEqual(self.run["partial_prs"], [url])

    def test_foreign_branch_is_not_modified(self):
        repo, path = self.repository("first")
        url = self.publish(repo, path)
        self.github.views[url].update(headRefName="somebody-elses-branch", isDraft=False)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.worker().verify(self.run)
        self.assertEqual(self.github.restored, [])

    def test_uncommitted_or_unpushed_changes_are_rejected(self):
        repo, path = self.repository("first")
        self.publish(repo, path)
        (path / "feature.txt").write_text("extra\n")
        with self.assertRaisesRegex(ValueError, "Uncommitted"):
            self.worker().verify(self.run)
        command(["git", "add", "."], cwd=path)
        command(["git", "commit", "-m", "Extra"], cwd=path)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.worker().verify(self.run)

    def test_failed_validation_never_moves_to_review(self):
        repo, path = self.repository("first", (("git", "diff", "--exit-code", "HEAD~1", "HEAD"),))
        self.publish(repo, path)
        self.worker().finish(self.run)
        self.assertEqual(self.run["phase"], "failed")
        self.assertEqual(self.jira.transitions, [])


    def test_full_launch_and_restart_collection_with_fake_codex(self):
        import sys
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        repo, original_worktree = self.repository("first", (("git", "diff", "--exit-code"),))
        command(["git", "worktree", "remove", str(original_worktree)], cwd=repo.path)
        command(["git", "branch", "-D", self.branch], cwd=repo.path)
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        binary = binary_dir / "codex"
        binary.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        binary.chmod(0o755)
        worker = self.worker()
        messages = []
        worker.notifier = lambda url, payload: messages.append(payload)
        run = {"key": "LTRA-1", "summary": "Task", "attempt": 0, "phase": "claiming", "notification": "pending"}
        with patch.dict(os.environ, {"PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]}):
            worker.prepare(run, self.jira.issue("LTRA-1"))
            worker.children["LTRA-1"].wait(timeout=15)
        self.assertEqual(run["branch"], "dubloom/ltra-1")
        launch = json.loads((Path(run["attempt_dir"]) / "launch.json").read_text())
        override = next(a for a in launch["args"] if a.startswith("permissions.agentic-task.filesystem="))
        permission = tomllib.loads(override)["permissions"]["agentic-task"]["filesystem"]
        common = command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo.path)
        self.assertEqual(permission[common], "write")
        result = json.loads((Path(run["attempt_dir"]) / "result.json").read_text())
        path = run["repositories"][repo.name]["path"]
        url = result["prs"][0]["url"]
        self.github.views[url] = {"url": url, "state": "OPEN", "isDraft": True, "headRefName": self.branch,
                                  "baseRefName": "main", "headRefOid": command(["git", "rev-parse", "HEAD"], cwd=path),
                                  "headRepository": {"name": "first"}, "headRepositoryOwner": {"login": "DataDog"}}
        # A new worker instance must collect the saved result without another launch.
        restarted = self.worker()
        restarted.notifier = worker.notifier
        with patch.object(restarted, "prepare") as prepare:
            restarted.tick(admit=False)
            restarted.tick(admit=False)
        prepare.assert_not_called()
        self.assertEqual(self.state.get("LTRA-1")["phase"], "review")
        self.assertEqual(self.jira.issue("LTRA-1")["fields"]["status"]["id"], "3")
        self.assertEqual(len(messages), 1)
        self.assertTrue((Path(run["attempt_dir"]) / "check-first-0.log").exists())


class SharedTests(unittest.TestCase):
    def test_credentials_are_not_passed_to_agents_or_repo_checks(self):
        with patch.dict(os.environ, {"JIRA_API_TOKEN": "secret", "JIRA_EMAIL": "private", "SLACK_WORKFLOW_URL": "secret", "PATH": "/bin"}):
            env = child_environment()
        self.assertNotIn("JIRA_API_TOKEN", env)
        self.assertNotIn("JIRA_EMAIL", env)
        self.assertNotIn("SLACK_WORKFLOW_URL", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_result_contract_rejects_incomplete_work(self):
        for result in ({}, {"outcome": "failed", "summary": "Missing context", "validation": "", "prs": []},
                       {"outcome": "success", "summary": "Done", "validation": "OK", "prs": []}):
            with self.assertRaises(ValueError):
                Worker.validate_result(result)

    def test_jira_column_normalization_query_quoting_and_retry_delay(self):
        self.assertEqual(column_statuses({"columnConfig": {"columns": [{"name": "To Do", "statuses": [{"id": "1"}]}]}}, "to_do"), ["1"])
        self.assertEqual(retry_delay("5"), 5)
        self.assertEqual(quote_jql('a"b'), '"a\\"b"')


FAKE_CODEX = r"""
import json
from pathlib import Path
import subprocess
import sys
prompt = sys.stdin.read()
repositories = json.loads(next(line.removeprefix("Repositories: ") for line in prompt.splitlines() if line.startswith("Repositories: ")))
branch = next(line.removeprefix("Branch: ") for line in prompt.splitlines() if line.startswith("Branch: "))
prs = []
for repo in repositories:
    path = Path(repo["path"])
    (path / "feature.txt").write_text("implemented feature\n")
    for args in (["git", "add", "."], ["git", "commit", "-m", "Implement task"], ["git", "push", "origin", branch]):
        subprocess.run(args, cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    prs.append({"repository": repo["github"], "url": "https://github.com/" + repo["github"] + "/pull/1"})
result = {"outcome": "success", "summary": "Implemented task", "validation": "Git diff checks passed", "prs": prs}
output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps(result))
print(json.dumps({"type": "turn.completed"}))
"""


if __name__ == "__main__":
    unittest.main()
