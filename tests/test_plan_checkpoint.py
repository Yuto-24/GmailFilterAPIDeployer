"""Read-only planning checkpoints: crash recovery and live Gmail history changes."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures
from test_deployer import write_xml


class PlanningCheckpointTests(unittest.TestCase):
    setUp = fixtures.MessageTests.setUp
    execute = fixtures.MessageTests.execute
    user_set = fixtures.MessageTests.user_set

    def make_plan(self):
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        plan = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(plan.close)
        return plan

    def test_interruption_after_first_rule_reuses_saved_search_without_writes(self):
        counts = [0]
        def hook(kind):
            if kind == "messages.list":
                counts[0] += 1
                if counts[0] == 7:
                    raise RuntimeError("simulated failure after checkpoint")
        # No previous planner pointer; the metadata phase must finish first.
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        pointer = json.loads((self.local / m.PLANNING).read_text())
        directory = m.run_directory(self.local, pointer["run_id"])
        db = sqlite3.connect(directory / "plan.sqlite")
        completed = db.execute("SELECT count(*) FROM rules").fetchone()[0]
        db.close()
        self.assertTrue((directory / "master.xml").exists())
        self.assertFalse((directory / "manifest.json").exists())
        self.assertEqual(self.client.writes, [])
        self.client.on_request = None
        previous_from_queries = self.client.queries.count("from:a@example.invalid")
        plan = self.make_plan()
        self.assertEqual(self.client.queries.count("from:a@example.invalid"), max(1, previous_from_queries))
        self.assertGreaterEqual(len(self.client.queries), 1)
        self.assertFalse((self.local / m.PLANNING).exists())
        self.assertEqual(plan.manifest["summary"]["target"], 4)

    def test_existing_read_change_rebases_snapshot_without_redeploy(self):
        seen = [False]
        def hook(kind):
            if kind == "messages.list" and not seen[0] and "from:a@example.invalid" in self.client.queries:
                self.client.mail["a"]["labelIds"].remove("UNREAD")
                self.client.history_events.append({"labelsRemoved": [
                    {"message": {"id": "a"}, "labelIds": ["UNREAD"]}]})
                seen[0] = True
        self.client.on_request = hook
        plan = self.make_plan()
        self.assertTrue(seen[0])
        self.assertNotIn("UNREAD", plan.before(plan.row("a")))
        self.assertEqual(plan.manifest["summary"]["target"], 4)
        self.assertEqual(self.client.writes, [])
        self.client.on_request = None
        self.assertTrue(self.execute(plan))
        self.assertNotIn("UNREAD", self.client.mail["a"]["labelIds"])

    def test_mutable_gmail_query_is_refreshed_for_changed_messages(self):
        write_xml(self.master, [{"hasTheWord": "is:unread", "label": "Type/A"}])
        self.client = fixtures.Client(d.compile_xml(self.master))
        self.client.add("a")
        self.client.add("b")
        self.client.hits = {"is:unread": {"a"}, "(is:unread)": {"a"}}
        self.api = m.Gmail(self.client)
        seen = [False]
        def hook(kind):
            if kind == "messages.list" and not seen[0] and any(
                    q and "is:unread" in q for q in self.client.queries):
                self.client.mail["a"]["labelIds"].remove("UNREAD")
                self.client.history_events.append({"labelsRemoved": [
                    {"message": {"id": "a"}, "labelIds": ["UNREAD"]}]})
                self.client.hits["is:unread"] = set()
                self.client.hits["(is:unread)"] = set()
                seen[0] = True
        self.client.on_request = hook
        plan = self.make_plan()
        self.assertTrue(seen[0])
        self.assertEqual(sum(q is not None and "is:unread" in q for q in self.client.queries), 2)
        self.assertNotIn(self.client.id_to_label.get("Type/A"), plan.desired(plan.row("a")))

    def test_deleted_old_message_dropped_only_with_history_evidence(self):
        seen = [False]
        def hook(kind):
            if kind == "messages.list" and not seen[0] and "from:a@example.invalid" in self.client.queries:
                self.client.mail.pop("a")
                self.client.history_events.append({"messagesDeleted": [{"message": {"id": "a"}}]})
                seen[0] = True
        self.client.on_request = hook
        plan = self.make_plan()
        self.assertEqual(plan.manifest["summary"]["target"], 3)
        self.assertFalse(plan.db.execute("SELECT 1 FROM messages WHERE id='a'").fetchone())
        self.assertFalse(self.client.writes)

    def test_old_excluded_message_entering_scope_requires_explicit_replan(self):
        seen = [False]
        def hook(kind):
            if kind == "messages.list" and not seen[0] and "from:a@example.invalid" in self.client.queries:
                self.client.mail["spam"]["labelIds"] = ["INBOX", "Manual"]
                self.client.history_events.append({"labelsRemoved": [
                    {"message": {"id": "spam"}, "labelIds": ["SPAM"]}]})
                seen[0] = True
        self.client.on_request = hook
        with self.assertRaisesRegex(d.DeploymentError, "新規計画"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.assertTrue((self.local / m.PLANNING).exists())
        self.assertFalse(self.client.writes)

    def test_checkpoint_rejects_changed_master_before_reuse(self):
        def hook(kind):
            if kind == "messages.list" and "from:a@example.invalid" in self.client.queries:
                raise RuntimeError("pause")
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "pause"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.client.on_request = None
        write_xml(self.master, [{"from": "else@example.invalid", "label": "Type/A"}])
        with self.assertRaisesRegex(d.DeploymentError, "Master"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.assertFalse(self.client.writes)

    def test_master_drift_blocks_filter_run_before_oauth_and_writes(self):
        def hook(kind):
            if kind == "messages.list" and "from:a@example.invalid" in self.client.queries:
                raise RuntimeError("pause")
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "pause"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.client.on_request = None
        different = self.local / "different.xml"
        write_xml(different, [{"from": "else@example.invalid", "label": "Type/A"}])
        with patch.object(d, "get_client", side_effect=AssertionError("OAuth should not run")):
            result = d.main(["run", "--target", str(different),
                             "--local-dir", str(self.local),
                             "--expected-email", fixtures.ACCOUNT])
        self.assertEqual(result, 2)
        self.assertTrue((self.local / m.PLANNING).exists())
        self.assertFalse(self.client.writes)

    def test_explicit_reset_discards_pointer_only_without_touching_backup(self):
        def hook(kind):
            if kind == "messages.list" and "from:a@example.invalid" in self.client.queries:
                raise RuntimeError("pause")
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "pause"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        pointer = json.loads((self.local / m.PLANNING).read_text())
        backup = m.run_directory(self.local, pointer["run_id"]) / "plan.sqlite"
        self.assertTrue(backup.exists())
        args = type("Args", (), {"local_dir": self.local, "expected_email": fixtures.ACCOUNT})()
        with patch.object(d, "confirm_interactive", return_value=False):
            m.reset_planning(args)
        self.assertTrue((self.local / m.PLANNING).exists())
        with patch.object(d, "confirm_interactive", return_value=True):
            m.reset_planning(args)
        self.assertFalse((self.local / m.PLANNING).exists())
        self.assertTrue(backup.exists())
        self.assertFalse(self.client.writes)

    def test_planning_cache_corruption_is_rejected_without_gmail_writes(self):
        def hook(kind):
            if kind == "messages.list" and "from:a@example.invalid" in self.client.queries:
                raise RuntimeError("pause")
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "pause"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.client.on_request = None
        checkpoint = json.loads((self.local / m.PLANNING).read_text())
        dbpath = m.run_directory(self.local, checkpoint["run_id"]) / "plan.sqlite"
        db = sqlite3.connect(dbpath)
        db.execute("INSERT INTO queries VALUES('injected:query')")
        db.commit()
        db.close()
        with self.assertRaisesRegex(d.DeploymentError, "検索キャッシュ"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.assertFalse(self.client.writes)
