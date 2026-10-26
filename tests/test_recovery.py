"""Fault injection against synthetic Gmail resources; never connects to Gmail."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from test_deployer import BASE, TARGET, FakeClient, FakeFilters, FakeLabels, Req, write_xml


class ApiError(Exception):
    def __init__(self, status=500, reason="backendError"):
        super().__init__("synthetic API error")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "mailFilters_v1.0.0_master.xml"
        baseline = self.local / "baseline.xml"
        write_xml(baseline, BASE)
        write_xml(self.master, TARGET)
        self.base = d.compile_xml(baseline)
        self.client = FakeClient(self.base)
        rows, labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, "test@example.invalid", labels)
        self.log = self.local / "journal.json"
        self.output = StringIO()
        self.start_patch(patch("sys.stdout", self.output))
        self.sleeper = self.start_patch(patch.object(d.time, "sleep"))
        self.start_patch(patch.object(d.random, "uniform", return_value=0.25))

    def start_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def deploy(self, **kwargs):
        kwargs.setdefault("batch_size", 1)  # Legacy single-operation recovery contract.
        d.perform(self.client, self.plan, "test@example.invalid",
                  self.plan["approval_digest"], self.log, **kwargs)

    def events(self, kind):
        return [x for x in json.loads(self.log.read_text())["events"] if x["kind"] == kind]

    def assert_complete(self):
        rows, _ = d.api_fetch(self.client)
        self.assertEqual(d.counter(x["filter"] for x in rows), d.counter(d.compile_xml(self.master)))
        self.assertTrue(json.loads(self.log.read_text())["verified"])

    def fail_first(self, resource, method, *, applied=False, error=None, side_effect=None):
        original = getattr(resource, method)
        calls = []
        def request(fake, **kwargs):
            underlying = original(fake, **kwargs)
            def execute():
                calls.append(deepcopy(kwargs))
                if len(calls) == 1:
                    if applied:
                        underlying.execute()
                    if side_effect:
                        side_effect(fake)
                    raise error if error is not None else ApiError()
                return underlying.execute()
            return Req(execute)
        self.start_patch(patch.object(resource, method, new=request))
        return calls

    def test_delete_500_not_applied_retries_same_id_after_two_reads(self):
        calls = self.fail_first(FakeFilters, "delete")
        self.deploy(verbose=True)
        self.assertEqual(len(calls), len(self.plan["remove"]) + 1)
        self.assertEqual(calls[0]["id"], calls[1]["id"])
        self.assertEqual([e["after_wait"] for e in self.events("reconciled_before_retry")], [False, True])
        self.sleeper.assert_called_once_with(1.25)
        self.assertIn("HTTP 500", self.output.getvalue())
        self.assertIn("次の試行 2/3", self.output.getvalue())
        self.assert_complete()

    def test_delete_500_applied_does_not_delete_again(self):
        calls = self.fail_first(FakeFilters, "delete", applied=True)
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["remove"]))
        self.assertEqual(self.events("reconciled_after_error")[0]["operation"], "delete_filter")
        self.sleeper.assert_not_called()
        self.assert_complete()

    def test_create_500_not_applied_retries_without_duplicate(self):
        calls = self.fail_first(FakeFilters, "create")
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["add"]) + 1)
        self.assertEqual(calls[0]["body"], calls[1]["body"])
        self.assert_complete()

    def test_create_500_applied_recovers_actual_id_without_duplicate(self):
        calls = self.fail_first(FakeFilters, "create", applied=True)
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["add"]))
        self.assertEqual(self.events("reconciled_after_error")[0]["operation"], "create_filter")
        self.assertTrue(all(x["id"] in self.client.filters for x in self.events("create_filter")))
        self.assert_complete()

    def test_create_visible_only_after_backoff_does_not_resend(self):
        # First reconciliation sees a stale pre-state; second sees the actual write.
        calls = self.fail_first(FakeFilters, "create", applied=True)
        original = FakeFilters.list
        reads_after_error = []
        def listing(fake, userId):
            req = original(fake, userId)
            def execute():
                result = req.execute()
                if len(calls) == 1:
                    reads_after_error.append(True)
                    if len(reads_after_error) == 1:
                        result["filter"] = [r for r in result["filter"] if r["id"] != f"filter_{fake.p.n}"]
                return result
            return Req(execute)
        self.start_patch(patch.object(FakeFilters, "list", new=listing))
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["add"]))
        self.assertEqual(len(reads_after_error), 2)  # Two recovery reads; no immediate duplicate list.
        self.sleeper.assert_called_once_with(1.25)
        self.assert_complete()

    def test_label_500_applied_recovers_label_id_without_duplicate(self):
        calls = self.fail_first(FakeLabels, "create", applied=True)
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["new_label_names"]))
        self.assertEqual(self.events("reconciled_after_error")[0]["operation"], "create_label")
        self.assertEqual(len(self.client.id_to_label), len(set(self.client.id_to_label.values())))
        self.assert_complete()

    def test_label_500_not_applied_retries(self):
        calls = self.fail_first(FakeLabels, "create")
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["new_label_names"]) + 1)
        self.assert_complete()

    def test_duplicate_label_names_during_reconciliation_stop(self):
        def duplicate(fake):
            fake.p.id_to_label["unexpected_duplicate"] = self.plan["new_label_names"][0]
        calls = self.fail_first(FakeLabels, "create", applied=True, side_effect=duplicate)
        with self.assertRaisesRegex(d.DeploymentError, "変更結果が不明"):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(self.log.read_text())["completed_add"], 0)
        self.assertEqual(len(self.events("reconciliation_failed")), 1)

    def test_repeated_write_failures_are_bounded_and_keep_progress(self):
        calls = []
        def failing(fake, **kwargs):
            def execute():
                calls.append(kwargs)
                raise ApiError()
            return Req(execute)
        self.start_patch(patch.object(FakeFilters, "delete", new=failing))
        with self.assertRaisesRegex(d.DeploymentError, "再試行上限"):
            self.deploy()
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in self.sleeper.call_args_list], [1.25, 2.25])
        state = json.loads(self.log.read_text())
        self.assertEqual((state["completed_add"], state["completed_remove"]), (0, 0))
        self.assertEqual(len(self.events("api_error")), 3)
        self.assertEqual(len(self.events("reconciled_before_retry")), 5)
        self.assertEqual(len(self.client.filters), len(self.base))

    def test_final_attempt_applied_is_reconciled_even_at_retry_limit(self):
        original = FakeFilters.delete
        calls = []
        def failing(fake, **kwargs):
            req = original(fake, **kwargs)
            def execute():
                calls.append(kwargs)
                if len(calls) == 3:
                    req.execute()
                raise ApiError()
            return Req(execute)
        self.start_patch(patch.object(FakeFilters, "delete", new=failing))
        self.deploy()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.events("reconciled_after_error")[0]["attempt"], 3)
        self.assert_complete()

    def test_rate_limit_exhaustion_stops_then_resumes_the_approved_plan(self):
        """HTTP 429 preserves progress after three attempts and resumes without duplicates."""
        calls = []

        def limited(fake, **kwargs):
            """Reject every delete without changing the synthetic Gmail state."""
            def execute():
                """Record the exact requested ID before returning a rate-limit error."""
                calls.append(kwargs)
                raise ApiError(429)
            return Req(execute)

        with patch.object(FakeFilters, 'delete', limited), self.assertRaisesRegex(d.DeploymentError, '再試行上限'):
            self.deploy()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({row['id'] for row in calls}), 1)
        state = json.loads(self.log.read_text())
        self.assertEqual(state['plan_digest'], self.plan['approval_digest'])
        self.assertEqual((state['completed_add'], state['completed_remove']), (0, 0))
        self.assertEqual(len(self.client.filters), len(self.base))
        self.assertTrue(all(event['error'] == 'HTTP 429' for event in self.events('api_error')))
        self.deploy()
        self.assert_complete()
        self.assertEqual(self.client.n, len(self.base) + len(self.plan['add']))

    def test_reconciliation_read_failure_stops_without_nested_retry_or_next_write(self):
        calls = self.fail_first(FakeFilters, "delete", applied=True)
        original = FakeLabels.list
        read_calls = []
        def listing(fake, **kwargs):
            if calls:
                def fail():
                    read_calls.append(True)
                    raise ApiError()
                return Req(fail)
            return original(fake, **kwargs)
        self.start_patch(patch.object(FakeLabels, "list", new=listing))
        with self.assertRaisesRegex(d.DeploymentError, "変更結果が不明"):
            self.deploy()
        self.assertEqual((len(calls), len(read_calls)), (1, 1))
        self.sleeper.assert_not_called()
        self.assertEqual(json.loads(self.log.read_text())["completed_remove"], 0)
        self.assertEqual(len(self.client.filters), len(self.base) - 1)

    def test_same_count_rule_drift_stops_after_error(self):
        def drift(fake):
            next(iter(fake.p.filters.values()))["criteria"] = {"from": "outside@example.invalid"}
        calls = self.fail_first(FakeFilters, "delete", side_effect=drift)
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.sleeper.assert_not_called()

    def test_same_rule_replaced_with_another_id_stops_before_retry(self):
        def drift(fake):
            old_id = next(iter(fake.p.filters))
            row = fake.p.filters.pop(old_id)
            fake.p.filters["outside_id"] = {**row, "id": "outside_id"}
        calls = self.fail_first(FakeFilters, "delete", side_effect=drift)
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.sleeper.assert_not_called()

    def test_duplicate_created_filters_stop(self):
        def duplicate(fake):
            row = deepcopy(fake.p.filters[f"filter_{fake.p.n}"])
            fake.p.filters["unexpected_duplicate"] = {**row, "id": "unexpected_duplicate"}
        calls = self.fail_first(FakeFilters, "create", applied=True, side_effect=duplicate)
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(self.log.read_text())["completed_add"], 0)

    def test_external_drift_during_backoff_prevents_retry(self):
        calls = self.fail_first(FakeFilters, "delete")
        def drift(delay):
            next(iter(self.client.filters.values()))["criteria"] = {"from": "outside@example.invalid"}
        self.sleeper.side_effect = drift
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.sleeper.assert_called_once()

    def test_network_error_after_success_reconciles(self):
        calls = self.fail_first(FakeFilters, "create", applied=True, error=TimeoutError())
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["add"]))
        self.assertEqual(self.events("api_error")[0]["error"], "TimeoutError")
        self.assert_complete()

    def test_permanent_write_error_is_not_retried(self):
        calls = self.fail_first(FakeFilters, "delete", error=ApiError(403, "domainPolicy"))
        with self.assertRaises(ApiError):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.sleeper.assert_not_called()
        self.assertEqual(self.events("reconciled_before_retry"), [])

    def test_legacy_add_first_plan_retains_order_during_recovery(self):
        del self.plan["operation_order"]
        self.plan["approval_digest"] = d.json_hash({k: v for k, v in self.plan.items()
                                                    if k not in {"created_utc", "approval_digest"}})
        calls = self.fail_first(FakeFilters, "create", applied=True)
        self.deploy()
        self.assertEqual(len(calls), len(self.plan["add"]))
        events = [x["kind"] for x in json.loads(self.log.read_text())["events"]]
        self.assertLess(events.index("create_filter"), events.index("delete_filter"))
        self.assert_complete()

    def test_pending_run_recovers_underreported_progress_and_preserves_approval(self):
        self.plan = d.select_plan_groups(self.plan, ["other"])
        # Keep the approved target path and digest, while simulating a prior failed response.
        pending = self.local / "run_pending.json"
        d.save_json(pending, self.plan)
        journal = self.local / "logs" / (self.plan["approval_digest"] + ".json")
        d.save_json(journal, {"plan_digest": self.plan["approval_digest"], "completed_remove": 0,
                             "completed_add": 0, "events": []})
        existing, _ = d.api_fetch(self.client)
        fid = next(x["id"] for x in existing if x["filter"] == self.plan["remove"][0])
        del self.client.filters[fid]
        with patch.object(d, "get_client", return_value=(self.client, object())), \
             patch.object(d, "account_email", return_value="test@example.invalid"), \
             patch.object(d, "confirm_interactive", return_value=True) as approval, \
             patch.object(d, "make_plan", side_effect=AssertionError("must not replan")), \
             patch.object(FakeFilters, "delete", side_effect=AssertionError("must not repeat delete")), \
             redirect_stderr(self.output):
            self.assertEqual(d.main(["run", "--local-dir", str(self.local), "--expected-email",
                                     "test@example.invalid", "--batch-size", "1"]), 0)
        approval.assert_called_once()
        self.assertFalse(pending.exists())
        self.assertTrue(json.loads(journal.read_text())["verified"])
        self.assertEqual(self.plan["selected_groups"], ["other"])

    def test_failed_run_retains_exact_pending_plan_and_can_resume(self):
        pending = self.local / "run_pending.json"
        self.plan = d.select_plan_groups(self.plan, ["other"])
        d.save_json(pending, self.plan)
        original = pending.read_bytes()
        def fail(fake, **kwargs):
            def execute():
                raise ApiError()
            return Req(execute)
        argv = ["run", "--local-dir", str(self.local), "--expected-email", "test@example.invalid", "--batch-size", "1"]
        with patch.object(d, "get_client", return_value=(self.client, object())), \
             patch.object(d, "account_email", return_value="test@example.invalid"), \
             patch.object(d, "confirm_interactive", return_value=True), \
             redirect_stderr(self.output):
            with patch.object(FakeFilters, "delete", new=fail):
                self.assertEqual(d.main(argv), 2)
            self.assertEqual(pending.read_bytes(), original)
            self.assertEqual(d.main(argv), 0)
        self.assertFalse(pending.exists())

    def prepare_missing_id_skip(self):
        extra = {"criteria": {"from": "obsolete@example.invalid"},
                 "action": {"addLabelIds": ["label:Service/Example"], "removeLabelIds": []}}
        self.base = d.compile_xml(self.local / "baseline.xml") + [extra]
        self.client = FakeClient(self.base)
        rows, labels = d.api_fetch(self.client)
        skipped = d.canonical_template(d.build_template(BASE[1]))
        fid = next(x["id"] for x in rows if x["filter"] == skipped)
        self.client.filters[fid].pop("id")
        rows, labels = d.api_fetch(self.client)
        plan = d.make_plan(None, self.master, self.base, "test@example.invalid", labels)
        self.plan = d.select_plan_groups(plan, ["other"], current=rows)
        self.assertEqual(self.plan["skipped_deletions"][0]["reason"], "missing_filter_id")
        self.assertEqual(len(self.plan["remove"]), 1)
        self.log.unlink(missing_ok=True)
        return fid

    def test_missing_id_skip_preserves_rule_and_deploys_other_changes(self):
        fid = self.prepare_missing_id_skip()
        old = deepcopy(self.client.filters[fid])
        self.deploy()
        self.assertEqual(self.client.filters[fid], old)
        self.assertEqual(d.counter(x["filter"] for x in d.api_fetch(self.client)[0]),
                         d.counter(d.planned_states(self.plan)[1]))
        self.assertTrue(json.loads(self.log.read_text())["verified"])

    def test_missing_id_skip_is_preserved_during_all_write_recovery_paths(self):
        for resource, method in ((FakeFilters, "delete"), (FakeFilters, "create"), (FakeLabels, "create")):
            for applied in (False, True):
                with self.subTest(resource=resource.__name__, method=method, applied=applied):
                    fid = self.prepare_missing_id_skip()
                    old = deepcopy(self.client.filters[fid])
                    original = getattr(resource, method)
                    calls = []
                    def request(fake, **kwargs):
                        req = original(fake, **kwargs)
                        def execute():
                            calls.append(kwargs)
                            if len(calls) == 1:
                                if applied:
                                    req.execute()
                                raise ApiError()
                            return req.execute()
                        return Req(execute)
                    with patch.object(resource, method, new=request):
                        self.deploy()
                    expected = len(self.plan["remove"] if method == "delete" else
                                   self.plan["new_label_names"] if resource is FakeLabels else self.plan["add"])
                    self.assertEqual(len(calls), expected + (0 if applied else 1))
                    self.assertEqual(self.client.filters[fid], old)
                    self.assertEqual(d.counter(x["filter"] for x in d.api_fetch(self.client)[0]),
                                     d.counter(d.planned_states(self.plan)[1]))
                    self.assertTrue(json.loads(self.log.read_text())["verified"])

    def test_changed_idless_rule_stops_recovery(self):
        fid = self.prepare_missing_id_skip()
        def drift(fake):
            fake.p.filters[fid]["criteria"] = {"from": "changed@example.invalid"}
        calls = self.fail_first(FakeFilters, "create", side_effect=drift)
        with self.assertRaisesRegex(d.DeploymentError, "変更結果が不明"):
            self.deploy()
        self.assertEqual(len(calls), 1)
        self.sleeper.assert_not_called()

    def test_duplicate_known_ids_still_stop_before_mutation(self):
        self.prepare_missing_id_skip()
        identified = [x for x in self.client.filters.values() if x.get("id")]
        identified[0]["id"] = identified[-1]["id"]
        with patch.object(FakeFilters, "delete", side_effect=AssertionError("must not delete")):
            with self.assertRaisesRegex(d.DeploymentError, "フィルタIDが重複"):
                self.deploy()



class ReadRetryTests(unittest.TestCase):
    def test_transient_read_statuses_retry_with_bounded_backoff(self):
        for error in (ApiError(500), ApiError(502), ApiError(503), ApiError(504), ApiError(429),
                      ApiError(403, "rateLimitExceeded"), ApiError(403, "userRateLimitExceeded"),
                      TimeoutError(), ConnectionResetError()):
            with self.subTest(error=d.api_error_code(error)), \
                 patch.object(d.time, "sleep") as sleeper, \
                 patch.object(d.random, "uniform", return_value=0.5):
                calls = []
                def fail():
                    calls.append(True)
                    raise error
                with self.assertRaisesRegex(d.DeploymentError, "一時障害"):
                    d.read_operation("synthetic read", fail)
                self.assertEqual(len(calls), 3)
                self.assertEqual([c.args[0] for c in sleeper.call_args_list], [1.5, 2.5])

    def test_permanent_read_failures_are_not_retried(self):
        for error in (ApiError(400), ApiError(401), ApiError(404), ApiError(403, "domainPolicy"),
                      ApiError(403, "dailyLimitExceeded"), ApiError(501), ValueError(), OSError()):
            with self.subTest(error=d.api_error_code(error)), patch.object(d.time, "sleep") as sleeper:
                def fail():
                    raise error
                with self.assertRaises(type(error)):
                    d.read_operation("synthetic read", fail)
                sleeper.assert_not_called()

    def test_read_recovers_on_last_attempt(self):
        client = FakeClient([])
        original = FakeLabels.list
        calls = []
        def listing(fake, **kwargs):
            def execute():
                calls.append(True)
                if len(calls) < 3:
                    raise ApiError()
                return original(fake, **kwargs).execute()
            return Req(execute)
        with patch.object(FakeLabels, "list", new=listing), patch.object(d.time, "sleep"):
            self.assertEqual(d.api_fetch(client), ([], {}))
        self.assertEqual(len(calls), 3)

    def test_cached_filter_read_uses_bounded_retry(self):
        client = FakeClient([])
        calls = []
        def listing(fake, **kwargs):
            def execute():
                calls.append(True)
                if len(calls) == 1:
                    raise ApiError()
                return {"filter": []}
            return Req(execute)
        with patch.object(FakeFilters, "list", new=listing), patch.object(d.time, "sleep"):
            self.assertEqual(d.filters_with_known_labels(client, {}), [])
        self.assertEqual(len(calls), 2)

    def test_malformed_or_mixed_403_reasons_are_not_retryable(self):
        for body in (b"invalid-json", b"{}", b'{"error": {"errors": []}}',
                     b'{"error": {"errors": [{"reason": "rateLimitExceeded"}, {"reason": "domainPolicy"}]}}'):
            err = ApiError(403)
            err.content = body
            self.assertFalse(d.transient_api_error(err))


try:
    import httplib2
    import requests
    from google.auth.credentials import AnonymousCredentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, "SDK integration tests run in the Docker image")
class SdkTransportTests(unittest.TestCase):
    def setUp(self):
        self.http = d.SingleAttemptHttp(AnonymousCredentials())
        self.addCleanup(self.http.close)
        self.api = build("gmail", "v1", http=self.http, cache_discovery=False, static_discovery=True)

    def response(self, status, body):
        result = requests.Response()
        result.status_code = status
        result._content = body
        result.headers["Content-Type"] = "application/json"
        return result

    def test_real_sdk_write_500_is_sent_once(self):
        response = self.response(500, ApiError().content)
        with patch.object(self.http.session, "send", return_value=response) as sender:
            with self.assertRaises(HttpError) as error:
                self.api.users().settings().filters().create(
                    userId="me", body={"criteria": {"from": "example@example.invalid"},
                                       "action": {"addLabelIds": ["STARRED"]}}).execute(num_retries=0)
        self.assertTrue(d.transient_api_error(error.exception))
        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs["timeout"], 60)
        self.assertFalse(sender.call_args.kwargs["allow_redirects"])

    def test_real_sdk_does_not_replay_disconnection_or_timeout(self):
        for error, expected in ((requests.exceptions.ConnectionError("BadStatusLine"), ConnectionError),
                                (requests.exceptions.Timeout(), TimeoutError),
                                (requests.exceptions.ChunkedEncodingError(), ConnectionError)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.http.session, "send", side_effect=error) as sender:
                with self.assertRaises(expected):
                    self.api.users().settings().filters().delete(userId="me", id="synthetic").execute(num_retries=0)
                sender.assert_called_once()
        self.assertEqual(self.http.session.get_adapter("https://gmail.googleapis.com").max_retries.total, 0)

    def test_ssl_error_is_not_retried(self):
        with patch.object(self.http.session, "send", side_effect=requests.exceptions.SSLError()) as sender:
            with self.assertRaises(requests.exceptions.SSLError) as error:
                self.api.users().labels().list(userId="me").execute(num_retries=0)
        self.assertFalse(d.transient_api_error(error.exception))
        sender.assert_called_once()

    def test_401_and_redirect_are_not_replayed(self):
        for status in (401, 307):
            response = self.response(status, b'{}')
            response.headers["Location"] = "https://gmail.googleapis.com/other"
            with self.subTest(status=status), patch.object(self.http.session, "send", return_value=response) as sender:
                # 3xx handling by SDK is irrelevant here; the transport never resends it.
                result, body = self.http.request("https://gmail.googleapis.com/synthetic", "POST", body="{}")
                self.assertEqual(result.status, status)
                sender.assert_called_once()
                self.assertFalse(sender.call_args.kwargs["allow_redirects"])

    def test_json_and_empty_delete_response_remain_compatible(self):
        with patch.object(self.http.session, "send", return_value=self.response(200, b'{"labels": []}')):
            self.assertEqual(self.api.users().labels().list(userId="me").execute(num_retries=0), {"labels": []})
        with patch.object(self.http.session, "send", return_value=self.response(204, b'')):
            self.assertEqual(self.api.users().settings().filters().delete(
                userId="me", id="synthetic").execute(num_retries=0), "")


if __name__ == "__main__":
    unittest.main()
