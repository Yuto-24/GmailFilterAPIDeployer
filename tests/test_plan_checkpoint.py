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
        self.assertEqual(sum(q is not None and "is:unread" in q for q in self.client.queries), 3)
        self.assertEqual(plan.desired(plan.row("a")), set())

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
        ids = {name: lid for lid, name in self.client.id_to_label.items()}
        self.client.filters = {"filter_1": {"id": "filter_1", **d.to_api(d.compile_xml(self.master)[0], ids)}}
        with self.assertRaisesRegex(d.DeploymentError, "未完了のメール計画とMaster"):
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

    def test_950_of_994_rules_are_not_replayed_after_interruption(self):
        rules = [{"from": f"sender{i}@example.invalid", "label": "Type/A"}
                 for i in range(994)]
        write_xml(self.master, rules)
        self.client = fixtures.Client(d.compile_xml(self.master))
        self.client.add("a")
        self.api = m.Gmail(self.client)
        def hook(kind):
            if kind == "messages.list" and sum(q is not None for q in self.client.queries) >= 950:
                raise RuntimeError("interrupt rule 951")
        self.client.on_request = hook
        with self.assertRaisesRegex(RuntimeError, "interrupt rule 951"):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        pointer = json.loads((self.local / m.PLANNING).read_text())
        db = sqlite3.connect(m.run_directory(self.local, pointer["run_id"]) / "plan.sqlite")
        self.assertEqual(db.execute("SELECT count(*) FROM rules").fetchone()[0], 950)
        db.close()
        self.client.on_request = None
        plan = self.make_plan()
        self.assertEqual(self.client.queries.count("from:sender0@example.invalid"), 1)
        self.assertEqual(self.client.queries.count("from:sender949@example.invalid"), 1)
        self.assertEqual(self.client.queries.count("from:sender993@example.invalid"), 1)
        self.assertEqual(plan.manifest["summary"]["target"], 1)
        self.assertEqual(self.client.writes, [])

    def pause_plan(self, *, skip_unknown_labels=False):
        original = self.api.ids
        def ids(query=None):
            if query is not None:
                raise RuntimeError("pause before query")
            yield from original(query)
        with patch.object(self.api, "ids", side_effect=ids):
            with self.assertRaisesRegex(RuntimeError, "pause before query"):
                m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT,
                              skip_unknown_labels=skip_unknown_labels)
        pointer = json.loads((self.local / m.PLANNING).read_text(encoding="utf-8"))
        return m.run_directory(self.local, pointer["run_id"])

    def dynamic_rule(self):
        write_xml(self.master, [{"hasTheWord": "newer_than:1d", "label": "Type/A"}])
        self.client = fixtures.Client(d.compile_xml(self.master))
        for mid in ("a", "b"):
            self.client.add(mid)
        self.client.hits = {"(newer_than:1d)": {"a", "b"}}
        self.api = m.Gmail(self.client)

    def test_empty_or_non_object_pointer_is_never_overwritten(self):
        for value in ({}, [], None):
            with self.subTest(value=value):
                path = self.local / m.PLANNING
                path.write_text(json.dumps(value), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(d.DeploymentError, "形式・必須項目"):
                    self.make_plan()
                self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.client.writes)

    def test_japanese_checkpoint_survives_non_utf8_default_encoding(self):
        self.client.id_to_label["Japanese"] = "分類/日本語"
        self.pause_plan()
        from pathlib import Path
        from types import SimpleNamespace
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == m.PLANNING:
                kwargs.setdefault("encoding", "ascii")
            return original(path, *args, **kwargs)
        args = SimpleNamespace(local_dir=self.local, expected_email=fixtures.ACCOUNT)
        with patch.object(Path, "read_text", read):
            m.require_planning_target(self.local, self.master, fixtures.ACCOUNT)
            with patch.object(d, "confirm_interactive", return_value=False):
                m.reset_planning(args)
            plan = self.make_plan()
        self.assertEqual(plan.labels["Japanese"]["name"], "分類/日本語")

    def test_missing_message_row_is_detected_before_reusing_checkpoint(self):
        directory = self.pause_plan()
        with sqlite3.connect(directory / "plan.sqlite") as db:
            db.execute("DELETE FROM messages WHERE id='a'")
        with self.assertRaisesRegex(d.DeploymentError, "対象ID状態"):
            self.make_plan()
        self.assertFalse(self.client.writes)

    def test_account_mismatch_preserves_checkpoint_without_writes(self):
        self.pause_plan()
        pointer = (self.local / m.PLANNING).read_bytes()
        with self.assertRaisesRegex(d.DeploymentError, "Master・アカウント"):
            m.create_plan(self.api, self.local, self.master, "other@example.invalid")
        self.assertEqual((self.local / m.PLANNING).read_bytes(), pointer)
        self.assertFalse(self.client.writes)

    def test_label_event_does_not_authorize_missing_message(self):
        self.pause_plan()
        self.client.mail.pop("a")
        self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        with self.assertRaisesRegex(d.DeploymentError, "削除履歴"):
            self.make_plan()
        self.assertFalse(self.client.writes)

    def test_deleted_deferred_message_does_not_count_as_pending(self):
        self.client.mail["a"]["labelIds"].append("Unknown")
        self.pause_plan(skip_unknown_labels=True)
        self.client.mail.pop("a")
        self.client.history_events.append({"messagesDeleted": [{"message": {"id": "a"}}]})
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT,
                                  skip_unknown_labels=True)
        plan = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(plan.close)
        self.assertEqual(plan.deferred_count, 0)
        self.assertEqual(plan.manifest["summary"]["deferred_unknown_labels"], 0)

    def test_time_dependent_hit_is_refreshed_without_history_changes(self):
        self.dynamic_rule()
        with patch.object(m, "_plan_reconcile", side_effect=RuntimeError("pause")):
            with self.assertRaisesRegex(RuntimeError, "pause"):
                self.make_plan()
        self.client.hits["(newer_than:1d)"] = {"a"}
        plan = self.make_plan()
        self.assertEqual(plan.desired(plan.row("b")), set())
        self.assertTrue(plan.desired(plan.row("a")))
        self.assertEqual(self.client.queries.count("(newer_than:1d)"), 2)

    def test_hit_difference_reclassifies_message_without_its_own_history_event(self):
        self.dynamic_rule()
        with patch.object(m, "_plan_reconcile", side_effect=RuntimeError("pause")):
            with self.assertRaises(RuntimeError):
                self.make_plan()
        self.client.hits["(newer_than:1d)"] = {"a"}
        self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        plan = self.make_plan()
        self.assertEqual(plan.desired(plan.row("b")), set())

    def test_query_replacement_and_classification_rollback_together(self):
        self.dynamic_rule()
        with patch.object(m, "_plan_reconcile", side_effect=RuntimeError("pause")):
            with self.assertRaises(RuntimeError):
                self.make_plan()
        self.client.hits["(newer_than:1d)"] = {"a"}
        with patch.object(m, "_plan_match", side_effect=RuntimeError("classification failure")):
            with self.assertRaisesRegex(RuntimeError, "classification failure"):
                self.make_plan()
        plan = self.make_plan()
        self.assertEqual(plan.desired(plan.row("b")), set())

    def test_repeated_same_id_events_during_search_stop_finalization(self):
        self.dynamic_rule()
        self.pause_plan()
        self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        original = self.api.ids
        def ids(query=None):
            if query is not None:
                # Metadata returns to its original value, but search saw an intermediate state.
                self.client.history_events.extend([
                    {"labelsRemoved": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]}]},
                    {"labelsAdded": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]}]}])
                yield "b"
            else:
                yield from original(query)
        with patch.object(self.api, "ids", side_effect=ids):
            with self.assertRaisesRegex(d.DeploymentError, "再照合中も既存メール"):
                self.make_plan()
        self.assertTrue((self.local / m.PLANNING).exists())
        self.assertFalse(list(self.local.glob("reclassification/*/manifest.json")))
        self.assertFalse(self.client.writes)

    def test_final_metadata_read_is_inside_history_interval(self):
        self.pause_plan()
        self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        calls = [0]
        def hook(kind):
            if kind == "messages.get":
                calls[0] += 1
                if calls[0] % 2 == 0:
                    self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        self.client.on_request = hook
        with self.assertRaisesRegex(d.DeploymentError, "再照合中も既存メール"):
            self.make_plan()
        self.assertEqual(calls[0], 2 * m.MAX_RECONCILE_PASSES)

    def test_reconcile_reads_use_configured_paced_snapshots(self):
        self.pause_plan()
        self.client.history_events.append({"labelsAdded": [{"message": {"id": "a"}}]})
        with patch.object(self.api, "snapshots", wraps=self.api.snapshots) as snapshots, \
             patch.object(self.api, "message", side_effect=AssertionError("unpaced GET")), \
             patch.object(m.time, "sleep") as sleep:
            self.make_plan()
        self.assertEqual(snapshots.call_args_list, [unittest.mock.call(["a"]), unittest.mock.call(["a"])])
        self.assertEqual(sleep.call_count, 2)

    def test_counts_survive_committed_reconcile_then_finalization_failure(self):
        self.pause_plan(skip_unknown_labels=True)
        self.client.mail["a"]["labelIds"].append("Unknown")
        self.client.mail["b"]["labelIds"].append("SPAM")
        self.client.history_events.append({"labelsAdded": [{"message": {"id": mid}} for mid in ("a", "b")]})
        original = self.api.filters_match
        calls = [0]
        def filters(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("after reconcile commit")
            return original(*args, **kwargs)
        with patch.object(self.api, "filters_match", side_effect=filters):
            with self.assertRaisesRegex(RuntimeError, "after reconcile commit"):
                m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT, skip_unknown_labels=True)
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT, skip_unknown_labels=True)
        plan = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(plan.close)
        self.assertEqual(plan.deferred_count, 1)
        self.assertEqual(plan.manifest["summary"]["excluded"], 5)
        self.assertEqual(plan.manifest["summary"]["target"], 2)

    def test_manifest_publication_crash_resumes_only_finalization(self):
        with patch.object(m, "_finish_planning", side_effect=RuntimeError("crash after manifest")):
            with self.assertRaisesRegex(RuntimeError, "crash after manifest"):
                self.make_plan()
        pointer = json.loads((self.local / m.PLANNING).read_text())
        directory = m.run_directory(self.local, pointer["run_id"])
        original = (directory / "plan.sqlite").read_bytes()
        with patch.object(self.api, "snapshots", side_effect=AssertionError("must not replan")):
            plan = self.make_plan()
        self.assertEqual(plan.directory, directory)
        self.assertEqual((directory / "plan.sqlite").read_bytes(), original)
        self.assertFalse((self.local / m.PLANNING).exists())

    def test_invalid_completed_plan_does_not_clear_checkpoint(self):
        with patch.object(m, "_finish_planning", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.make_plan()
        directory = next(self.local.glob("reclassification/*"))
        with (directory / "plan.sqlite").open("ab") as stream:
            stream.write(b"corruption")
        with self.assertRaisesRegex(d.DeploymentError, "ハッシュ不一致"):
            self.make_plan()
        self.assertTrue((self.local / m.PLANNING).exists())

    def test_apply_checks_checkpoint_before_oauth(self):
        self.pause_plan()
        plan_file = self.local / "filter_plan.json"
        plan_file.write_text(json.dumps({"account": fixtures.ACCOUNT, "target_sha256": "different"}))
        from types import SimpleNamespace
        args = SimpleNamespace(plan=plan_file, local_dir=self.local)
        with patch.object(d, "normalization_for_plan"), \
             patch.object(d, "get_client", side_effect=AssertionError("must stop before OAuth")):
            with self.assertRaisesRegex(d.DeploymentError, "Master・アカウント"):
                d.cmd_apply(args)
        m.require_planning_target(self.local, None, fixtures.ACCOUNT, target_sha256=d.file_hash(self.master))

    def test_run_rechecks_generated_plan_before_saving(self):
        self.pause_plan()
        original = d.make_plan
        def changed_master(*args, **kwargs):
            write_xml(self.master, [{"from": "different@example.invalid", "label": "Type/A"}])
            return original(*args, **kwargs)
        with patch.object(d, "get_client", return_value=(self.client, object())), \
             patch.object(d, "account_email", return_value=fixtures.ACCOUNT), \
             patch.object(d, "make_plan", side_effect=changed_master):
            result = d.main(["run", "--target", str(self.master), "--local-dir", str(self.local),
                             "--expected-email", fixtures.ACCOUNT])
        self.assertEqual(result, 2)
        self.assertFalse((self.local / "run_plan.json").exists())
        self.assertFalse(self.client.writes)

    def test_corrupt_pointer_reset_quarantines_original_bytes_only_after_confirmation(self):
        directory = self.pause_plan()
        backup = (directory / "plan.sqlite").read_bytes()
        path = self.local / m.PLANNING
        path.write_bytes(b'{"account":')
        from types import SimpleNamespace
        args = SimpleNamespace(local_dir=self.local, expected_email=fixtures.ACCOUNT)
        with patch.object(d, "confirm_interactive", return_value=False):
            m.reset_planning(args)
        self.assertEqual(path.read_bytes(), b'{"account":')
        with patch.object(d, "confirm_interactive", return_value=True):
            m.reset_planning(args)
        self.assertFalse(path.exists())
        archived = list(self.local.glob("messages_planning.invalid.*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), b'{"account":')
        self.assertEqual((directory / "plan.sqlite").read_bytes(), backup)
        self.assertFalse(self.client.writes)

    def test_corrupt_pointer_reset_never_clears_approved_pending_work(self):
        path = self.local / m.PLANNING
        path.write_bytes(b'{')
        (self.local / "run_pending.json").write_text("{}")
        from types import SimpleNamespace
        args = SimpleNamespace(local_dir=self.local, expected_email=fixtures.ACCOUNT)
        with patch.object(d, "confirm_interactive", side_effect=AssertionError("must not ask")):
            with self.assertRaisesRegex(d.DeploymentError, "承認済み"):
                m.reset_planning(args)
        self.assertEqual(path.read_bytes(), b'{')

    def test_reconcile_honors_read_batch_size_and_pacing(self):
        from test_message_read_batches import Batch
        self.pause_plan()
        self.client.read_batches, self.client.read_fault = [], None
        self.client.new_batch_http_request = lambda callback: Batch(self.client, callback)
        self.api = m.Gmail(self.client, read_batch_size=3)
        self.client.history_events.append({"labelsAdded": [{"message": {"id": mid}} for mid in ("a", "b")]})
        with patch.object(m.time, "monotonic", return_value=0), patch.object(m.time, "sleep") as sleep:
            self.make_plan()
        self.assertEqual(self.client.read_batches, [["a", "b"], ["a", "b"]])
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(0.5), unittest.mock.call(0.5)])

    def test_old_checkpoint_requires_explicit_reset_but_allows_same_master_guard(self):
        self.pause_plan()
        path = self.local / m.PLANNING
        pointer = json.loads(path.read_text())
        pointer["schema"] = "gmail-message-planning-checkpoint/v1"
        m.save(path, pointer)
        m.require_planning_target(self.local, self.master, fixtures.ACCOUNT)
        with self.assertRaisesRegex(d.DeploymentError, "未完了のメール計画"):
            self.make_plan()
        from types import SimpleNamespace
        args = SimpleNamespace(local_dir=self.local, expected_email=fixtures.ACCOUNT)
        with patch.object(d, "confirm_interactive", return_value=True):
            m.reset_planning(args)
        self.assertFalse(path.exists())

    def test_finalization_recovery_preserves_approved_message_pending(self):
        with patch.object(m, "_finish_planning", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.make_plan()
        pointer = json.loads((self.local / m.PLANNING).read_text())
        m.save(self.local / m.PENDING, {"mode": "apply", "run_id": pointer["run_id"]})
        with self.assertRaisesRegex(d.DeploymentError, "未完了のメール更新"):
            self.make_plan()
        self.assertTrue((self.local / m.PLANNING).exists())
        self.assertTrue((self.local / m.PENDING).exists())

    def test_apply_allows_matching_master_and_no_checkpoint(self):
        self.pause_plan()
        from types import SimpleNamespace
        plan = {"account": fixtures.ACCOUNT, "target_sha256": d.file_hash(self.master), "remove": []}
        plan_file = self.local / "filter_plan.json"
        plan_file.write_text(json.dumps(plan))
        args = SimpleNamespace(plan=plan_file, local_dir=self.local, client_secrets=None, token=None,
                               expected_email=fixtures.ACCOUNT, approve=None, log=None, batch_size=1,
                               verbose=False)
        with patch.object(d, "normalization_for_plan"), \
             patch.object(d, "get_client", return_value=(self.client, object())), \
             patch.object(d, "account_email", return_value=fixtures.ACCOUNT), \
             patch.object(d, "perform", return_value=True) as perform:
            d.cmd_apply(args)
            (self.local / m.PLANNING).unlink()
            d.cmd_apply(args)
        self.assertEqual(perform.call_count, 2)
