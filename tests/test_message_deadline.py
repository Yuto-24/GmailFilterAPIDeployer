"""Time budgets retain a fixed authorized plan and finish owned-write readback."""
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_auto_messages as auto
import test_messages as fixtures


class MessageDeadlineTests(unittest.TestCase):
    setUp = fixtures.MessageTests.setUp
    plan = fixtures.MessageTests.plan
    args = auto.AutoMessagesTests.args
    authorize = auto.AutoMessagesTests.authorize

    def test_expired_upfront_budget_retains_approved_fixed_plan_before_preflight(self):
        plan = self.plan()
        args = self.authorize()
        self.assertFalse(m.execute(self.api, plan, self.local, deadline=0,
                                   authorization=args._message_authorization))
        self.assertEqual(self.client.writes, [])
        self.assertEqual(m.pending(self.local)['run_id'], plan.directory.name)
        self.assertIsNone(self.api.deadline)
        with patch('sys.stdin.isatty', return_value=False):
            self.assertTrue(m.execute(self.api, plan, self.local,
                                      authorization=args._message_authorization))
        self.assertFalse((self.local/m.PENDING).exists())

    def test_deadline_after_write_finishes_readback_and_resumes_without_duplicate(self):
        plan = self.plan()
        args = self.authorize()
        now = [0]
        self.client.on_write = lambda body: now.__setitem__(0, 11)
        with patch.object(m.time, 'monotonic', side_effect=lambda: now[0]):
            self.assertFalse(m.execute(self.api, plan, self.local, batch_size=1, deadline=10,
                                       authorization=args._message_authorization))
        self.assertEqual(len(self.client.writes), 1)
        first = self.client.writes[0]
        journal = m.Journal(plan)
        try:
            states = dict(journal.db.execute('SELECT message,status FROM progress'))
            self.assertEqual(states[first['ids'][0]], 'applied')
            self.assertNotIn('pending', states.values())
        finally:
            journal.close()
        self.client.on_write = None
        self.assertTrue(m.execute(self.api, plan, self.local, authorization=args._message_authorization))
        self.assertEqual(sum(body == first for body in self.client.writes), 1)

    def test_expiration_before_final_verify_keeps_plan_even_if_all_writes_applied(self):
        self.client.mail = {'a': self.client.mail['a']}
        plan = self.plan()
        args = self.authorize()
        now = [0]
        self.client.on_write = lambda body: now.__setitem__(0, 11)
        with patch.object(m.time, 'monotonic', side_effect=lambda: now[0]):
            self.assertFalse(m.execute(self.api, plan, self.local, deadline=10,
                                       authorization=args._message_authorization))
        self.assertTrue((self.local/m.PENDING).exists())
        self.assertEqual(len(self.client.writes), 1)
        self.client.on_write = None
        self.assertTrue(m.execute(self.api, plan, self.local, authorization=args._message_authorization))
        self.assertEqual(len(self.client.writes), 1)

    def test_invalid_time_budgets_fail_before_oauth(self):
        with patch.object(d, 'get_client', side_effect=AssertionError('no OAuth')):
            for value in ('0','-1','nan','inf'):
                with self.subTest(value=value):
                    self.assertEqual(d.main(['run','--expected-email',fixtures.ACCOUNT,
                        '--local-dir',str(self.local),'--max-message-seconds',value]), 2)
