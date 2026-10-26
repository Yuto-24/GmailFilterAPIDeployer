"""Offline measurements: synthetic time, Gmail resources, and SDK HTTP responses."""
from io import StringIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from test_deployer import BASE, TARGET, FakeClient, Req, write_xml
from test_recovery import SDK_AVAILABLE


class Clock:
    """A manually advanced clock for deterministic duration assertions."""
    now = 0.0

    def __call__(self):
        """Return the controlled monotonic time without advancing it."""
        return self.now

    def advance(self, seconds):
        """Advance synthetic time without real sleeping or network activity."""
        self.now += seconds


class MeasurementTests(unittest.TestCase):
    """Verify isolated API, retry, prompt, and exception measurements."""
    def setUp(self):
        """Isolate the clock, backoff jitter, and console output for each measurement."""
        self.clock = Clock()
        self.output = StringIO()
        for patcher in (patch.object(d.time, "perf_counter", self.clock),
                        patch.object(d.time, "sleep", self.clock.advance),
                        patch.object(d.random, "uniform", return_value=0),
                        patch("sys.stdout", self.output)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_read_retry_counts_attempts_and_excludes_backoff_from_api_time(self):
        """Count all read attempts while keeping backoff outside API latency."""
        calls = []

        def request():
            """Fail two timed reads, then return a response on the third attempt."""
            calls.append(1)
            self.clock.advance(len(calls))
            if len(calls) < 3:
                raise TimeoutError()
            return "response"

        with d.observe_command("run", True) as observation:
            self.assertEqual(d.read_operation("Gmail filters.list", request), "response")
        self.assertEqual(observation.apis["filters.list"], {
            "attempts": 3, "success": 1, "failure": 2, "seconds": 6, "max_seconds": 3})
        self.assertEqual(self.clock.now, 9)  # 6 seconds in API calls, 1+2 in backoff.
        self.assertEqual(observation.approval_wait, 0)
        self.assertIn("平均=2.00s 最大=3.00s", self.output.getvalue())
        self.assertIsNone(d.OBSERVATION.get())

    def test_mutation_reconciled_after_lost_response_is_one_failed_attempt(self):
        """Keep the transport failure count even when readback proves the write applied."""
        def send():
            """Model a write whose response is lost after four seconds."""
            self.clock.advance(4)
            raise TimeoutError()

        with d.observe_command("run") as observation:
            result = d.recover_mutation("Gmail filters.create [1/1]", send,
                lambda: (True, {"id": "synthetic"}), lambda event: None)
        self.assertEqual(result, {"id": "synthetic"})
        self.assertEqual(observation.apis["filters.create"]["attempts"], 1)
        self.assertEqual(observation.apis["filters.create"]["success"], 0)
        self.assertEqual(observation.apis["filters.create"]["failure"], 1)

    def test_mutation_retry_has_two_attempts_but_one_logical_result(self):
        """Count a recovered retry separately without retaining a private label as a key."""
        attempts = []

        def send():
            """Model one failed write attempt followed by a successful retry."""
            attempts.append(1)
            self.clock.advance(2)
            if len(attempts) == 1:
                raise TimeoutError()
            return {"id": "new"}

        with d.observe_command("apply") as observation:
            self.assertEqual(d.recover_mutation("Gmail labels.create private-label", send,
                lambda: (False, None), lambda event: None), {"id": "new"})
        self.assertEqual(set(observation.apis), {"labels.create"})
        self.assertEqual(observation.apis["labels.create"]["attempts"], 2)
        self.assertEqual(observation.apis["labels.create"]["seconds"], 4)
        self.assertNotIn("private-label", repr(observation.apis))

    def test_parent_time_is_not_an_api_and_interrupt_resets_scope(self):
        """Exclude parent timers from API totals and clear interrupted command state."""
        def interrupt():
            """Interrupt an in-flight API operation after three synthetic seconds."""
            self.clock.advance(3)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            with d.observe_command("run", True) as interrupted:
                d.start_phase("[6/6] deployment", deployment=True)
                d.timed_operation("parent deployment", lambda:
                    d.timed_operation("Gmail filters.delete [1/1]", interrupt))
        self.assertEqual(interrupted.deployment_seconds, 3)
        self.assertEqual(set(interrupted.apis), {"filters.delete"})
        self.assertEqual(interrupted.apis["filters.delete"]["failure"], 1)
        self.assertIn("結果: 中断", self.output.getvalue())
        self.assertIn("[フェーズ停止]", self.output.getvalue())
        self.assertIsNone(d.OBSERVATION.get())
        with d.observe_command("apply", True) as fresh:
            pass
        self.assertEqual(fresh.apis, {})
        self.assertIn("Gmail API試行なし", self.output.getvalue())

    def test_single_caught_http_failure_and_callback_failure_are_distinct(self):
        """Distinguish a caught request failure from a failed journal callback."""
        def fail():
            """Raise a transport timeout after two synthetic seconds."""
            self.clock.advance(2)
            raise TimeoutError()

        callbacks = []
        with d.observe_command("apply") as observation:
            d.execute_filter_single(0, Req(fail), lambda *args: callbacks.append(args),
                                    api_kind="filters.delete")
            with self.assertRaises(OSError):
                d.execute_filter_single(1, Req(lambda: {}),
                    lambda *args: (_ for _ in ()).throw(OSError("disk")), api_kind="filters.delete")
        self.assertIsInstance(callbacks[0][2], TimeoutError)
        self.assertEqual(observation.apis["filters.delete"]["success"], 1)
        self.assertEqual(observation.apis["filters.delete"]["failure"], 1)

    def test_input_wait_accumulates_including_eof_without_logging_inside_prompt(self):
        """Accumulate prompt wait and handle EOF without interleaving measurement output."""
        def answer(prompt):
            """Wait synthetically and assert that the prompt interval remains quiet."""
            before = self.output.getvalue()
            self.clock.advance(7)
            self.assertEqual(self.output.getvalue(), before)
            return "yes"

        with d.observe_command("run") as observation, patch.object(d.sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", side_effect=answer):
                self.assertTrue(d.confirm_interactive("group"))
                self.assertTrue(d.confirm_interactive("final"))
            with patch("builtins.input", side_effect=EOFError()):
                self.assertFalse(d.confirm_interactive())
        self.assertEqual(observation.approval_wait, 14)


class CommandMeasurementTests(unittest.TestCase):
    """Exercise CLI phase reporting and exit paths against synthetic Gmail state."""
    def setUp(self):
        """Run the real CLI against temporary XML and timed synthetic Gmail resources."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "mailFilters_v1.0.0_master.xml"
        self.baseline = self.local / "base.xml"
        write_xml(self.master, TARGET)
        write_xml(self.baseline, BASE)
        self.client = FakeClient(d.compile_xml(self.baseline))
        self.clock = Clock()
        self.output = StringIO()
        self.observations = []
        original = d.Observation

        def observation(*args):
            """Capture command-local metrics for assertions after the context is reset."""
            value = original(*args)
            self.observations.append(value)
            return value

        def auth(*args, **kwargs):
            """Model eleven seconds of OAuth work without using real credentials."""
            self.clock.advance(11)  # OAuth/browser wait must not count as approval input.
            return self.client, object()

        original_execute = Req.execute

        def execute(request, *args, **kwargs):
            """Charge two seconds per executed fake request while preserving its behavior."""
            self.clock.advance(2)
            return original_execute(request, *args, **kwargs)

        for patcher in (patch.object(d, "Observation", observation),
                        patch.object(d.time, "perf_counter", self.clock),
                        patch.object(d, "get_client", side_effect=auth),
                        patch.object(d, "account_email", return_value="test@example.invalid"),
                        patch.object(d.sys.stdin, "isatty", return_value=True),
                        patch.object(Req, "execute", execute),
                        patch("sys.stdout", self.output), patch("sys.stderr", StringIO())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.argv = ["run", "--local-dir", str(self.local), "--expected-email", "test@example.invalid"]

    def answer(self, prompt):
        """Approve a prompt after five seconds of synthetic user wait."""
        self.clock.advance(5)
        return "y"

    def test_verbose_six_phases_wait_and_deployment_do_not_double_count(self):
        # Force both sensitive group prompts as well as the final confirmation.
        """Verify all six phases and distinguish OAuth, approval, API, and deployment time."""
        write_xml(self.baseline, BASE[:3] + [{"from": "extra@example.invalid", "label": "Action/Delete"},
                                           {"hasTheWord": "in:spam extra", "shouldTrash": "true"}])
        self.client = FakeClient(d.compile_xml(self.baseline))
        with patch("builtins.input", side_effect=self.answer) as ask:
            self.assertEqual(d.main(self.argv + ["-v"]), 0)
        observation = self.observations[-1]
        self.assertEqual(ask.call_count, 3)
        self.assertEqual(observation.approval_wait, 15)
        output = self.output.getvalue()
        self.assertEqual(len(re.findall(r"\[フェーズ開始\] \[[1-6]/6\]", output)), 6)
        self.assertEqual(len(re.findall(r"\[フェーズ完了\] \[[1-6]/6\]", output)), 6)
        self.assertRegex(output, r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\] \[フェーズ完了\]")
        seconds = sum(row["seconds"] for row in observation.apis.values())
        self.assertEqual(self.clock.now, seconds + 11 + 15)
        # Initial/preapproval listing happens outside the deployment interval.
        self.assertEqual(observation.deployment_seconds, seconds - 8)
        self.assertTrue(all(row["max_seconds"] == 2 for row in observation.apis.values()))
        self.assertIn("応答確認済み:", output)
        self.assertIn("全件照合済み:", output)
        self.assertIsNone(d.OBSERVATION.get())

    def test_normal_output_and_noop_second_run_have_separate_totals(self):
        """Keep normal output compact and start a fresh measurement for a no-op run."""
        with patch("builtins.input", side_effect=self.answer):
            self.assertEqual(d.main(self.argv), 0)
        first = self.output.getvalue()
        self.assertIn("[計測] run全体", first)
        self.assertNotIn("[API集計]", first)
        self.assertNotIn("[フェーズ開始]", first)
        self.assertIn("配備開始", first)
        with patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            self.assertEqual(d.main(self.argv), 0)
        second = self.observations[-1]
        self.assertEqual(second.outcome, "変更不要")
        self.assertEqual(second.approval_wait, 0)
        self.assertEqual(second.deployment_seconds, 0)
        self.assertEqual(set(second.apis), {"labels.list", "filters.list"})
        self.assertEqual(second.apis["filters.list"]["attempts"], 1)

    def test_decline_and_preflight_exception_report_without_deployment(self):
        """Report decline and preflight failure while retaining zero deployment time."""
        with patch("builtins.input", return_value="n"):
            self.assertEqual(d.main(self.argv), 0)
        self.assertEqual(self.observations[-1].outcome, "承認辞退")
        self.assertEqual(self.observations[-1].deployment_seconds, 0)
        with patch.object(d, "compile_xml", side_effect=ValueError("bad fixture")):
            self.assertEqual(d.main(self.argv + ["-v"]), 3)
        self.assertEqual(self.observations[-1].outcome, "例外停止")
        self.assertIn("[フェーズ停止] [1/6]", self.output.getvalue())
        self.assertIn("Gmail API試行なし", self.output.getvalue())
        self.assertIsNone(d.OBSERVATION.get())

    def test_partial_stop_and_resume_already_applied_report(self):
        # A synthetic bounded deployment keeps the real CLI approval/order path.
        """Report bounded completion and an already-applied resume as distinct outcomes."""
        with patch("builtins.input", side_effect=self.answer), patch.object(d, "perform", return_value=False):
            self.assertEqual(d.main(self.argv), 0)
        self.assertEqual(self.observations[-1].outcome, "途中終了（保留計画を保持）")
        self.assertTrue((self.local / "run_pending.json").exists())
        self.client = FakeClient(d.compile_xml(self.master))
        with patch("builtins.input", side_effect=AssertionError("already applied")):
            self.assertEqual(d.main(self.argv), 0)
        self.assertEqual(self.observations[-1].outcome, "前回配備の反映済みを照合")
        self.assertFalse((self.local / "run_pending.json").exists())

    def test_actual_bounded_batch_keeps_progress_and_reports_partial_stop(self):
        """Retain pending work and readback progress at a real synthetic batch limit."""
        from test_batch import BatchClient
        self.client = BatchClient(d.compile_xml(self.baseline), None)
        rows, labels = d.api_fetch(self.client)
        plan = d.make_plan(None, self.master, [r["filter"] for r in rows], "test@example.invalid", labels)
        plan = d.select_plan_groups(plan, ["other"], current=rows)
        self.client.log = self.local / "logs" / (plan["approval_digest"] + ".json")
        with patch("builtins.input", side_effect=self.answer):
            self.assertEqual(d.main(self.argv + ["--batch-size", "2", "--max-batches", "1"]), 0)
        self.assertEqual(self.observations[-1].outcome, "途中終了（保留計画を保持）")
        self.assertTrue((self.local / "run_pending.json").exists())
        self.assertIn("全件照合済み", self.output.getvalue())
        self.assertEqual(len(self.client.batches), 1)

    def test_interrupt_during_deployment_reports_last_progress_and_resets(self):
        """Preserve the last acknowledged and verified progress when Ctrl-C stops a write."""
        from test_deployer import FakeFilters
        original_create = FakeFilters.create
        calls = []

        def create(resource, **kwargs):
            """Let one create complete and interrupt the next request at execution."""
            request = original_create(resource, **kwargs)
            calls.append(1)
            return request if len(calls) == 1 else Req(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        with patch("builtins.input", side_effect=self.answer), patch.object(FakeFilters, "create", create):
            with self.assertRaises(KeyboardInterrupt):
                d.main(self.argv + ["-v"])
        observation = self.observations[-1]
        self.assertEqual(observation.outcome, "中断")
        self.assertEqual(observation.apis["filters.create"]["success"], 1)
        self.assertEqual(observation.apis["filters.create"]["failure"], 1)
        self.assertIn("応答確認済み: 削除 1/1 / 追加 1/2", self.output.getvalue())
        self.assertIn("全件照合済み: 削除 1 / 追加 0", self.output.getvalue())
        self.assertIn("[フェーズ停止] [6/6]", self.output.getvalue())
        self.assertTrue((self.local / "run_pending.json").exists())
        self.assertIsNone(d.OBSERVATION.get())

    def test_apply_uses_same_measurement_without_approval_wait(self):
        """Measure apply phases and API time without introducing interactive approval."""
        rows, labels = d.api_fetch(self.client)
        plan = d.make_plan(None, self.master, [r["filter"] for r in rows], "test@example.invalid", labels)
        path = self.local / "plan.json"
        d.save_json(path, plan)
        argv = ["apply", "--plan", str(path), "--approve", plan["approval_digest"],
                "--log", str(self.local / "journal.json"), "--expected-email", plan["account"],
                "--allow-removals", "-v"]
        self.assertEqual(d.main(argv), 0)
        observation = self.observations[-1]
        self.assertEqual(observation.approval_wait, 0)
        self.assertEqual(observation.deployment_seconds, sum(r["seconds"] for r in observation.apis.values()))
        self.assertIn("[計測] apply全体", self.output.getvalue())
        self.assertIn("[フェーズ完了] [3/3]", self.output.getvalue())


@unittest.skipUnless(SDK_AVAILABLE, "SDK integration tests run in Docker")
class BatchMeasurementTests(unittest.TestCase):
    """Exercise SDK multipart transport metrics without contacting Gmail."""
    def setUp(self):
        """Build the real SDK client with anonymous credentials and synthetic time."""
        from google.auth.credentials import AnonymousCredentials
        from googleapiclient.discovery import build
        self.http = d.SingleAttemptHttp(AnonymousCredentials())
        self.addCleanup(self.http.close)
        self.client = build("gmail", "v1", http=self.http, cache_discovery=False, static_discovery=True)
        self.clock = Clock()
        for patcher in (patch.object(d.time, "perf_counter", self.clock), patch("sys.stdout", StringIO())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def requests(self):
        """Build two unsent SDK delete requests for fake filter IDs."""
        return [(i, self.client.users().settings().filters().delete(userId="me", id=f"test{i}"))
                for i in range(2)]

    def response(self, statuses, outer=200):
        """Create a transport stub with configurable outer and inner HTTP statuses."""
        def send(request, **kwargs):
            """Return a synthetic multipart response after four controlled seconds."""
            from email.parser import Parser
            import requests
            self.clock.advance(4)
            body = request.body.decode() if isinstance(request.body, bytes) else request.body
            parts = Parser().parsestr("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n" + body)
            payload = []
            for part, status in zip(parts.get_payload(), statuses):
                payload.append("--test\r\nContent-Type: application/http\r\nContent-ID: "
                    + part["Content-ID"] + f"\r\n\r\nHTTP/1.1 {status} Test\r\n"
                    "Content-Type: application/json\r\n\r\n{}\r\n")
            response = requests.Response()
            response.status_code = outer
            response.headers["Content-Type"] = "multipart/mixed; boundary=test"
            response._content = ("".join(payload) + "--test--\r\n").encode()
            return response
        return send

    def test_batch_outer_latency_and_inner_response_counts_are_separate(self):
        """Exclude callback time and count inner errors independently of outer success."""
        with d.observe_command("apply", True) as observation:
            with patch.object(self.http.session, "send", side_effect=self.response([204, 500])) as send:
                d.execute_filter_batch(self.client, self.requests(), lambda *args: self.clock.advance(3),
                                       api_kind="filters.batch.delete")
        send.assert_called_once()
        self.assertEqual(observation.apis["filters.batch.delete"], {
            "attempts": 1, "success": 1, "failure": 0, "seconds": 4, "max_seconds": 4})
        self.assertEqual(observation.batch_items["filters.batch.delete"], {
            "submitted": 2, "success": 1, "failure": 1, "unknown": 0})
        self.assertEqual(self.clock.now, 10)  # Callback/journal cost is not outer HTTP latency.
        self.assertNotIn("filters.delete", observation.apis)

    def test_outer_failure_and_callback_failure_preserve_unknown_responses(self):
        """Keep undelivered responses unknown without misclassifying callback failures."""
        for outer, disk_error in ((500, False), (200, True)):
            with self.subTest(outer=outer), d.observe_command("apply") as observation:
                with patch.object(self.http.session, "send", side_effect=self.response([204, 204], outer)) as send:
                    def callback(*args):
                        """Optionally fail local persistence after receiving a successful HTTP response."""
                        if disk_error:
                            raise OSError("disk")
                    with self.assertRaises(Exception):
                        d.execute_filter_batch(self.client, self.requests(), callback, api_kind="filters.batch.delete")
                send.assert_called_once()
                row = observation.apis["filters.batch.delete"]
                self.assertEqual(row["success"], int(outer == 200))
                self.assertEqual(row["failure"], int(outer != 200))
                items = observation.batch_items["filters.batch.delete"]
                self.assertEqual(items["success"], int(disk_error))
                self.assertEqual(items["unknown"], 1 if disk_error else 2)

    def test_timeout_and_inner_401_do_not_add_replay_attempts(self):
        """Retain one send and coherent unknown counts after timeout or refused 401 replay."""
        import requests
        for effect in (requests.exceptions.Timeout(), self.response([401, 401])):
            with self.subTest(effect=type(effect).__name__), d.observe_command("apply") as observation:
                with patch.object(self.http.session, "send", side_effect=effect) as send:
                    with self.assertRaises(Exception):
                        d.execute_filter_batch(self.client, self.requests(), lambda *args: None,
                                               api_kind="filters.batch.delete")
                send.assert_called_once()
                self.assertEqual(observation.apis["filters.batch.delete"]["attempts"], 1)
                items = observation.batch_items["filters.batch.delete"]
                self.assertEqual(items["submitted"], items["success"] + items["failure"] + items["unknown"])


if __name__ == "__main__":
    unittest.main()
