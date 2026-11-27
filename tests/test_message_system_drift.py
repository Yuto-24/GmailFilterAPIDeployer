"""Synthetic fixed-plan recovery with concurrent ordinary mailbox activity."""
import sqlite3
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures


class SystemDriftTests(unittest.TestCase):
    setUp = fixtures.MessageTests.setUp
    plan = fixtures.MessageTests.plan
    execute = fixtures.MessageTests.execute

    def test_all_observation_phases_preserve_current_system_labels(self):
        self.client.systems.update({"CATEGORY_UPDATES", "RED_STAR"})
        plan = self.plan()
        phases = []
        changes = {"事前照合": "UNREAD", "承認後照合": "INBOX", "更新直前照合": "RED_STAR",
                   "書込み後照合": "IMPORTANT", "最終照合": "CATEGORY_UPDATES"}
        observe = m.observe

        def concurrent_change(api, plan, journal, row, mode, *, now, phase, **kwargs):
            if row["id"] == "a" and phase not in phases:
                phases.append(phase)
                lids = self.client.mail["a"]["labelIds"]
                flag = changes[phase]
                if flag in lids:
                    lids.remove(flag)
                else:
                    lids.append(flag)
                now["labelIds"] = list(lids)
                self.client.history_events.append({"messages": [{"id": "a"}]})
            return observe(api, plan, journal, row, mode, now=now, phase=phase, **kwargs)

        def approve(question):
            self.client.history_events.append({"messages": [{"id": "a"}]})
            return True

        with patch.object(m, "observe", side_effect=concurrent_change), patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", side_effect=approve):
            self.assertTrue(m.execute(self.api, plan, self.local))
        self.assertEqual(phases, list(changes))
        self.assertEqual(set(self.client.mail["a"]["labelIds"]) & self.client.systems,
                         {"RED_STAR", "IMPORTANT", "CATEGORY_UPDATES"})
        self.assertEqual(sum("a" in body["ids"] for body in self.client.writes), 1)

    def query_plan(self, query):
        fixtures.write_xml(self.master, [{"hasTheWord": query, "label": "Type/A"}])
        self.client = fixtures.Client(d.compile_xml(self.master))
        self.api = m.Gmail(self.client)
        self.client.add("a")
        predicate = m.search_spec(d.compile_xml(self.master)[0])["all"][0][0]
        self.client.hits[predicate] = {"a"}
        return self.plan(), predicate

    def test_changed_gmail_search_result_refuses_stale_classification(self):
        plan, predicate = self.query_plan("is:unread")
        self.client.queries.clear()
        self.client.mail["a"]["labelIds"].remove("UNREAD")
        self.client.hits[predicate].clear()
        with self.assertRaisesRegex(d.DeploymentError, "分類結果が固定計画と異なります"):
            self.execute(plan)
        self.assertEqual(self.client.queries, [predicate])
        self.assertEqual(self.client.writes, [])

    def test_system_query_rechecked_after_owned_write_can_still_match(self):
        plan, predicate = self.query_plan("in:inbox")
        self.client.queries.clear()
        self.client.on_write = lambda body: self.client.mail["a"]["labelIds"].append("IMPORTANT")
        self.assertTrue(self.execute(plan))
        self.assertEqual(self.client.queries, [predicate, predicate])
        self.assertEqual(len(self.client.writes), 1)

    def test_unrecognized_system_label_in_saved_catalog_blocks(self):
        self.client.systems.add("FUTURE_SYSTEM")
        plan = self.plan()
        self.client.mail["a"]["labelIds"].append("FUTURE_SYSTEM")
        with self.assertRaisesRegex(d.DeploymentError, "未対応のシステム状態"):
            self.execute(plan)
        self.assertFalse(self.client.writes)

    def test_own_label_removal_preserves_original_custom_label_predicate(self):
        plan, predicate = self.query_plan("label:Manual/Private")
        def concurrent_read(body):
            self.client.hits[predicate].clear()
            self.client.mail["a"]["labelIds"].remove("UNREAD")
        self.client.on_write = concurrent_read
        self.assertTrue(self.execute(plan))
        self.assertEqual(len(self.client.writes), 1)

    def test_opaque_expression_cannot_conflate_own_write_with_external_change(self):
        plan, predicate = self.query_plan("label:Manual/Private OR is:unread")
        def concurrent_read(body):
            self.client.mail["a"]["labelIds"].remove("UNREAD")
            self.client.hits[predicate].clear()
        self.client.on_write = concurrent_read
        for attempt in range(2):
            with self.assertRaisesRegex(d.DeploymentError, "分離できません"):
                self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)

    def test_interrupted_final_stage_reuses_only_valid_prefix_after_approval(self):
        plan = self.plan()
        reconcile = m.reconcile
        get = self.api.message_request

        def interrupt(mid):
            if mid == "c":
                raise RuntimeError("final GET interrupted")
            return get(mid)

        def run_stage(*args, phase, **kwargs):
            if phase == "最終照合":
                with patch.object(self.api, "message_request", side_effect=interrupt):
                    return reconcile(*args, phase=phase, **kwargs)
            return reconcile(*args, phase=phase, **kwargs)

        with patch.object(m, "reconcile", side_effect=run_stage), self.assertRaises(RuntimeError):
            self.execute(plan)
        writes = list(self.client.writes)
        self.client.mail["a"]["labelIds"].remove("UNREAD")
        self.client.history_events.append({"messages": [{"id": "a"}]})
        with patch.object(self.api, "message_request", wraps=get) as reads:
            self.assertFalse(self.execute(plan, answer=False))
            self.assertEqual([call.args[0] for call in reads.call_args_list], ["a", "c", "d"])
            reads.reset_mock()
            self.assertTrue(self.execute(plan))
            reads.assert_not_called()
        self.assertEqual(self.client.writes, writes)

    def test_applied_137_resume_invalidates_old_proof_and_recovers_final_prefix(self):
        for i in range(133):
            self.client.add(f"x{i:03}")
        plan = self.plan()
        final = m.reconcile
        def interrupt(*args, phase, **kwargs):
            if phase == "最終照合":
                raise RuntimeError("simulated older run stopped before final scan")
            return final(*args, phase=phase, **kwargs)
        with patch.object(m, "reconcile", side_effect=interrupt), self.assertRaises(RuntimeError):
            self.execute(plan)
        writes = list(self.client.writes)
        with sqlite3.connect(plan.directory / "progress.sqlite") as db:
            self.assertEqual(db.execute("SELECT status,count(*) FROM progress GROUP BY status").fetchall(),
                             [("applied", 137)])
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='complete'").fetchone(), ("false",))
        with sqlite3.connect(plan.directory / m.RECONCILIATION) as db:
            db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                             "INSERT INTO meta VALUES('identity','old-verifier'),('cursor','1');"
                             "CREATE TABLE verified(message TEXT PRIMARY KEY); INSERT INTO verified VALUES('d');")
        for mid in "abcd":
            self.client.mail[mid]["labelIds"] = sorted(set(self.client.mail[mid]["labelIds"]) ^ {"UNREAD", "STARRED"})
        self.client.queries.clear()
        reads = []
        get = self.api.message_request
        def track(mid):
            reads.append(mid)
            if mid == "d":
                raise RuntimeError("interrupted")
            return get(mid)
        with patch.object(self.api, "message_request", side_effect=track), self.assertRaises(RuntimeError):
            self.execute(plan)
        self.assertEqual(reads, list("abcd"))
        self.client.history_events.append({"messages": [{"id": "b"}]})
        with patch.object(self.api, "message_request", wraps=get) as requests:
            self.assertTrue(self.execute(plan))
        self.assertEqual([call.args[0] for call in requests.call_args_list],
                         ["b", "d"] + [f"x{i:03}" for i in range(133)])
        self.assertEqual(self.client.writes, writes)
        self.assertEqual(self.client.queries, [])
        plan.verify_files()
        self.assertFalse((self.local / m.PENDING).exists())
