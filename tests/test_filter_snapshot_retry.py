"""Bounded retry of Gmail filter snapshot mismatches before message writes."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
from test_deployer import FakeClient, Req


class SnapshotClient:
    """Minimal read-only fake; each filters.list response is a fresh snapshot."""

    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.calls = 0

    def users(self):
        return SimpleNamespace(settings=lambda: SimpleNamespace(
            filters=lambda: SimpleNamespace(list=lambda userId: Req(self._read))))

    def _read(self):
        self.calls += 1
        value = next(self.snapshots)
        if isinstance(value, BaseException):
            raise value
        return {"filter": deepcopy(value)}


class FilterSnapshotRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.diagnostics = Path(self.tmp.name) / "logs" / "diagnostics"
        self.templates = [
            d.build_template({"from": "a@example.invalid", "label": "Type/A"}),
            d.build_template({"from": "b@example.invalid", "label": "Type/B"}),
        ]
        fake = FakeClient(self.templates)
        self.snapshot = list(fake.filters.values())
        self.labels = {lid: {"name": name, "type": "user"}
                       for lid, name in fake.id_to_label.items()}

    def api(self, responses):
        client = SnapshotClient(responses)
        return m.Gmail(client, diagnostics_dir=self.diagnostics), client

    def records(self):
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.diagnostics.glob("message_filter_mismatch_*.json"))]

    def test_full_match_uses_one_read_without_wait_or_diagnostics(self):
        api, client = self.api([self.snapshot])
        with patch.object(m.time, "sleep") as sleep:
            api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_missing_filter_then_complete_snapshot_recovers(self):
        api, client = self.api([self.snapshot[:-1], self.snapshot])
        with patch.object(m.time, "sleep") as sleep:
            api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 2)
        sleep.assert_called_once_with(30)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["recovered"])
        self.assertEqual(records[0]["attempts"][0]["master_only"], 1)
        self.assertEqual(records[0]["attempts"][0]["gmail_only"], 0)
        self.assertEqual(len(records[0]["last_mismatch"]["master_only"]), 1)
        self.assertEqual(self.diagnostics.stat().st_mode & 0o777, 0o700)
        self.assertEqual(next(self.diagnostics.glob("*.json")).stat().st_mode & 0o777, 0o600)

    def test_stable_drift_stops_after_three_snapshots(self):
        api, client = self.api([self.snapshot[:-1]] * 3)
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaisesRegex(d.DeploymentError, "3回照合しても不一致"):
                api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 3)
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [30, 60])
        records = self.records()
        self.assertFalse(records[0]["recovered"])
        self.assertEqual([v["master_only"] for v in records[0]["attempts"]], [1, 1, 1])

    def test_equal_count_but_different_filter_does_not_pass(self):
        changed = deepcopy(self.snapshot)
        changed[1]["criteria"]["from"] = "other@example.invalid"
        api, client = self.api([changed] * 3)
        with patch.object(m.time, "sleep"):
            with self.assertRaises(d.DeploymentError):
                api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 3)
        last = self.records()[0]["last_mismatch"]
        self.assertEqual(len(last["master_only"]), 1)
        self.assertEqual(len(last["gmail_only"]), 1)

    def test_nontransient_read_error_is_not_mistaken_for_snapshot_drift(self):
        api, client = self.api([RuntimeError("permanent read failure")])
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "permanent read failure"):
                api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()

    def test_unknown_label_id_stops_without_retry(self):
        changed = deepcopy(self.snapshot)
        changed[0]["action"]["addLabelIds"] = ["Unknown_Label_ID"]
        api, client = self.api([changed])
        with patch.object(m.time, "sleep") as sleep:
            with self.assertRaisesRegex(d.DeploymentError, "不明なGmailラベルID"):
                api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()

    def test_time_budget_stops_before_retry_read(self):
        api, client = self.api([self.snapshot[:-1], self.snapshot])
        def expire(_):
            api.deadline = 0
        with patch.object(m.time, "sleep", side_effect=expire):
            with self.assertRaises(m.MessageTimeLimit):
                api.filters_match(self.templates, self.labels)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
