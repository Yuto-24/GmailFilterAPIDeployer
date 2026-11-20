"""Read batching preserves metadata validation, complete responses, and one-send writes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures
from test_recovery import ApiError, SDK_AVAILABLE


class Batch:
    def __init__(self, client, callback):
        self.client, self.callback, self.requests = client, callback, []

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        self.client.read_batches.append([mid for mid, _ in self.requests])
        if hasattr(self.client, 'batch_started_times'):
            self.client.batch_started_times.append(m.time.monotonic())
        for mid, request in reversed(self.requests):
            if self.client.read_fault == 'missing':
                continue
            if self.client.read_fault == 'forbidden':
                self.callback(mid, None, ApiError(403, 'forbidden'))
            elif self.client.read_fault == 'transient':
                self.client.read_fault = None
                self.callback(mid, None, ApiError(500, 'backendError'))
            elif self.client.read_fault == 'precondition':
                self.client.read_fault = None
                self.callback(mid, None, ApiError(400, 'failedPrecondition'))
            elif self.client.read_fault == 'persistent_precondition':
                self.callback(mid, None, ApiError(400, 'failedPrecondition'))
            elif self.client.read_fault == 'mixed_precondition_forbidden':
                reason = 'failedPrecondition' if mid == 'b' else 'forbidden'
                self.callback(mid, None, ApiError(400 if mid == 'b' else 403, reason))
            else:
                self.callback(mid, request.execute(), None)
                if self.client.read_fault == 'duplicate':
                    self.callback(mid, {}, None)


class MessageReadBatchTests(unittest.TestCase):
    execute = fixtures.MessageTests.execute

    def setUp(self):
        fixtures.MessageTests.setUp(self)
        self.client.read_batches, self.client.read_fault = [], None
        self.client.new_batch_http_request = lambda callback: Batch(self.client, callback)
        self.api = m.Gmail(self.client, read_batch_size=3)
        sleeper = patch.object(m.time, 'sleep')
        self.sleeper = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_out_of_order_responses_are_validated_and_returned_in_requested_order(self):
        rows = list(self.api.snapshots(['a','b','c','d']))
        self.assertEqual([mid for mid, _ in rows], ['a','b','c','d'])
        self.assertEqual(self.client.read_batches, [['a','b','c'], ['d']])
        self.assertEqual([self.api.metadata(mid, row, self.api.labels())['id'] for mid, row in rows],
                         ['a','b','c','d'])
        self.assertTrue(self.sleeper.called)
        self.assertEqual(self.client.writes, [])

    def test_single_and_batched_reads_reserve_current_per_user_quota_headroom(self):
        for size, expected_delay in [(1, 0.25), (3, 0.75)]:
            with self.subTest(size=size):
                api = m.Gmail(self.client, read_batch_size=size)
                self.sleeper.reset_mock()
                with patch.object(m.time, 'monotonic', return_value=0):
                    list(api.snapshots(['a','b','c'][:size]))
                self.sleeper.assert_called_once_with(expected_delay)

    def test_missing_duplicate_and_forbidden_responses_yield_no_partial_chunk(self):
        for fault in ('missing','duplicate','forbidden'):
            with self.subTest(fault=fault):
                self.client.read_fault = fault
                yielded = []
                with self.assertRaises(Exception):
                    for row in self.api.snapshots(['a','b']):
                        yielded.append(row)
                self.assertEqual(yielded, [])
        self.assertEqual(self.client.writes, [])

    def test_transient_inner_read_failure_retries_only_get_requests(self):
        self.client.read_fault = 'transient'
        self.assertEqual(len(list(self.api.snapshots(['a','b']))), 2)
        self.assertEqual(self.client.read_batches, [['a','b'], ['a','b']])
        self.assertEqual(self.client.writes, [])

    def test_transient_precondition_get_recovers_without_writes(self):
        attempts = []
        def fail_once(kind):
            if kind == 'messages.get':
                attempts.append(kind)
                if len(attempts) == 1:
                    raise ApiError(400, 'failedPrecondition')
        self.client.on_request = fail_once
        api = m.Gmail(self.client)
        self.assertEqual([mid for mid, _ in api.snapshots(['a'])], ['a'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.client.writes, [])

    def test_persistent_precondition_get_stops_after_three_attempts(self):
        def fail(kind):
            if kind == 'messages.get':
                raise ApiError(400, 'failedPrecondition')
        self.client.on_request = fail
        with self.assertRaises(d.DeploymentError):
            list(m.Gmail(self.client).snapshots(['a']))
        self.assertEqual(self.client.calls.count('messages.get'), 3)
        self.assertEqual(self.client.writes, [])

    def test_other_errors_and_non_message_reads_are_not_reclassified(self):
        errors = [ApiError(400, 'invalidArgument'), ApiError(401, 'authError'), ApiError(403, 'forbidden')]
        malformed = ApiError(400, 'failedPrecondition')
        malformed.content = b'not JSON'
        errors.append(malformed)
        for error in errors:
            with self.subTest(error=error.content):
                request = fixtures.Req(lambda: (_ for _ in ()).throw(error))
                with patch.object(request, 'execute', wraps=request.execute) as execute:
                    with self.assertRaises(type(error)):
                        m.Gmail(self.client).read('messages.get', request)
                    self.assertEqual(execute.call_count, 1)
        error = ApiError(400, 'failedPrecondition')
        self.assertFalse(d.transient_api_error(error))
        request = fixtures.Req(lambda: (_ for _ in ()).throw(error))
        with patch.object(request, 'execute', wraps=request.execute) as execute:
            with self.assertRaises(ApiError):
                m.Gmail(self.client).read('labels.list', request)
            self.assertEqual(execute.call_count, 1)

    def test_inner_precondition_recovers_but_permanent_sibling_stops(self):
        self.client.read_fault = 'precondition'
        self.assertEqual([mid for mid, _ in self.api.snapshots(['a','b'])], ['a','b'])
        self.assertEqual(self.client.read_batches, [['a','b'], ['a','b']])
        self.client.read_batches.clear()
        self.client.read_fault = 'mixed_precondition_forbidden'
        with self.assertRaises(ApiError) as caught:
            list(self.api.snapshots(['a','b']))
        self.assertEqual(caught.exception.resp.status, 403)
        self.assertEqual(self.client.read_batches, [['a','b']])
        self.assertEqual(self.client.writes, [])

    def test_precondition_write_failure_is_never_retried(self):
        self.client.write_failure = ApiError(400, 'failedPrecondition')
        with self.assertRaises(ApiError):
            self.api.modify(['a'], ['L1'], [])
        self.assertEqual(len(self.client.writes), 1)

    def test_every_failed_fifty_message_attempt_reserves_its_quota_budget(self):
        self.client.read_fault = 'persistent_precondition'
        self.client.batch_started_times = []
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(m.time, 'sleep', side_effect=sleep), \
             patch.object(d.random, 'uniform', return_value=0):
            with self.assertRaises(d.DeploymentError):
                list(m.Gmail(self.client, read_batch_size=50).snapshots([str(i) for i in range(50)]))
        starts = self.client.batch_started_times
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(b - a >= 12.5 for a, b in zip(starts, starts[1:])))
        self.assertGreaterEqual(clock[0] - starts[-1], 12.5)
        self.assertEqual(self.client.writes, [])

    def test_plan_apply_and_restore_with_batched_reads_and_unknown_metadata(self):
        self.client.mail['a']['labelIds'].append('Label_missing')
        original = deepcopy(self.client.mail)
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT, skip_unknown_labels=True)
        plan = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(plan.close)
        self.assertFalse(self.execute(plan))
        self.assertEqual(self.client.mail['a'], original['a'])
        self.assertTrue(self.execute(plan, mode='restore'))
        self.assertEqual({mid: set(row["labelIds"]) for mid,row in self.client.mail.items()},
                         {mid: set(row["labelIds"]) for mid,row in original.items()})
        self.assertTrue(all('a' not in body['ids'] for body in self.client.writes))

    def test_failed_postwrite_batch_read_preserves_owned_intent_without_resend(self):
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        plan = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(plan.close)
        self.client.on_write = lambda body: setattr(self.client, 'read_fault', 'forbidden')
        with self.assertRaises(Exception):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)
        self.assertTrue((self.local/m.PENDING).exists())
        first = deepcopy(self.client.writes[0])
        self.client.read_fault, self.client.on_write = None, None
        self.assertTrue(self.execute(plan))
        self.assertEqual(sum(body == first for body in self.client.writes), 1)

    def test_invalid_read_batch_size_fails_before_client_use(self):
        for size in (0,51):
            with self.assertRaises(d.DeploymentError):
                m.Gmail(object(), read_batch_size=size)

    @unittest.skipUnless(SDK_AVAILABLE, 'SDK integration tests run in Docker')
    def test_real_sdk_batch_contains_only_minimal_gets(self):
        from email.parser import Parser
        from urllib.parse import urlparse, unquote
        import httplib2
        from googleapiclient.discovery import build
        calls = []
        class Http:
            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                calls.append((uri, method))
                self_body = body.decode() if isinstance(body, bytes) else body
                mime = Parser().parsestr('Content-Type: '+headers['content-type']+'\r\n\r\n'+self_body)
                parts = []
                for part in reversed(mime.get_payload()):
                    line = part.get_payload().splitlines()[0]
                    assert line.startswith('GET ') and 'format=minimal' in line
                    assert 'fields=' in line and 'batchModify' not in line
                    mid = unquote(urlparse(line.split()[1]).path.rsplit('/',1)[1])
                    payload = json.dumps({'id':mid,'threadId':'thread','labelIds':['INBOX']})
                    parts.append('--test\r\nContent-Type: application/http\r\nContent-ID: '+part['Content-ID']+
                                 '\r\n\r\nHTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n'+payload+'\r\n')
                return httplib2.Response({'status':'200','content-type':'multipart/mixed; boundary=test'}), \
                       (''.join(parts)+'--test--\r\n').encode()
        api = m.Gmail(build('gmail','v1',http=Http(),static_discovery=True), read_batch_size=25)
        self.assertEqual([mid for mid,_ in api.snapshots(['a','b'])], ['a','b'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'POST')
        self.assertTrue(calls[0][0].endswith('/batch'))
