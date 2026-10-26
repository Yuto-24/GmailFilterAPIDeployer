"""Synthetic network budgets, durable windows, and restart/error boundaries."""
from contextlib import ExitStack
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from test_batch import BatchClient
from test_deployer import FakeFilters, Req, write_xml
from test_recovery import ApiError


class CadenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.log = self.local / 'journal.json'
        self.master = self.local / 'mailFilters_v1.0.0_master.xml'
        self.writes = []
        self.reads = []
        self.before = []
        self.output = StringIO()
        out = patch('sys.stdout', self.output)
        out.start()
        self.addCleanup(out.stop)
        sleep = patch.object(d.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def configure(self, removes=0, adds=0):
        """Create approved fixtures with an existing label and no live credentials."""
        keep = [{'from': 'keep@example.invalid', 'label': 'Type/Test'}]
        base = self.local / 'base.xml'
        write_xml(base, keep + [{'from': f'old{i}@example.invalid', 'label': 'Type/Test'} for i in range(removes)])
        write_xml(self.master, keep + [{'from': f'new{i}@example.invalid', 'label': 'Type/Test'} for i in range(adds)])
        self.base = d.compile_xml(base)
        self.client = BatchClient(self.base, self.log)
        _, labels = d.api_fetch(self.client)
        self.plan = d.make_plan(None, self.master, self.base, 'test@example.invalid', labels)

    def state(self):
        return json.loads(self.log.read_text())

    def execute(self, **kwargs):
        """Observe real synthetic requests and require persisted intent at every send."""
        fetch = d.api_fetch
        def read(*args, **opts):
            self.reads.append(len(self.writes))
            return fetch(*args, **opts)
        def wrap(method):
            original = getattr(FakeFilters, method)
            def request(fake, **opts):
                req = original(fake, **opts)
                def send():
                    state = self.state()
                    self.assertIsNotNone(state['batch_checkpoint']['pending'])
                    self.before.append(deepcopy(state))
                    self.writes.append((method, deepcopy(opts)))
                    return req.execute()
                return Req(send)
            return request
        with ExitStack() as stack:
            stack.enter_context(patch.object(d, 'api_fetch', read))
            stack.enter_context(patch.object(FakeFilters, 'create', wrap('create')))
            stack.enter_context(patch.object(FakeFilters, 'delete', wrap('delete')))
            return d.perform(self.client, self.plan, self.plan['account'], self.plan['approval_digest'],
                             self.log, **kwargs)

    def test_49_50_51_and_repeated_boundaries_for_single_writes(self):
        for kind in ('adds', 'removes'):
            for count in (49, 50, 51, 100, 101):
                with self.subTest(kind=kind, count=count):
                    self.setUp()
                    self.configure(**{kind: count})
                    self.execute()
                    expected = [0] + list(range(50, count + 1, 50))
                    if expected[-1] != count:
                        expected.append(count)
                    self.assertEqual(self.reads, expected)
                    self.assertEqual(len(self.writes), count)
                    self.assertTrue(self.state()['verified'])
                    if count >= 50:
                        prior = self.before[49]
                        key = 'add' if kind == 'adds' else 'remove'
                        self.assertEqual(prior['acknowledged_' + key], 49)
                        self.assertEqual(prior['completed_' + key], 0)

    def test_batches_split_at_50_and_phase_transition_resets_window(self):
        self.configure(removes=51, adds=51)
        self.execute(batch_size=20)
        self.assertEqual([len(x) for x in self.client.batches], [20, 20, 10, 1] * 2)
        self.assertEqual(self.reads, [0, 50, 51, 101, 102])
        self.assertEqual([x[0] for x in self.writes], ['delete'] * 51 + ['create'] * 51)

    def test_read_request_reduction_does_not_change_mutations(self):
        totals = []
        for interval in (1, 50):
            self.setUp()
            self.configure(removes=45, adds=43)
            self.execute(readback_interval=interval)
            # Every api_fetch is one labels.list plus one filters.list.
            totals.append((len(self.writes), 2 * len(self.reads)))
        self.assertEqual(totals, [(88, 178), (88, 6)])

    def test_controlled_clock_comparison_preserves_mutations_and_target(self):
        """Measure both reconciliation intervals under identical synthetic API delays."""
        from scripts.benchmark_readback_cadence import compare
        comparison = compare()
        old, new = comparison['trials']
        self.assertTrue(comparison['same_mutations'])
        self.assertTrue(comparison['same_final_state'])
        self.assertTrue(old['target_matches'] and new['target_matches'])
        self.assertEqual((old['write_requests'], new['write_requests']), (88, 88))
        self.assertEqual((old['read_requests'], new['read_requests']), (178, 6))
        self.assertEqual((old['total_requests'], new['total_requests']), (266, 94))
        self.assertEqual((old['controlled_deployment_seconds'], new['controlled_deployment_seconds']),
                         (266.0, 94.0))
        self.assertEqual(old['api_attempts'], {
            'labels.list': 89, 'filters.list': 89, 'filters.delete': 45, 'filters.create': 43})
        self.assertEqual(new['api_attempts'], {
            'labels.list': 3, 'filters.list': 3, 'filters.delete': 45, 'filters.create': 43})

    def test_crash_before_50th_send_keeps_49_acknowledged_and_zero_confirmed(self):
        self.configure(adds=51)
        save = d.save_json
        def crash(path, state):
            save(path, state)
            cp = state.get('batch_checkpoint', {})
            if (cp.get('pending') or {}).get('steps') == [49]:
                raise KeyboardInterrupt()
        with patch.object(d, 'save_json', crash), self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertEqual(len(self.writes), 49)
        self.assertEqual(self.state()['acknowledged_add'], 49)
        self.assertEqual(self.state()['completed_add'], 0)
        self.execute()
        self.assertEqual(len(self.writes), 51)
        self.assertEqual(self.reads, [0, 49, 51])
        self.assertTrue(self.state()['verified'])

    def test_lost_response_after_prior_acknowledgements_never_duplicates(self):
        self.configure(adds=51)
        create = FakeFilters.create
        count = 0
        def lost(fake, **opts):
            req = create(fake, **opts)
            def send():
                nonlocal count
                count += 1
                result = req.execute()
                if count == 25:
                    raise TimeoutError()
                return result
            return Req(send)
        with patch.object(FakeFilters, 'create', lost):
            self.execute()
        self.assertEqual(count, 51)
        self.assertEqual(self.client.n, 52)
        self.assertIn(25, self.reads)
        self.assertTrue(self.state()['verified'])

    def test_partial_batch_after_acknowledged_window_stops_then_resumes(self):
        self.configure(adds=55)
        def partial(batch):
            for i in range(len(batch.requests)):
                if len(self.client.batches) == 2 and i == 1:
                    batch.callback(batch.requests[i][0], None, ApiError(503))
                else:
                    batch.apply(i)
        self.client.executor = partial
        with self.assertRaises(d.DeploymentError):
            self.execute(batch_size=20)
        self.assertEqual(len(self.client.batches), 2)
        self.assertEqual(self.state()['completed_add'], 39)
        self.client.executor = None
        self.execute(batch_size=1)
        self.assertEqual(self.client.n, 56)
        self.assertTrue(self.state()['verified'])

    def test_external_change_is_detected_at_boundary_before_51st_write(self):
        self.configure(adds=51)
        create = FakeFilters.create
        def drift(fake, **opts):
            req = create(fake, **opts)
            def send():
                result = req.execute()
                if fake.p.n == 2:
                    fake.p.filters['filter_1']['criteria'] = {'from': 'external@example.invalid'}
                return result
            return Req(send)
        with patch.object(FakeFilters, 'create', drift), self.assertRaises(d.DeploymentError):
            self.execute()
        self.assertEqual(len(self.writes), 50)
        self.assertEqual(self.reads, [0, 50, 50, 50])
        self.assertFalse(self.state()['verified'])
        self.assertEqual(self.state()['completed_add'], 0)
        with self.assertRaises(d.DeploymentError):
            self.execute()
        self.assertEqual(len(self.writes), 50)

    def test_failed_readback_after_50_acks_stops_and_resume_does_not_repeat(self):
        self.configure(adds=51)
        original = FakeFilters.list
        def unavailable(fake, **opts):
            if len(self.writes) == 50:
                return Req(lambda: (_ for _ in ()).throw(ApiError()))
            return original(fake, **opts)
        with patch.object(FakeFilters, 'list', unavailable), self.assertRaises(ApiError):
            self.execute()
        self.assertEqual(len(self.writes), 50)
        self.assertEqual(self.state()['acknowledged_add'], 50)
        self.assertEqual(self.state()['completed_add'], 0)
        self.execute()
        self.assertEqual(len(self.writes), 51)
        self.assertTrue(self.state()['verified'])

    def test_bounded_exit_confirms_tail_before_return(self):
        self.configure(adds=51)
        self.assertIs(self.execute(batch_size=20, max_batches=1), False)
        self.assertEqual(self.reads, [0, 20])
        self.assertEqual(self.state()['completed_add'], 20)
        self.assertFalse(self.state()['verified'])
        self.execute(batch_size=20)
        self.assertEqual(len(self.writes), 51)

    def test_run_and_apply_use_50_without_redundant_final_read(self):
        for command in ('run', 'apply'):
            with self.subTest(command=command):
                self.setUp()
                self.configure(adds=51)
                planfile = self.local / 'approved.json'
                d.save_json(planfile, self.plan)
                positions = []
                fetch = d.api_fetch
                def read(*args, **kwargs):
                    positions.append(self.client.n - len(self.base))
                    return fetch(*args, **kwargs)
                args = [command, '--local-dir', str(self.local), '--expected-email', self.plan['account']]
                if command == 'apply':
                    args += ['--plan', str(planfile), '--approve', self.plan['approval_digest'], '--log', str(self.log)]
                with patch.object(d, 'get_client', return_value=(self.client, object())), \
                     patch.object(d, 'account_email', return_value=self.plan['account']), \
                     patch.object(d, 'confirm_interactive', return_value=True), \
                     patch.object(d, 'api_fetch', read):
                    self.assertEqual(d.main(args), 0)
                self.assertEqual(positions, [0, 0, 0, 50, 51] if command == 'run' else [0, 50, 51])
                self.assertFalse((self.local / 'run_pending.json').exists())

    def test_run_finishes_journal_after_crash_before_final_readback(self):
        self.configure(adds=3)
        self.log = self.local / 'logs' / (self.plan['approval_digest'] + '.json')
        self.client.log = self.log
        pending = self.local / 'run_pending.json'
        d.save_json(pending, self.plan)
        save = d.save_json
        def crash(path, state):
            save(path, state)
            cp = state.get('batch_checkpoint', {})
            if cp.get('add') == [0, 1, 2] and not cp.get('readback_confirmed') and cp.get('pending') is None:
                raise KeyboardInterrupt()
        with patch.object(d, 'save_json', crash), self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertFalse(self.state()['verified'])
        state = self.state()
        state['events'].append({'note': '照合待ちの記録'})
        d.save_json(self.log, state)
        read_text = Path.read_text
        def locale_read(path, encoding=None, errors=None):
            return read_text(path, encoding=encoding or 'cp932', errors=errors)
        with patch.object(d, 'get_client', return_value=(self.client, object())), \
             patch.object(d, 'account_email', return_value=self.plan['account']), \
             patch.object(d, 'confirm_interactive', side_effect=AssertionError('already applied')), \
             patch.object(Path, 'read_text', locale_read):
            self.assertEqual(d.main(['run', '--local-dir', str(self.local),
                                    '--expected-email', self.plan['account']]), 0)
        self.assertTrue(self.state()['verified'])
        self.assertEqual(self.state()['completed_add'], 3)
        self.assertEqual(self.state()['events'][-1], {'note': '照合待ちの記録'})
        self.assertFalse(pending.exists())

    def test_old_batch_checkpoint_without_new_fields_resumes(self):
        self.configure(adds=51)
        self.execute(batch_size=20, max_batches=1)
        state = self.state()
        for key in ('serial_recovery', 'acknowledged_add', 'acknowledged_remove', 'readback_interval'):
            state.pop(key)
        state['batch_checkpoint'].pop('readback_confirmed')
        d.save_json(self.log, state)
        self.execute(batch_size=1)
        self.assertFalse(self.state()['serial_recovery'])
        self.assertEqual(len(self.writes), 51)
        self.assertTrue(self.state()['verified'])

    def test_permanent_error_after_transient_retry_gets_its_own_readback(self):
        self.configure(adds=2)
        create = FakeFilters.create
        count = 0
        def failures(fake, **opts):
            req = create(fake, **opts)
            def send():
                nonlocal count
                count += 1
                if count == 1:
                    raise ApiError(500)
                req.execute()
                raise ApiError(400)
            return Req(send)
        with patch.object(FakeFilters, 'create', failures), self.assertRaises(ApiError):
            self.execute()
        self.assertEqual(count, 2)
        self.assertEqual(self.reads, [0, 1, 1, 2])
        self.assertEqual(self.state()['completed_add'], 1)
        self.execute()
        self.assertEqual(self.client.n, 3)
        self.assertTrue(self.state()['verified'])


if __name__ == '__main__':
    unittest.main()
