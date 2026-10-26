"""Synthetic batch faults, journal restart and real SDK multipart transport."""
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from test_deployer import FakeClient, FakeFilters, FakeLabels, Req, write_xml
from test_recovery import ApiError, SDK_AVAILABLE


class FakeBatch:
    def __init__(self, client, callback):
        self.client, self.callback, self.requests = client, callback, []

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def apply(self, index, *, callback=True):
        key, request = self.requests[index]
        result = request.execute()
        if callback:
            self.callback(key, result, None)
        return result

    def execute(self, http=None):
        self.client.batches.append([key for key, _ in self.requests])
        # Disk intent must already exist when network execution starts.
        state = json.loads(self.client.log.read_text())
        assert state['batch_checkpoint']['pending']['steps'] == [int(k) for k, _ in self.requests]
        if self.client.executor:
            return self.client.executor(self)
        for i in reversed(range(len(self.requests))):
            self.apply(i)


class BatchClient(FakeClient):
    def __init__(self, templates, log):
        super().__init__(templates)
        self.log, self.batches, self.executor = log, [], None

    def new_batch_http_request(self, callback):
        return FakeBatch(self, callback)


class BatchTests(unittest.TestCase):
    def test_error_details_preserve_backend_reason_without_request_headers(self):
        error = ApiError(503, 'backendError')
        error.content = json.dumps({'error': {'code': 503, 'status': 'UNAVAILABLE',
            'message': 'Backend Error', 'errors': [{'reason': 'backendError', 'domain': 'global'}]},
            'request': {'Authorization': 'secret'}}).encode()
        details = d.api_error_details(error)
        self.assertEqual(details['google_status'], 'UNAVAILABLE')
        self.assertEqual(details['errors'], [{'reason': 'backendError', 'domain': 'global'}])
        self.assertNotIn('secret', json.dumps(details))
        self.assertEqual(d.api_error_details(TimeoutError()),
                         {'code': 'TimeoutError', 'exception_type': 'TimeoutError'})

    def test_error_details_tolerate_malformed_response(self):
        for content in (b'invalid', b'[]', b'{"error":null}', b'{"error":{"errors":"invalid"}}'):
            error = ApiError()
            error.content = content
            self.assertEqual(d.api_error_details(error)['code'], 'HTTP 500')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.log = self.local / 'journal.json'
        self.base_path = self.local / 'base.xml'
        self.master = self.local / 'mailFilters_v1.0.0_master.xml'
        self.common = [{'from': 'keep@example.invalid', 'label': 'Type/Test'}]
        write_xml(self.base_path, self.common + [
            {'from': f'old{i}@example.invalid', 'label': 'Type/Test'} for i in range(7)])
        write_xml(self.master, self.common + [
            {'from': f'new{i}@example.invalid', 'label': 'Type/Test'} for i in range(5)])
        self.base = d.compile_xml(self.base_path)
        self.client = BatchClient(self.base, self.log)
        _, labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', labels)
        self.output = StringIO()
        self.stdout = patch('sys.stdout', self.output)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)
        sleep = patch.object(d.time, 'sleep')
        self.sleeper = sleep.start()
        self.addCleanup(sleep.stop)

    def deploy(self, size=3):
        d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                  self.log, batch_size=size, verbose=True)

    def state(self):
        return json.loads(self.log.read_text())

    def complete(self):
        self.assertTrue(self.state()['verified'])
        rows, _ = d.api_fetch(self.client)
        self.assertEqual(d.counter(row['filter'] for row in rows), d.counter(d.compile_xml(self.master)))

    def test_reverse_order_batches_read_only_at_phase_boundaries(self):
        with patch.object(d, 'api_fetch', wraps=d.api_fetch) as fetch:
            self.deploy()
        self.assertEqual([len(x) for x in self.client.batches], [3, 3, 1, 3, 2])
        self.assertEqual(fetch.call_count, 3)  # Start, after removals, after additions.
        self.assertIn('Gmail filters.batch remove', self.output.getvalue())
        self.complete()

    def test_default_1_sends_operations_serially(self):
        d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'], self.log)
        self.assertEqual(self.client.batches, [])
        self.complete()

    def test_bounded_run_retains_plan_then_resumes_without_duplicate_writes(self):
        self.plan = d.select_plan_groups(self.plan, ['other'])
        self.log = self.local / 'logs' / (self.plan['approval_digest'] + '.json')
        self.client.log = self.log
        pending = self.local / 'run_pending.json'
        d.save_json(pending, self.plan)
        original = pending.read_bytes()
        argv = ['run', '--local-dir', str(self.local), '--expected-email', self.plan['account'],
                '--batch-size', '2', '--max-batches', '1']
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'make_plan', side_effect=AssertionError('must not replan')), \
             patch.object(d, 'confirm_interactive', return_value=True), redirect_stderr(self.output):
            self.assertEqual(d.main(argv), 0, self.output.getvalue())
            self.assertEqual(pending.read_bytes(), original)
            self.assertEqual(self.client.batches, [['0', '1']])
            self.assertFalse(self.state()['verified'])
            single = argv.copy()
            single[single.index('--batch-size') + 1] = '1'
            self.assertEqual(d.main(single), 0, self.output.getvalue())
            self.assertEqual(pending.read_bytes(), original)
            self.assertEqual(len(self.client.batches), 1)
            self.assertEqual(self.state()['events'][-1]['transport'], 'single')
            self.assertEqual(d.main(argv[:-2]), 0, self.output.getvalue())
        self.assertFalse(pending.exists())
        self.assertEqual(self.client.batches[1], ['3', '4'])
        self.complete()

    def test_limit_does_not_switch_a_fresh_serial_plan_to_batch_transport(self):
        with patch.object(d, 'fetch_for_deploy', side_effect=AssertionError('must not access API')):
            with self.assertRaisesRegex(d.DeploymentError, '新規の1件逐次処理'):
                d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                          self.log, batch_size=1, max_batches=1)
        self.plan = d.select_plan_groups(self.plan, ['other'])
        d.save_json(self.local / 'run_pending.json', self.plan)
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'confirm_interactive', side_effect=AssertionError('must fail before approval')) as approval, \
             redirect_stderr(self.output):
            self.assertEqual(d.main(['run', '--local-dir', str(self.local), '--expected-email', self.plan['account'],
                                     '--batch-size', '1', '--max-batches', '1']), 2)
            approval.assert_not_called()
        self.assertEqual(self.client.batches, [])

    def test_limit_at_phase_boundary_does_not_create_labels(self):
        write_xml(self.master, self.common + [{'from': 'new@example.invalid', 'label': 'Service/New'}])
        _, labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', labels)
        result = d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                           self.log, batch_size=7, max_batches=1)
        self.assertIs(result, False)
        self.assertEqual(len(self.client.batches), 1)
        self.assertNotIn('Service/New', self.client.id_to_label.values())

    def test_invalid_limit_rejected_before_api_access(self):
        with patch.object(d, 'get_client', side_effect=AssertionError('must not access API')), \
             redirect_stderr(self.output):
            self.assertEqual(d.main(['run', '--max-batches', '0']), 2)

    def test_google_error_reason_is_saved_with_partial_progress(self):
        def partial(batch):
            batch.apply(0)
            error = ApiError(503)
            error.content = b'{"error":{"status":"UNAVAILABLE","errors":[{"reason":"backendError"}]}}'
            batch.callback(batch.requests[1][0], None, error)
        self.client.executor = partial
        with self.assertRaises(d.DeploymentError):
            self.deploy(size=2)
        state = self.state()
        details = state['batch_checkpoint']['pending']['error_details']['1']
        self.assertEqual(details['google_status'], 'UNAVAILABLE')
        self.assertEqual(details['errors'][0]['reason'], 'backendError')
        self.assertEqual(state['events'][-1]['error_details']['1'], details)
        self.assertEqual(state['completed_remove'], 1)
        self.assertEqual(len(self.client.batches), 1)

    def test_partial_delete_stops_then_resumes_only_missing_steps(self):
        def partial(batch):
            batch.apply(2)
            batch.callback(batch.requests[0][0], None, ApiError(429))
            batch.callback(batch.requests[1][0], None, ApiError(403, 'domainPolicy'))
        self.client.executor = partial
        with self.assertRaisesRegex(d.DeploymentError, '一部失敗'):
            self.deploy()
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(self.state()['completed_remove'], 1)
        rows, labels = d.api_fetch(self.client)
        self.assertEqual(d.batch_progress(self.base, self.plan, rows, self.log, labels), (set(), {2}))
        d.validate_before_final_approval(self.plan, self.plan['account'], self.log, 3, rows)
        self.client.executor = None
        self.deploy()
        self.assertEqual(self.client.batches[1], ['0', '1', '3'])
        self.complete()

    def test_lost_response_after_nonprefix_creates_does_not_duplicate_on_resume(self):
        def lost(batch):
            kind = self.state()['batch_checkpoint']['pending']['kind']
            if kind == 'remove':
                for i in range(len(batch.requests)):
                    batch.apply(i)
            else:
                batch.apply(2, callback=False)
                raise ConnectionResetError()
        self.client.executor = lost
        with self.assertRaisesRegex(d.DeploymentError, '一部失敗'):
            self.deploy()
        self.assertEqual(self.state()['completed_add'], 1)
        self.assertEqual(self.state()['batch_checkpoint']['pending']['successes'].keys(), {'2'})
        self.client.executor = None
        self.deploy()
        self.complete()
        self.assertEqual(self.client.n, len(self.base) + 5)

    def test_process_death_before_callbacks_resumes_from_persisted_intent(self):
        def crash(batch):
            batch.apply(1, callback=False)
            raise KeyboardInterrupt()
        self.client.executor = crash
        with self.assertRaises(KeyboardInterrupt):
            self.deploy()
        self.assertEqual(self.state()['batch_checkpoint']['pending']['successes'], {})
        self.client.executor = None
        self.deploy(size=1)  # A batch journal stays resumable even with changed size.
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(self.state()['events'][-1]['transport'], 'single')
        self.complete()

    def test_lost_response_after_all_success_keeps_intent_until_resume(self):
        def lost(batch):
            for i in range(len(batch.requests)):
                batch.apply(i, callback=False)
            raise TimeoutError()
        self.client.executor = lost
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(self.state()['completed_remove'], 3)
        self.client.executor = None
        self.deploy()
        self.assertEqual(self.client.batches[1], ['3', '4', '5'])
        self.complete()

    def test_wrong_existing_id_or_content_or_labels_stops_after_first_batch(self):
        for drift in ('id', 'content', 'labels', 'unexpected'):
            with self.subTest(drift=drift):
                self.setUp()
                def changed(batch):
                    batch.apply(0)
                    keep = next(fid for fid, row in self.client.filters.items()
                                if row.get('criteria', {}).get('from') == 'keep@example.invalid')
                    if drift == 'id':
                        row = self.client.filters.pop(keep)
                        row['id'] = 'external_id'
                        self.client.filters['external_id'] = row
                    elif drift == 'content':
                        self.client.filters[keep]['criteria']['from'] = 'external@example.invalid'
                    elif drift == 'labels':
                        self.client.id_to_label['outside_label'] = 'Type/Outside'
                    else:
                        self.client.filters['external'] = {'id': 'external', 'criteria': {'from': 'outside@example.invalid'},
                                                           'action': {'addLabelIds': ['STARRED']}}
                    raise ConnectionResetError()
                self.client.executor = changed
                with self.assertRaises(d.DeploymentError):
                    self.deploy()
                self.assertEqual(len(self.client.batches), 1)
                self.assertTrue(list((self.local / 'diagnostics').glob('batch_*.json')))

    def test_acknowledged_success_not_visible_never_resends(self):
        def false_ack(batch):
            for key, _ in batch.requests:
                batch.callback(key, {}, None)
        self.client.executor = false_ack
        with self.assertRaisesRegex(d.DeploymentError, '読戻し'):
            self.deploy()
        # Acknowledgements can lead readback until the phase/50-operation boundary.
        self.assertEqual(len(self.client.batches), 3)
        self.assertEqual(len(self.client.filters), len(self.base))

    def test_transient_readback_recovers_without_more_writes(self):
        original = FakeFilters.list
        glitched = []
        def listing(fake, **kwargs):
            req = original(fake, **kwargs)
            def execute():
                result = req.execute()
                if self.client.batches and not glitched:
                    glitched.append(True)
                    result['filter'] = result['filter'][:-1]
                return result
            return Req(execute)
        with patch.object(FakeFilters, 'list', new=listing):
            self.deploy()
        self.sleeper.assert_called_once_with(2)
        self.complete()

    def test_legacy_prefix_is_upgraded_without_resending_completed_removals(self):
        for rule in self.plan['remove'][:2]:
            rows, _ = d.api_fetch(self.client)
            fid = next(r['id'] for r in rows if r['filter'] == rule)
            self.client.users().settings().filters().delete(userId='me', id=fid).execute()
        d.save_json(self.log, {'plan_digest': self.plan['approval_digest'], 'events': [],
                              'completed_add': 0, 'completed_remove': 2})
        self.deploy()
        self.assertEqual(self.client.batches[0], ['2', '3', '4'])
        self.complete()

    def test_add_first_plan_preserves_order(self):
        self.plan['operation_order'] = 'add_first'
        self.plan['approval_digest'] = d.json_hash({k: v for k, v in self.plan.items()
                                                  if k not in {'created_utc', 'approval_digest'}})
        self.deploy()
        self.assertEqual([e['operation'] for e in self.state()['events']],
                         ['add', 'add', 'remove', 'remove', 'remove'])
        self.complete()

    def test_journal_tamper_or_outside_change_blocks_resume_before_send(self):
        def partial(batch):
            batch.apply(2)
            raise TimeoutError()
        self.client.executor = partial
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        state = self.state()
        state['batch_checkpoint']['pending']['steps'] = [0, 1, 6]
        d.save_json(self.log, state)
        self.client.executor = None
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.assertEqual(len(self.client.batches), 1)

    def test_label_creation_crash_is_reconciled_on_resume(self):
        write_xml(self.master, self.common + [{'from': 'new@example.invalid', 'label': 'Service/New'}])
        _, labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, self.plan['account'], labels)
        original = FakeLabels.create
        def creation(fake, **kwargs):
            req = original(fake, **kwargs)
            def execute():
                req.execute()
                raise KeyboardInterrupt()
            return Req(execute)
        with patch.object(FakeLabels, 'create', new=creation), self.assertRaises(KeyboardInterrupt):
            self.deploy()
        self.assertEqual(self.state()['batch_checkpoint']['pending_label'], 'Service/New')
        self.deploy()
        self.complete()
        self.assertEqual(list(self.client.id_to_label.values()).count('Service/New'), 1)

    def test_run_resumes_nonprefix_batch_with_original_approval_and_no_replan(self):
        self.plan = d.select_plan_groups(self.plan, ['other'])
        self.log = self.local / 'logs' / (self.plan['approval_digest'] + '.json')
        self.client.log = self.log
        pending = self.local / 'run_pending.json'
        d.save_json(pending, self.plan)
        original = pending.read_bytes()
        def partial(batch):
            batch.apply(2, callback=False)
            raise TimeoutError()
        self.client.executor = partial
        with self.assertRaises(d.DeploymentError):
            self.deploy()
        self.client.executor = None
        argv = ['run', '--local-dir', str(self.local), '--expected-email', self.plan['account']]
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'make_plan', side_effect=AssertionError('must not replan')), \
             patch.object(d, 'confirm_interactive', return_value=False) as approval, \
             redirect_stderr(self.output):
            self.assertEqual(d.main(argv), 0)
            self.assertEqual(pending.read_bytes(), original)
            self.assertEqual(len(self.client.batches), 1)
            approval.return_value = True
            self.assertEqual(d.main(argv), 0, self.output.getvalue())
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.client.batches), 1)
        resumed = [event for event in self.state()['events'] if event.get('transport') == 'single'
                   and event['operation'] == 'remove']
        self.assertNotIn(2, [step for event in resumed for step in event['steps']])
        self.complete()

    def test_direct_delete_failure_keeps_intent_and_does_not_retry(self):
        d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                  self.log, batch_size=2, max_batches=1)
        calls = []
        def fail(fake, **kwargs):
            def execute():
                pending = self.state()['batch_checkpoint']['pending']
                self.assertEqual(pending['transport'], 'single')
                self.assertIn(kwargs['id'], pending['ids'].values())
                calls.append(kwargs['id'])
                error = ApiError(400)
                error.content = b'{"error":{"errors":[{"reason":"failedPrecondition"}]}}'
                raise error
            return Req(execute)
        with patch.object(FakeFilters, 'delete', new=fail):
            with self.assertRaises(d.DeploymentError):
                self.deploy(size=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state()['completed_remove'], 2)
        self.assertEqual(self.state()['batch_checkpoint']['pending']['error_details']['2']['errors'][0]['reason'],
                         'failedPrecondition')
        self.deploy(size=1)
        self.complete()
        self.assertEqual(len(self.client.batches), 1)

    def test_direct_create_lost_response_is_read_back_and_not_duplicated_on_resume(self):
        d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                  self.log, batch_size=7, max_batches=1)
        original = FakeFilters.create
        calls = []
        def lost(fake, **kwargs):
            request = original(fake, **kwargs)
            def execute():
                self.assertEqual(self.state()['batch_checkpoint']['pending']['transport'], 'single')
                calls.append(True)
                request.execute()
                raise TimeoutError()
            return Req(execute)
        with patch.object(FakeFilters, 'create', new=lost):
            with self.assertRaises(d.DeploymentError):
                self.deploy(size=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state()['completed_add'], 1)
        self.deploy(size=1)
        self.complete()
        self.assertEqual(self.client.n, len(self.base) + 5)

    def test_failed_recovery_read_does_not_retry_or_send_next_batch(self):
        def lost(batch):
            batch.apply(1, callback=False)
            raise TimeoutError()
        self.client.executor = lost
        original = FakeLabels.list
        reads = []
        def listing(fake, **kwargs):
            if self.client.batches:
                def fail():
                    reads.append(True)
                    raise ApiError()
                return Req(fail)
            return original(fake, **kwargs)
        with patch.object(FakeLabels, 'list', new=listing), self.assertRaises(ApiError):
            self.deploy()
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(self.client.batches), 1)
        self.sleeper.assert_not_called()
        self.client.executor = None
        self.deploy()
        self.complete()

    def test_wrong_create_id_and_duplicate_creation_stop(self):
        for mode in ('wrong_id', 'duplicate'):
            with self.subTest(mode=mode):
                self.setUp()
                def malformed(batch):
                    kind = self.state()['batch_checkpoint']['pending']['kind']
                    if kind == 'remove':
                        for i in range(len(batch.requests)):
                            batch.apply(i)
                        return
                    if mode == 'wrong_id':
                        for i, (key, _) in enumerate(batch.requests):
                            batch.apply(i, callback=False)
                            batch.callback(key, {'id': 'unrelated_id'}, None)
                    else:
                        batch.apply(0)
                        batch.apply(0, callback=False)
                        raise TimeoutError()
                self.client.executor = malformed
                with self.assertRaises(d.DeploymentError):
                    self.deploy()
                self.assertEqual(len(self.client.batches), 4)
                self.assertFalse(self.state()['verified'])

    def test_idless_skipped_rule_survives_batch_deployment(self):
        extra = {'from': 'skip@example.invalid', 'label': 'Type/Test'}
        write_xml(self.base_path, self.common + [extra] + [
            {'from': f'old{i}@example.invalid', 'label': 'Type/Test'} for i in range(7)])
        self.base = d.compile_xml(self.base_path)
        self.client = BatchClient(self.base, self.log)
        row = next(r for r in self.client.filters.values() if r['criteria'].get('from') == extra['from'])
        del row['id']
        rows, labels = d.api_fetch(self.client)
        self.plan = d.select_plan_groups(
            d.make_plan(None, self.master, self.base, self.plan['account'], labels), ['other'], current=rows)
        self.deploy()
        self.assertEqual(len(self.plan['skipped_deletions']), 1)
        self.assertTrue(self.state()['verified'])
        actual, _ = d.api_fetch(self.client)
        self.assertEqual(sum(not r.get('id') for r in actual), 1)

    def test_noop_completed_journal_does_not_send_more_batches(self):
        self.deploy()
        count = len(self.client.batches)
        self.deploy()
        self.assertEqual(len(self.client.batches), count)
        self.complete()

    def test_transport_wrapper_rejects_second_http_send(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        http = Mock()
        class ReplayBatch:
            def add(self, request, request_id):
                pass
            def execute(self, http):
                http.request('https://gmail.googleapis.com/batch', 'POST')
                http.request('https://gmail.googleapis.com/batch', 'POST')
        client = SimpleNamespace(_http=http, new_batch_http_request=lambda callback: ReplayBatch())
        with self.assertRaisesRegex(d.DeploymentError, '再送を拒否'):
            d.execute_filter_batch(client, [(0, object())], lambda *args: None)
        http.request.assert_called_once()


if SDK_AVAILABLE:
    from google.auth.credentials import AnonymousCredentials
    from googleapiclient.discovery import build
    import requests


@unittest.skipUnless(SDK_AVAILABLE, 'SDK integration tests run in Docker')
class SdkBatchTests(unittest.TestCase):
    def setUp(self):
        self.http = d.SingleAttemptHttp(AnonymousCredentials())
        self.addCleanup(self.http.close)
        self.client = build('gmail', 'v1', http=self.http, cache_discovery=False, static_discovery=True)

    def test_direct_resume_uses_normal_endpoint_and_never_replays(self):
        for status in (204, 400, 401, 500, 'timeout'):
            with self.subTest(status=status):
                callbacks = []
                def send(request, **kwargs):
                    self.assertEqual(request.method, 'DELETE')
                    self.assertEqual(request.url, 'https://gmail.googleapis.com/gmail/v1/users/me/settings/filters/test')
                    if status == 'timeout':
                        raise requests.exceptions.Timeout()
                    response = requests.Response()
                    response.status_code = status
                    response.headers['Content-Type'] = 'application/json'
                    response._content = b'' if status == 204 else b'{"error":{"message":"test"}}'
                    return response
                request = self.client.users().settings().filters().delete(userId='me', id='test')
                with patch.object(self.http.session, 'send', side_effect=send) as transport:
                    d.execute_filter_single(5, request, lambda *args: callbacks.append(args))
                transport.assert_called_once()
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0][0], '5')
                self.assertEqual(callbacks[0][2] is None, status == 204)

    def test_real_multipart_and_inner_errors_send_http_once(self):
        for status in (204, 401, 429, 500):
            with self.subTest(status=status):
                calls, callbacks = [], []
                def send(request, **kwargs):
                    from email.parser import Parser
                    calls.append(request)
                    self.assertEqual(kwargs['timeout'], (60, 180))
                    self.assertEqual(request.url, 'https://gmail.googleapis.com/batch')
                    body = request.body.decode() if isinstance(request.body, bytes) else request.body
                    parts = Parser().parsestr('Content-Type: ' + request.headers['Content-Type'] + '\r\n\r\n' + body)
                    response_parts = []
                    for part in reversed(parts.get_payload()):
                        self.assertIn('DELETE /gmail/v1/users/me/settings/filters/', part.get_payload())
                        response_parts.append('--test\r\nContent-Type: application/http\r\nContent-ID: '
                                              + part['Content-ID'] + f'\r\n\r\nHTTP/1.1 {status} Test\r\n'
                                              'Content-Type: application/json\r\n\r\n{}\r\n')
                    response = requests.Response()
                    response.status_code = 200
                    response.headers['Content-Type'] = 'multipart/mixed; boundary=test'
                    response._content = (''.join(response_parts) + '--test--\r\n').encode()
                    return response
                reqs = [(i, self.client.users().settings().filters().delete(userId='me', id=f'id{i}'))
                        for i in range(3)]
                with patch.object(self.http.session, 'send', side_effect=send):
                    if status == 401:
                        with self.assertRaises(Exception):
                            d.execute_filter_batch(self.client, reqs, lambda *args: callbacks.append(args))
                    else:
                        d.execute_filter_batch(self.client, reqs, lambda *args: callbacks.append(args))
                        self.assertEqual([c[0] for c in callbacks], ['0', '1', '2'])
                        self.assertEqual([c[2] is None for c in callbacks], [status == 204] * 3)
                self.assertEqual(len(calls), 1)

    def test_outer_timeout_and_500_are_not_replayed(self):
        response = requests.Response()
        response.status_code = 500
        response._content = b'{}'
        response.headers['Content-Type'] = 'application/json'
        for effect in (requests.exceptions.Timeout(), response):
            with self.subTest(effect=type(effect).__name__):
                kwargs = {'side_effect': effect} if isinstance(effect, Exception) else {'return_value': effect}
                with patch.object(self.http.session, 'send', **kwargs) as send:
                    with self.assertRaises(Exception):
                        d.execute_filter_batch(self.client, [(0, self.client.users().settings().filters().delete(
                            userId='me', id='test'))], lambda *args: None)
                    send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
