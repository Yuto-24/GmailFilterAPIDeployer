"""No network: benchmark intent, uncertain responses, and cleanup ownership."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from scripts import benchmark_filter_add as b


class Request:
    def __init__(self, operation):
        self.operation = operation

    def execute(self, num_retries=0):
        assert num_retries == 0
        return self.operation()


class Client:
    def __init__(self, path):
        self.path = path
        self.rows = {"existing": {"criteria": {"from": "existing@example.invalid"},
                                  "action": {"addLabelIds": ["STARRED"]}}}
        self.creates = 0
        self.deletes = []
        self.lose_create_response = False
        self.lose_delete_response = False

    def users(self):
        return self

    def settings(self):
        return self

    def filters(self):
        return self

    def snapshot(self):
        return {"filters": deepcopy(self.rows), "labels": []}, 0.01

    def create(self, userId, body):
        def send():
            state = json.loads(self.path.read_text())
            assert state["rules"].index(body) in state["attempted"]
            self.creates += 1
            fid = "trial" + str(self.creates)
            self.rows[fid] = deepcopy(body)
            if self.lose_create_response:
                raise TimeoutError()
            return {"id": fid}
        return Request(send)

    def delete(self, userId, id):
        def send():
            state = json.loads(self.path.read_text())
            assert state["cleanup"][-1] == {"index": state["rules"].index(self.rows[id]),
                                           "id": id, "status": "intent"}
            assert id != "existing"
            self.deletes.append(id)
            del self.rows[id]
            if self.lose_delete_response:
                raise TimeoutError()
            return {}
        return Request(send)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "pending.json"
        self.client = Client(self.path)
        self.base = deepcopy(self.client.rows)
        patcher = patch.object(b, "fetch", lambda client: client.snapshot())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_restores_exact_baseline(self):
        result = b.run_trial(self.client, "test@example.invalid", self.path, 1, 2)
        self.assertEqual(self.client.rows, self.base)
        self.assertTrue(result["restored"])
        self.assertEqual(b.summary(result)["acknowledged_and_confirmed"], 2)
        for rule in result["rules"]:
            self.assertEqual(rule["criteria"]["query"], rule["criteria"]["negatedQuery"])

    def test_lost_create_response_readback_without_replay_or_next_create(self):
        self.client.lose_create_response = True
        result = b.run_trial(self.client, "test@example.invalid", self.path, 1, 2)
        self.assertEqual(self.client.creates, 1)
        self.assertEqual(self.client.rows, self.base)
        self.assertEqual(b.summary(result)["applied_without_success_response"], 1)

    def test_partial_batch_readback_cleans_only_applied_subset(self):
        def batch(client, requests, callback):
            index, request = requests[1]
            request.execute()
            raise TimeoutError()
        with patch.object(d, "execute_filter_batch", batch):
            result = b.run_trial(self.client, "test@example.invalid", self.path, 2, 2)
        self.assertEqual(result["attempted"], [0, 1])
        self.assertEqual(self.client.creates, 1)
        self.assertEqual(self.client.rows, self.base)
        self.assertEqual(b.summary(result)["applied_without_success_response"], 1)

    def test_lost_delete_response_recovery_does_not_repeat_delete(self):
        self.client.lose_delete_response = True
        with self.assertRaises(d.DeploymentError):
            b.run_trial(self.client, "test@example.invalid", self.path, 1, 1)
        state = json.loads(self.path.read_text())
        self.assertFalse(state["restored"])
        b.cleanup(self.client, state, self.path)
        self.assertEqual(self.client.deletes, ["trial1"])
        self.assertTrue(state["restored"])

    def test_unrelated_change_blocks_all_deletes(self):
        def batch(client, requests, callback):
            requests[0][1].execute()
            client.rows["unrelated"] = {"criteria": {"query": "unrelated"}, "action": {}}
        with patch.object(d, "execute_filter_batch", batch):
            with self.assertRaises(d.DeploymentError):
                b.run_trial(self.client, "test@example.invalid", self.path, 2, 2)
        self.assertEqual(self.client.deletes, [])
        self.assertTrue(self.path.exists())

    def test_pending_trial_blocks_new_writes(self):
        self.path.write_text("{}")
        with self.assertRaises(d.DeploymentError):
            b.run_trial(self.client, "test@example.invalid", self.path, 1, 2)
        self.assertEqual(self.client.creates, 0)

    def test_existing_filter_change_blocks_cleanup(self):
        def batch(client, requests, callback):
            requests[0][1].execute()
            client.rows["existing"]["criteria"] = {"query": "external edit"}
        with patch.object(d, "execute_filter_batch", batch):
            with self.assertRaises(d.DeploymentError):
                b.run_trial(self.client, "test@example.invalid", self.path, 2, 2)
        self.assertEqual(self.client.deletes, [])

    def test_readback_failure_preserves_intent_for_cleanup(self):
        baseline = self.client.snapshot()
        with patch.object(b, "fetch", side_effect=[baseline, TimeoutError()]):
            with self.assertRaises(TimeoutError):
                b.run_trial(self.client, "test@example.invalid", self.path, 1, 2)
        state = json.loads(self.path.read_text())
        self.assertEqual(state["attempted"], [0, 1])
        self.assertFalse(state["restored"])
        b.cleanup(self.client, state, self.path)
        self.assertEqual(self.client.creates, 2)
        self.assertEqual(self.client.rows, self.base)

    def test_recovery_after_crash_before_send_does_not_create(self):
        with patch.object(d, "execute_filter_single", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                b.run_trial(self.client, "test@example.invalid", self.path, 1, 2)
        state = json.loads(self.path.read_text())
        b.cleanup(self.client, state, self.path)
        self.assertEqual(self.client.creates, 0)
        self.assertEqual(self.client.deletes, [])
        self.assertTrue(state["restored"])


if __name__ == "__main__":
    unittest.main()
