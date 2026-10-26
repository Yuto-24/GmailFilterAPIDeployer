"""Regression tests for bounded, read-only reconciliation and API-call reduction.

All Gmail resources are synthetic; these tests never authenticate or mutate Gmail.
"""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

import gmail_filter_deployer as d
from test_deployer import BASE, TARGET, FakeClient, FakeFilters, FakeLabels, Req, write_xml


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "target.xml"
        write_xml(self.master, TARGET)
        base_file = self.local / "base.xml"
        write_xml(base_file, BASE)
        self.base = d.compile_xml(base_file)
        self.client = FakeClient(self.base)
        self.plan = d.make_plan(None, self.master, self.base, "test@example.invalid", {})
        self.initial, self.labels = d.api_fetch(self.client)

    def test_one_transient_missing_filter_recovers_without_mutation(self):
        first = (self.initial[:-1], self.labels)
        second = (self.initial, self.labels)
        out = StringIO()
        with patch.object(d, "fetch_for_deploy", side_effect=[first, second]) as fetch, \
             patch.object(d.time, "sleep") as sleeper, redirect_stdout(out):
            self.assertTrue(d.verify_partial(
                self.client, self.base, self.plan, add_count=0, remove_count=0))
        self.assertEqual(fetch.call_count, 2)
        sleeper.assert_called_once_with(2)
        self.assertIn("再照合2/3回目で一致", out.getvalue())
        self.assertEqual(len(self.client.filters), len(self.base))

    def test_persistent_missing_filter_fails_closed_after_three_reads(self):
        broken = (self.initial[:-1], self.labels)
        with patch.object(d, "fetch_for_deploy", return_value=broken) as fetch, \
             patch.object(d.time, "sleep") as sleeper:
            with self.assertRaisesRegex(d.DeploymentError, "API読戻しが不一致"):
                d.verify_partial(self.client, self.base, self.plan, 0, 0)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleeper.call_args_list, [call(2), call(5)])
        self.assertEqual(len(self.client.filters), len(self.base))

    def test_same_count_unapproved_external_change_does_not_pass(self):
        tampered = [dict(row) for row in self.initial]
        tampered[0] = {
            "id": "unplanned-filter",
            "filter": {
                "criteria": {"from": "outside@example.invalid"},
                "action": {"addLabelIds": ["label:Service/Example"], "removeLabelIds": []},
            },
        }
        out = StringIO()
        with patch.object(d, "fetch_for_deploy", return_value=(tampered, self.labels)), \
             patch.object(d.time, "sleep"), redirect_stdout(out):
            with self.assertRaises(d.DeploymentError):
                d.verify_partial(self.client, self.base, self.plan, 0, 0, verbose=True)
        self.assertIn("不足=1 / 予定外=1", out.getvalue())
        self.assertIn("id=['unplanned-filter']", out.getvalue())

    def test_readback_glitch_after_delete_recovers_before_any_create(self):
        """Keep the missing ID and full rule data from a transient post-delete mismatch."""
        original_list = FakeFilters.list
        did_glitch = [False]
        base_size = len(self.base)
        def read_with_one_glitch(fake, userId):
            req = original_list(fake, userId)
            def execute():
                result = req.execute()
                if len(fake.p.filters) == base_size - 1 and not did_glitch[0]:
                    did_glitch[0] = True
                    result = {"filter": result["filter"][:-1]}
                return result
            return Req(execute)
        log = self.local / "journal.json"
        out = StringIO()
        with patch.object(FakeFilters, "list", new=read_with_one_glitch), \
             patch.object(d.time, "sleep") as sleeper, redirect_stdout(out):
            d.perform(self.client, self.plan, "test@example.invalid",
                      self.plan["approval_digest"], log, batch_size=1)
        self.assertTrue(did_glitch[0])
        sleeper.assert_called_once_with(2)
        self.assertIn("再照合2/3回目で一致", out.getvalue())
        self.assertEqual(d.counter([x["filter"] for x in d.api_fetch(self.client)[0]]),
                         d.counter(d.compile_xml(self.master)))
        self.assertTrue(json.loads(log.read_text())["verified"])
        snapshots = list((self.local / "diagnostics").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        snapshot = json.loads(snapshots[0].read_text())
        self.assertEqual(snapshot["expected_count"], len(self.base) - 1)
        self.assertEqual(snapshot["observed_count"], len(self.base) - 2)
        self.assertEqual(len(snapshot["missing_filter_ids"]), 1)
        self.assertEqual(snapshot["unexpected_filter_ids"], [])
        before = {row['id']: row['filter'] for row in snapshot['before_filters']}
        observed = {row['id']: row['filter'] for row in snapshot['observed_filters']}
        initial = {row['id']: row['filter'] for row in self.initial}
        missing_id = snapshot['missing_filter_ids'][0]
        self.assertEqual(before[missing_id], initial[missing_id])
        self.assertIn('criteria', before[missing_id])
        self.assertIn('action', before[missing_id])
        self.assertEqual(observed, {fid: rule for fid, rule in before.items() if fid != missing_id})
        self.assertEqual(snapshot['labels'], self.labels)
        self.assertIsNone(snapshot['pending'])  # Responses already acknowledged; boundary readback failed.
        from datetime import datetime
        self.assertIsNotNone(datetime.fromisoformat(snapshot['timestamp_utc']).tzinfo)

    def test_unknown_cached_label_id_falls_back_to_full_fetch(self):
        with patch.object(d, "fetch_for_deploy", wraps=d.fetch_for_deploy) as fetch:
            actual = d.filters_with_known_labels(self.client, {})
        self.assertEqual(d.counter(x["filter"] for x in actual), d.counter(self.base))
        fetch.assert_called_once()

    def test_deletion_avoids_redundant_post_delete_labels_list(self):
        common = [{"from": "shared@example.invalid", "label": "Type/Promotion"}]
        old = [{"from": f"obsolete{i}@example.invalid", "label": "Type/Promotion"}
               for i in range(7)]
        replacement = [{"from": "fresh@example.invalid", "label": "Type/Promotion"}]
        old_path = self.local / "old.xml"
        target_path = self.local / "new.xml"
        write_xml(old_path, common + old)
        write_xml(target_path, common + replacement)
        baseline = d.compile_xml(old_path)
        client = FakeClient(baseline)
        plan = d.make_plan(None, target_path, baseline, "test@example.invalid", {})
        self.assertEqual(len(plan["remove"]), 7)
        label_calls = [0]
        original_list = FakeLabels.list
        def count_labels(fake, userId):
            label_calls[0] += 1
            return original_list(fake, userId)
        with patch.object(FakeLabels, "list", new=count_labels):
            d.perform(client, plan, "test@example.invalid",
                      plan["approval_digest"], self.local / "fast.json", batch_size=1)
        # One complete pre-delete read per deletion, not two; plus initial and final reads.
        self.assertLess(label_calls[0], 2 * len(plan["remove"]))
        self.assertEqual(d.counter(x["filter"] for x in d.api_fetch(client)[0]),
                         d.counter(d.compile_xml(target_path)))


if __name__ == "__main__":
    unittest.main()
