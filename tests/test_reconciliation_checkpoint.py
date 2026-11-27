"""Fixed-plan reconciliation reuse must retain approval and external-change checks."""
from contextlib import redirect_stderr
from io import StringIO
import sqlite3
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures
from test_recovery import ApiError


class ReconciliationCheckpointTests(unittest.TestCase):
    plan = fixtures.MessageTests.plan
    execute = fixtures.MessageTests.execute

    def setUp(self):
        fixtures.MessageTests.setUp(self)
        self.plan_obj = self.plan()
        self.reads, self.failures, self.failed_attempts = [], {}, []
        request = self.api.message_request

        def tracked(mid):
            self.reads.append(mid)
            if mid in self.failures:
                def fail():
                    self.failed_attempts.append(mid)
                    raise self.failures[mid]
                return fixtures.Req(fail)
            return request(mid)
        tracking = patch.object(self.api, "message_request", side_effect=tracked)
        tracking.start()
        self.addCleanup(tracking.stop)

    def reconcile(self, phase="事前照合", journal=None, mode="apply"):
        m.reconcile(self.api, self.plan_obj, journal, mode, phase=phase, checkpoint=True)

    def changed(self, mid):
        self.client.history_events.append({"labelsRemoved": [
            {"message": {"id": mid}, "labelIds": ["UNREAD"]}]})

    def saved_ids(self):
        with sqlite3.connect(self.plan_obj.directory / m.RECONCILIATION) as db:
            return {row[0] for row in db.execute("SELECT message FROM verified")}

    def test_interrupted_preflight_reuses_only_durable_validated_reads(self):
        self.failures["c"] = RuntimeError("read interrupted")
        with self.assertRaisesRegex(RuntimeError, "read interrupted"):
            self.execute(self.plan_obj)
        self.assertEqual(self.saved_ids(), {"a", "b"})
        self.assertFalse((self.plan_obj.directory / "progress.sqlite").exists())
        self.assertFalse((self.local / m.PENDING).exists())
        self.reads.clear()
        self.failures.clear()
        self.assertFalse(self.execute(self.plan_obj, answer=False))
        self.assertEqual(self.reads, ["c", "d"])
        self.assertEqual(self.client.writes, [])
        self.plan_obj.verify_files()

    def test_persistent_400_retains_prefix_and_is_bounded_by_existing_retry(self):
        self.failures["c"] = ApiError(400, "failedPrecondition")
        with self.assertRaises(d.DeploymentError) as caught:
            self.execute(self.plan_obj)
        self.assertEqual(self.saved_ids(), {"a", "b"})
        self.assertIn("書込みは未開始", caught.exception.message_stop_context)
        self.assertEqual(self.failed_attempts, ["c", "c", "c"])
        self.assertEqual(self.client.writes, [])
        self.failures.clear()
        self.reads.clear()
        self.assertFalse(self.execute(self.plan_obj, tty=False))
        self.assertEqual(self.reads, ["c", "d"])

    def test_approval_reuses_read_evidence_but_keeps_fresh_write_and_final_checks(self):
        self.assertTrue(self.execute(self.plan_obj))
        # One preflight, one immediate prewrite, one readback, one final GET per ID.
        self.assertEqual({mid: self.reads.count(mid) for mid in "abcd"}, dict.fromkeys("abcd", 4))
        self.assertFalse((self.plan_obj.directory / m.RECONCILIATION).exists())
        self.assertTrue(self.client.writes)

    def test_change_during_approval_is_detected_before_any_write(self):
        def approve(question):
            self.client.mail["a"]["labelIds"].append("TRASH")
            self.changed("a")
            return True
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", side_effect=approve), \
                self.assertRaisesRegex(d.DeploymentError, "所属が変更") as caught:
            m.execute(self.api, self.plan_obj, self.local)
        self.assertEqual(self.reads, ["a", "b", "c", "d", "a"])
        self.assertIn("承認後照合", caught.exception.message_stop_context)
        self.assertEqual(self.client.writes, [])

    def test_interrupted_postapproval_requires_new_approval_on_resume(self):
        def approve(question):
            self.changed("b")
            self.failures["b"] = RuntimeError("postapproval read failed")
            return True
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", side_effect=approve), self.assertRaises(RuntimeError):
            m.execute(self.api, self.plan_obj, self.local)
        self.assertEqual(self.saved_ids(), {"a", "c", "d"})
        self.failures.clear()
        self.reads.clear()
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", return_value=False) as confirm:
            self.assertFalse(m.execute(self.api, self.plan_obj, self.local))
        confirm.assert_called_once()
        self.assertEqual(self.reads, ["b"])
        self.assertEqual(self.client.writes, [])

    def test_cached_message_changed_during_remaining_scan_is_rechecked(self):
        self.failures["c"] = RuntimeError("pause")
        with self.assertRaises(RuntimeError):
            self.reconcile()
        self.failures.clear()
        self.reads.clear()
        def hook(kind):
            if kind == "messages.get" and self.reads[-1] == "d":
                self.client.mail["a"]["labelIds"].append("TRASH")
                self.changed("a")
        self.client.on_request = hook
        with self.assertRaisesRegex(d.DeploymentError, "所属が変更"):
            self.reconcile()
        self.assertEqual(self.reads, ["c", "d", "a"])
        self.assertEqual(self.client.writes, [])

    def test_repeated_same_id_events_cannot_disappear_in_set_difference(self):
        self.reconcile()
        self.changed("a")
        self.reads.clear()
        def hook(kind):
            if kind == "messages.get":
                self.changed("a")
        self.client.on_request = hook
        with self.assertRaisesRegex(d.DeploymentError, "照合中の変更"):
            self.reconcile()
        self.assertEqual(self.reads, ["a", "a", "a"])
        self.assertNotIn("a", self.saved_ids())
        self.assertEqual(self.client.writes, [])

    def test_expired_history_discards_cached_prefix_and_performs_full_read(self):
        self.reconcile()
        self.reads.clear()
        original = self.api.changes
        calls = [0]
        def expired_once(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise ApiError(404, "notFound")
            return original(*args, **kwargs)
        with patch.object(self.api, "changes", side_effect=expired_once):
            self.reconcile()
        self.assertEqual(self.reads, list("abcd"))
        self.assertIn("履歴期限切れ", self.out.getvalue())

    def test_history_failure_is_not_an_empty_delta_or_permission_to_write(self):
        self.reconcile()
        self.reads.clear()
        self.client.history_failure = ApiError(403, "forbidden")
        with self.assertRaises(Exception):
            self.execute(self.plan_obj)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.client.writes, [])

    def test_crash_after_cursor_advance_does_not_restore_invalidated_evidence(self):
        self.reconcile()
        self.changed("b")
        with patch.object(self.api, "snapshots", side_effect=RuntimeError("crash before read")), \
                self.assertRaises(RuntimeError):
            self.reconcile()
        self.assertEqual(self.saved_ids(), {"a", "c", "d"})
        self.reads.clear()
        self.reconcile()
        self.assertEqual(self.reads, ["b"])

    def test_history_expiration_at_end_cannot_publish_old_reads_as_current(self):
        original = self.api.changes
        calls = [0]
        def expire_after_scan(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                self.client.mail["a"]["labelIds"].append("TRASH")
                raise ApiError(404, "notFound")
            return original(*args, **kwargs)
        with patch.object(self.api, "changes", side_effect=expire_after_scan), \
                self.assertRaisesRegex(d.DeploymentError, "所属が変更"):
            self.execute(self.plan_obj)
        self.assertEqual(self.reads, ["a", "b", "c", "d", "a"])
        self.assertEqual(self.client.writes, [])

    def test_large_prefix_resume_and_paginated_delta_preserve_exact_work(self):
        # Exercise multiple 50-row durable commits without real mailbox data.
        for n in range(123):
            self.client.add(f"extra-{n:03}")
        self.plan_obj = self.plan()
        self.reads.clear()
        self.failures["extra-107"] = RuntimeError("late read failure")
        with self.assertRaises(RuntimeError):
            self.reconcile()
        self.assertEqual(len(self.saved_ids()), 111)
        self.failures.clear()
        for mid in ("a", "b", "extra-050", "extra-100", "extra-105"):
            self.changed(mid)
        self.reads.clear()
        self.reconcile()
        expected = ["a", "b", "extra-050", "extra-100", "extra-105"] + [
            f"extra-{n:03}" for n in range(107, 123)]
        self.assertEqual(self.reads, expected)
        self.assertEqual(len(self.saved_ids()), 127)
        self.assertEqual(self.client.writes, [])

    def test_missing_or_stale_history_position_cannot_validate_cached_results(self):
        self.reconcile()
        original = self.api.read
        for response in ({}, {"historyId": "0"}, {"historyId": "1", "history": None}):
            with self.subTest(response=response):
                def malformed(kind, request):
                    return response if kind == "history.list" else original(kind, request)
                with patch.object(self.api, "read", side_effect=malformed), \
                        self.assertRaisesRegex(d.DeploymentError, "履歴の応答"):
                    self.execute(self.plan_obj)
        self.assertEqual(self.client.writes, [])

    def test_new_arrivals_do_not_extend_the_fixed_plan(self):
        self.reconcile()
        self.reads.clear()
        self.client.add("new-mail")
        self.client.history_events.append({"messagesAdded": [{"message": {"id": "new-mail"}}]})
        self.reconcile()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.saved_ids(), set("abcd"))

    def test_deleted_target_is_never_silently_skipped(self):
        self.reconcile()
        del self.client.mail["a"]
        self.client.history_events.append({"messagesDeleted": [{"message": {"id": "a"}}]})
        with self.assertRaises(KeyError):
            self.execute(self.plan_obj)
        self.assertNotIn("a", self.saved_ids())
        self.assertEqual(self.client.writes, [])

    def test_catalog_change_without_message_history_still_stops(self):
        self.reconcile()
        self.client.id_to_label["Manual"] = "Renamed"
        with self.assertRaisesRegex(d.DeploymentError, "ラベル定義"):
            self.execute(self.plan_obj)
        self.assertEqual(self.client.writes, [])

    def test_existing_progress_change_invalidates_read_checkpoint(self):
        self.reconcile()
        self.reads.clear()
        journal = m.Journal(self.plan_obj, create=True)
        self.addCleanup(journal.close)
        self.reconcile(journal=journal)
        self.assertEqual(self.reads, list("abcd"))

    def test_checkpoint_is_removed_before_durable_write_and_failure_retains_intent(self):
        def fail_write(body):
            self.assertFalse((self.plan_obj.directory / m.RECONCILIATION).exists())
            raise TimeoutError("lost write response")
        self.client.on_write = fail_write
        with self.assertRaises(d.DeploymentError) as caught:
            self.execute(self.plan_obj)
        self.assertIn("書込みは開始済み", caught.exception.message_stop_context)
        self.assertTrue((self.local / m.PENDING).exists())
        journal = m.Journal(self.plan_obj)
        self.addCleanup(journal.close)
        self.assertEqual(journal.db.execute("SELECT count(*) FROM intents").fetchone()[0], 1)

    def test_resolved_intent_is_durable_before_its_read_evidence_can_be_reused(self):
        def break_readback(body):
            self.failures[body["ids"][0]] = RuntimeError("readback interrupted")
            raise TimeoutError("write did not apply")
        self.client.on_write = break_readback
        with self.assertRaises(RuntimeError):
            self.execute(self.plan_obj)
        mid = self.client.writes[0]["ids"][0]
        self.assertEqual(mid, "c")
        self.failures.clear()
        journal = m.Journal(self.plan_obj)
        self.addCleanup(journal.close)
        self.assertEqual(journal.state(mid), "pending")
        def hook(kind):
            if kind == "messages.get" and self.reads[-1] == "d":
                # A second connection sees only durable state, as after a crash.
                with sqlite3.connect(self.plan_obj.directory / "progress.sqlite") as db:
                    status = db.execute("SELECT status FROM progress WHERE message=?", (mid,)).fetchone()[0]
                self.assertEqual(status, "new")
                raise KeyboardInterrupt()
        self.client.on_request = hook
        with self.assertRaises(KeyboardInterrupt):
            self.reconcile(journal=journal)
        self.assertIn(mid, self.saved_ids())

    def test_restoration_does_not_reuse_apply_mode_evidence(self):
        self.assertTrue(self.execute(self.plan_obj))
        journal = m.Journal(self.plan_obj)
        self.addCleanup(journal.close)
        self.reconcile(journal=journal)
        self.reads.clear()
        self.reconcile(journal=journal, mode="restore")
        self.assertEqual(self.reads, list("abcd"))

    def test_restore_approval_change_blocks_before_any_restore_write(self):
        self.assertTrue(self.execute(self.plan_obj))
        count = len(self.client.writes)
        def approve(question):
            self.client.mail["a"]["labelIds"].append("Manual")
            self.client.history_events.append({"labelsAdded": [
                {"message": {"id": "a"}, "labelIds": ["Manual"]}]})
            return True
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", side_effect=approve), \
                self.assertRaisesRegex(d.DeploymentError, "ユーザーラベル競合"):
            m.execute(self.api, self.plan_obj, self.local, mode="restore")
        self.assertEqual(len(self.client.writes), count)

    def test_cli_reports_prewrite_stop_without_generic_partial_write_warning(self):
        self.failures["c"] = RuntimeError("synthetic read error")
        args = ["reclassify", "--resume", self.plan_obj.directory.name,
                "--local-dir", str(self.local), "--expected-email", fixtures.ACCOUNT]
        err = StringIO()
        with patch.object(m, "message_client", return_value=self.api), \
                patch("sys.stdin.isatty", return_value=True), redirect_stderr(err):
            result = d.main(args)
        self.assertEqual(result, 3)
        self.assertIn("事前照合で停止", err.getvalue())
        self.assertIn("書込みは未開始", err.getvalue())
        self.assertNotIn("途中まで反映された可能性", err.getvalue())
        self.assertEqual(self.client.writes, [])
