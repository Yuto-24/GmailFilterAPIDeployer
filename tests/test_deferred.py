"""Fault-injected continuation, partial lists, durable unknowns and exact resume."""
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_filter_deferred as deferred
from test_deployer import FakeClient, FakeFilters, FakeLabels, Req, write_xml
from test_recovery import ApiError


class DeferredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.master = self.root / 'mailFilters_v1.0.0_master.xml'
        base = self.root / 'base.xml'
        write_xml(base, [{'from': 'keep@example.invalid', 'label': 'Type/Test'}] + [
            {'from': f'old{i}@example.invalid', 'label': 'Type/Test'} for i in range(3)])
        write_xml(self.master, [{'from': 'keep@example.invalid', 'label': 'Type/Test'}] + [
            {'from': f'new{i}@example.invalid', 'label': 'Type/Test'} for i in range(3)])
        self.base = d.compile_xml(base)
        self.client = FakeClient(self.base)
        self.rows, self.labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', self.labels)
        self.plan = d.select_plan_groups(self.plan, ['other'], current=self.rows)
        self.pending = self.root / 'run_pending.json'
        d.save_json(self.pending, self.plan)
        self.original_plan = self.pending.read_bytes()
        self.log = self.root / 'logs' / (self.plan['approval_digest'] + '.json')
        self.output = StringIO()
        self.start(patch('sys.stdout', self.output))
        self.sleep = self.start(patch.object(d.time, 'sleep'))
        def get(fake, userId, id):
            def act():
                if id not in fake.p.filters:
                    raise ApiError(404, 'notFound')
                return deepcopy(fake.p.filters[id])
            return Req(act)
        self.start(patch.object(FakeFilters, 'get', new=get, create=True))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_plan(self, **kwargs):
        return d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                         self.log, defer_unconfirmed=True, **kwargs)

    def state(self):
        return json.loads(self.log.read_text())

    def seed(self):
        d.save_json(self.log, {'account': self.plan['account'], 'plan_digest': self.plan['approval_digest'],
            'events': [], 'completed_add': 0, 'completed_remove': 0,
            'acknowledged_add': 0, 'acknowledged_remove': 0, 'verified': False,
            'batch_checkpoint': {'schema': 2, 'rows': self.rows, 'labels': self.labels, 'add': [], 'remove': [],
                'pending': None, 'deferred': {}, 'available_ids': [r['id'] for r in self.rows],
                'readback_confirmed': True, 'unconfirmed_ids': []}})

    def complete(self, result):
        self.assertTrue(result['verified'])
        self.assertTrue(self.state()['verified'])
        self.assertTrue(self.state()['batch_checkpoint']['readback_confirmed'])
        rows, _ = d.api_fetch(self.client)
        self.assertEqual(d.counter(r['filter'] for r in rows), d.counter(d.compile_xml(self.master)))
        self.assertEqual(self.pending.read_bytes(), self.original_plan)

    def test_healthy_run_final_wait_and_no_duplicate_resume(self):
        self.complete(self.run_plan())
        count = self.client.n
        self.complete(self.run_plan())
        self.assertEqual(self.client.n, count)
        self.assertIn(30, [c.args[0] for c in self.sleep.call_args_list])

    def test_failed_delete_defers_only_it_and_blocks_dependent_add_phase(self):
        original = FakeFilters.delete
        calls = []
        first = next(r['id'] for r in self.rows if r['filter'] == self.plan['remove'][0])
        def delete(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts['id'])
                if opts['id'] == first and calls.count(first) == 1:
                    raise ApiError(500)
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'delete', new=delete))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(self.state()['acknowledged_remove'], 2)
        self.assertEqual(self.client.n, len(self.base))
        self.assertEqual(calls.count(first), 1)
        self.assertEqual(len(self.state()['batch_checkpoint']['deferred']), 4)
        self.complete(self.run_plan())
        self.assertEqual(calls.count(first), 2)
        self.assertEqual(sorted(Counter(calls).values()), [1, 1, 2])

    def test_unknown_create_is_never_resent_even_on_next_run(self):
        original = FakeFilters.create
        calls = []
        failed_rule = self.plan['add'][0]
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(deepcopy(opts['body']))
                if len(calls) == 1:
                    raise ApiError(500)
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(self.state()['acknowledged_add'], 2)
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.run_plan()['verified'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.state()['batch_checkpoint']['deferred']['add:0']['status'], 'unknown')
        # A late server commit becomes visible: reconcile it, never create again.
        original(self.client.users().settings().filters(), userId='me', body=d.to_api(failed_rule, self.labels)).execute()
        self.complete(self.run_plan())
        self.assertEqual(len(calls), 3)

    def test_create_applied_then_error_is_recognized_without_retry(self):
        original = FakeFilters.create
        calls = []
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts)
                response = req.execute()
                if len(calls) == 1:
                    raise ApiError(500)
                return response
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        self.complete(self.run_plan())
        self.assertEqual(len(calls), 3)

    def test_crash_after_send_is_reconciled_without_duplicate(self):
        original = FakeFilters.create
        sent = []
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                pending = self.state()['batch_checkpoint']['pending']
                self.assertEqual(pending['kind'], 'add')
                sent.append(opts)
                response = req.execute()
                if len(sent) == 1:
                    raise KeyboardInterrupt()
                return response
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan()
        self.assertIsNotNone(self.state()['batch_checkpoint']['pending'])
        self.complete(self.run_plan())
        self.assertEqual(len(sent), 3)

    def test_crash_before_create_result_stays_unknown_and_other_creates_continue(self):
        original = FakeFilters.create
        calls = []
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts)
                if len(calls) == 1:
                    raise KeyboardInterrupt()
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan()
        self.assertFalse(self.run_plan()['verified'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.state()['acknowledged_add'], 2)

    def test_partial_list_missing_target_uses_get_and_does_not_infer_deletion(self):
        self.seed()
        omitted = next(r['id'] for r in self.rows if r['filter'] == self.plan['remove'][0])
        original = FakeFilters.list
        def listing(fake, **opts):
            req = original(fake, **opts)
            return Req(lambda: {'filter': [r for r in req.execute()['filter'] if r['id'] != omitted]})
        self.start(patch.object(FakeFilters, 'list', new=listing))
        calls = []
        get = FakeFilters.get
        def observed(fake, **opts):
            calls.append(opts['id'])
            return get(fake, **opts)
        self.start(patch.object(FakeFilters, 'get', new=observed))
        self.complete(self.run_plan())
        self.assertIn(omitted, calls)
        self.assertNotIn(omitted, self.client.filters)

    def test_unavailable_target_is_held_and_other_deletions_continue(self):
        self.seed()
        omitted = next(r['id'] for r in self.rows if r['filter'] == self.plan['remove'][0])
        original = FakeFilters.list
        def listing(fake, **opts):
            req = original(fake, **opts)
            return Req(lambda: {'filter': [r for r in req.execute()['filter'] if r['id'] != omitted]})
        self.start(patch.object(FakeFilters, 'list', new=listing))
        def get(fake, **opts):
            return Req(lambda: (_ for _ in ()).throw(ApiError(500)))
        self.start(patch.object(FakeFilters, 'get', new=get))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertIn(omitted, self.client.filters)
        self.assertEqual(self.state()['acknowledged_remove'], 2)
        self.assertEqual(self.state()['completed_remove'], 0)
        self.assertIn(60, [c.args[0] for c in self.sleep.call_args_list])

    def test_missing_unrelated_id_keeps_final_result_partial_after_other_work(self):
        self.seed()
        omitted = self.rows[0]['id']
        original = FakeFilters.list
        def listing(fake, **opts):
            req = original(fake, **opts)
            return Req(lambda: {'filter': [r for r in req.execute()['filter'] if r['id'] != omitted]})
        self.start(patch.object(FakeFilters, 'list', new=listing))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(self.state()['acknowledged_remove'], 3)
        self.assertEqual(self.state()['acknowledged_add'], 3)
        self.assertEqual(self.state()['completed_add'], 0)
        self.assertEqual(self.state()['batch_checkpoint']['unconfirmed_ids'], [omitted])

    def test_observed_foreign_change_stops_before_writes(self):
        self.seed()
        self.client.filters[self.rows[0]['id']]['criteria'] = {'from': 'external@example.invalid'}
        before = deepcopy(self.client.filters)
        with self.assertRaisesRegex(d.DeploymentError, '既存フィルタ'):
            self.run_plan()
        self.assertEqual(before, self.client.filters)

    def test_delete_applied_with_missing_list_entry_requires_actual_get_404(self):
        original = FakeFilters.delete
        calls = []
        def delete(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts['id'])
                result = req.execute()
                if len(calls) == 1:
                    raise ApiError(500)
                return result
            return Req(send)
        self.start(patch.object(FakeFilters, 'delete', new=delete))
        self.complete(self.run_plan())
        self.assertEqual(len(calls), 3)

    def test_permanent_rejected_create_is_retried_only_on_next_invocation(self):
        original = FakeFilters.create
        calls = []
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts)
                if len(calls) == 1:
                    raise ApiError(400, 'badRequest')
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        self.assertFalse(self.run_plan()['verified'])
        self.assertEqual(len(calls), 3)
        self.complete(self.run_plan())
        self.assertEqual(len(calls), 4)

    def test_new_mode_rejects_batch_sends_before_read_or_write(self):
        with patch.object(d, 'fetch_for_deploy', side_effect=AssertionError('no API')):
            with self.assertRaisesRegex(d.DeploymentError, '単独|batch-size 1'):
                self.run_plan(batch_size=2)

    def test_main_retains_plan_and_returns_partial_exit_code_then_resumes_without_flag(self):
        original = FakeFilters.delete
        failures = [True]
        def delete(fake, **opts):
            req = original(fake, **opts)
            def send():
                if failures[0]:
                    failures[0] = False
                    raise ApiError(500)
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'delete', new=delete))
        argv = ['run', '--local-dir', str(self.root), '--expected-email', self.plan['account'], '--defer-unconfirmed']
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'confirm_interactive', return_value=True), \
             patch.object(d, 'make_plan', side_effect=AssertionError('do not replan')), \
             patch('gmail_message_reclassifier.offer_after_deploy') as messages, redirect_stderr(self.output):
            self.assertEqual(d.main(argv), 4, self.output.getvalue())
            messages.assert_not_called()
            self.assertEqual(self.pending.read_bytes(), self.original_plan)
            self.assertEqual(d.main(argv[:-1]), 0, self.output.getvalue())
        self.assertFalse(self.pending.exists())
        self.assertTrue(self.state()['verified'])

    def test_journal_corruption_is_rejected_before_network(self):
        self.seed()
        state = self.state()
        state['batch_checkpoint']['remove'] = [2]
        d.save_json(self.log, state)
        with self.assertRaisesRegex(d.DeploymentError, '進捗と承認計画'):
            self.run_plan()

    def test_local_persistence_failure_never_causes_another_send(self):
        original = d.save_json
        calls = []
        create = FakeFilters.create
        def write(path, state):
            original(path, state)
            if (state.get('batch_checkpoint', {}).get('pending') or {}).get('kind') == 'add':
                raise OSError('disk error')
        def make(fake, **opts):
            calls.append(opts)
            return create(fake, **opts)
        self.start(patch.object(FakeFilters, 'create', new=make))
        with patch.object(d, 'save_json', side_effect=write), self.assertRaises(OSError):
            self.run_plan()
        self.assertEqual(len(calls), 1)  # Request construction is allowed; no HTTP send took place.
        self.assertEqual(self.client.n, len(self.base))

    def test_unknown_label_blocks_only_dependent_rules(self):
        write_xml(self.master, [{'from': 'keep@example.invalid', 'label': 'Type/Test'},
            {'from': 'foo@example.invalid', 'label': 'Service/Foo'},
            {'from': 'bar@example.invalid', 'label': 'Service/Bar'},
            {'from': 'existing@example.invalid', 'label': 'Type/Test'}])
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', self.labels)
        d.save_json(self.pending, self.plan)
        original = FakeLabels.create
        def create(fake, **opts):
            if opts['body']['name'] == 'Service/Foo':
                return Req(lambda: (_ for _ in ()).throw(ApiError(500)))
            return original(fake, **opts)
        self.start(patch.object(FakeLabels, 'create', new=create))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(self.state()['acknowledged_add'], 2)
        self.assertIn('Service/Bar', self.client.id_to_label.values())
        self.assertNotIn('Service/Foo', self.client.id_to_label.values())
        self.assertEqual(self.state()['batch_checkpoint']['deferred']['label:Service/Foo']['status'], 'unknown')
        fetch = d.api_fetch
        calls = []
        def fail_after_initial(*args, **kwargs):
            calls.append(True)
            if len(calls) > 1:
                raise ApiError(500)
            return fetch(*args, **kwargs)
        with patch.object(d, 'api_fetch', new=fail_after_initial):
            self.assertFalse(self.run_plan()['verified'])
        held = self.state()['batch_checkpoint']['deferred']['label:Service/Foo']
        self.assertTrue(held['attempted'])
        self.assertEqual(held['status'], 'unknown')

    def test_global_read_failures_prevent_more_writes_until_later_confirmation(self):
        original_create = FakeFilters.create
        calls = []
        original_list = FakeFilters.list
        def listing(fake, **opts):
            if calls:
                return Req(lambda: (_ for _ in ()).throw(ApiError(500)))
            return original_list(fake, **opts)
        def create(fake, **opts):
            calls.append(opts)
            return Req(lambda: (_ for _ in ()).throw(ApiError(500)))
        self.start(patch.object(FakeFilters, 'list', new=listing))
        self.start(patch.object(FakeFilters, 'create', new=create))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.state()['batch_checkpoint']['read_available'])

    def test_new_mode_can_replace_an_entire_account_without_a_keep_rule(self):
        write_xml(self.master, [{'from': 'only@example.invalid', 'label': 'Type/Test'}])
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', self.labels)
        self.complete(self.run_plan())

    def test_partial_readback_does_not_mark_all_acknowledged_work_complete_on_resume(self):
        self.complete(self.run_plan())
        original = FakeFilters.list
        def listing(fake, **opts):
            req = original(fake, **opts)
            return Req(lambda: {'filter': req.execute()['filter'][:-1]})
        self.start(patch.object(FakeFilters, 'list', new=listing))
        argv = ['run', '--local-dir', str(self.root), '--expected-email', self.plan['account']]
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'confirm_interactive', return_value=True), redirect_stderr(self.output):
            self.assertEqual(d.main(argv), 4, self.output.getvalue())
        self.assertTrue(self.pending.exists())
        self.assertFalse(self.state()['verified'])

    def test_get_404_for_unsent_target_is_external_drift_not_success(self):
        self.seed()
        missing = next(r['id'] for r in self.rows if r['filter'] == self.plan['remove'][0])
        del self.client.filters[missing]
        before = deepcopy(self.client.filters)
        with self.assertRaisesRegex(d.DeploymentError, '外部変更'):
            self.run_plan()
        self.assertEqual(self.client.filters, before)

    def test_corrupt_deferred_operation_is_rejected_without_writes(self):
        self.seed()
        state = self.state()
        state['batch_checkpoint']['deferred']['add:99'] = {
            'kind': 'add', 'step': 99, 'attempted': True, 'status': 'unknown'}
        d.save_json(self.log, state)
        before = deepcopy(self.client.filters)
        with self.assertRaises(d.DeploymentError):
            self.run_plan()
        self.assertEqual(self.client.filters, before)

    def test_old_batch_partial_journal_can_opt_in_without_repeating_applied_steps(self):
        # Start with the original engine and a persisted single confirmed operation.
        d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                  self.log, batch_size=1, readback_interval=1)
        before = self.client.n
        self.complete(self.run_plan())
        self.assertEqual(self.client.n, before)

    def test_add_first_plan_keeps_removals_blocked_when_create_unknown(self):
        self.plan['operation_order'] = 'add_first'
        self.plan['approval_digest'] = d.json_hash({k:v for k,v in self.plan.items() if k not in ('created_utc', 'approval_digest')})
        d.save_json(self.pending, self.plan)
        self.log = self.root / 'logs' / (self.plan['approval_digest'] + '.json')
        original = FakeFilters.create
        calls = []
        def create(fake, **opts):
            req = original(fake, **opts)
            def send():
                calls.append(opts)
                if len(calls) == 1:
                    raise ApiError(500)
                return req.execute()
            return Req(send)
        self.start(patch.object(FakeFilters, 'create', new=create))
        result = self.run_plan()
        self.assertFalse(result['verified'])
        self.assertEqual(self.state()['acknowledged_remove'], 0)
        self.assertEqual(self.state()['acknowledged_add'], 2)

    def test_fifty_operation_readback_cadence_is_preserved(self):
        write_xml(self.master, [{'from': 'keep@example.invalid', 'label': 'Type/Test'}] + [
            {'from': f'added{i}@example.invalid', 'label': 'Type/Test'} for i in range(51)])
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', self.labels)
        fetch = d.api_fetch
        reads = []
        def observe(*args, **kwargs):
            reads.append(len(self.client.filters))
            return fetch(*args, **kwargs)
        with patch.object(d, 'api_fetch', new=observe):
            self.run_plan()
        # Start, removal phase end, 50 added, phase end/final (no unverified success).
        self.assertEqual(reads[:3], [4, 1, 51])
        self.assertTrue(self.state()['verified'])
