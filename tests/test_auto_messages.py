"""Explicit upfront authorization, automatic handoff, and immutable plan binding."""
from contextlib import redirect_stderr
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures

ACCOUNT = fixtures.ACCOUNT


class AutoMessagesTests(unittest.TestCase):
    setUp = fixtures.MessageTests.setUp
    plan = fixtures.MessageTests.plan
    execute = fixtures.MessageTests.execute
    user_set = fixtures.MessageTests.user_set

    def args(self, **options):
        args = SimpleNamespace(cmd='run', expected_email=ACCOUNT, local_dir=self.local, target=self.master,
            client_secrets=self.local/'credentials.json', token=self.local/'token.json', message_token=None,
            reclassify_existing=True, plan_only=False, resume=None, verbose=False,
            message_batch_size=50, max_message_batches=None)
        for name, value in options.items():
            setattr(args, name, value)
        return args

    def authorize(self, args=None):
        args = args or self.args()
        with patch('sys.stdin.isatty', return_value=True), \
             patch.object(d, 'confirm_interactive', return_value=True), \
             patch.object(m, 'message_client', return_value=self.api):
            self.assertTrue(m.authorize_run(args))
        return args

    def test_refusal_does_not_authenticate_plan_or_write(self):
        with patch('sys.stdin.isatty', return_value=True), \
             patch.object(d, 'confirm_interactive', return_value=False), \
             patch.object(m, 'message_client', side_effect=AssertionError('no OAuth')):
            self.assertFalse(m.authorize_run(self.args()))
        self.assertFalse((self.local/'reclassification').exists())
        self.assertEqual(self.client.writes, [])

    def test_noninteractive_start_does_not_bypass_upfront_consent(self):
        with patch('sys.stdin.isatty', return_value=False), \
             patch.object(m, 'message_client', side_effect=AssertionError('no OAuth')):
            with self.assertRaisesRegex(d.DeploymentError, '対話端末'):
                m.authorize_run(self.args())
        self.assertEqual(self.client.writes, [])

    def test_auto_noop_filter_run_authorizes_once_and_classifies_existing_messages(self):
        oauth = []
        def client(*args, **kwargs):
            oauth.append('messages' if 'scopes' in kwargs else 'settings')
            return self.client, object()
        with patch('sys.stdin.isatty', return_value=True), \
             patch.object(d, 'get_client', side_effect=client), \
             patch.object(d, 'account_email', return_value=ACCOUNT), \
             patch.object(d, 'confirm_interactive', return_value=True) as ask, redirect_stderr(self.out):
            result = d.main(['run', '--target', str(self.master), '--local-dir', str(self.local),
                             '--expected-email', ACCOUNT, '--reclassify-existing'])
        self.assertEqual(result, 0, self.out.getvalue())
        self.assertEqual(oauth, ['messages', 'settings'])
        self.assertEqual(ask.call_count, 1)
        self.assertTrue(self.client.writes)
        self.assertFalse((self.local/m.PENDING).exists())

    def test_authorized_execution_keeps_all_preflight_and_backup_checks(self):
        args = self.authorize()
        plan = self.plan()
        with patch('sys.stdin.isatty', return_value=False), \
             patch.object(d, 'confirm_interactive', side_effect=AssertionError('no second question')):
            self.assertTrue(m.execute(self.api, plan, self.local, authorization=args._message_authorization))
        self.assertTrue((plan.directory/'plan.sqlite').exists())
        journal = m.Journal(plan)
        try:
            self.assertEqual(journal.meta('complete'), 'true')
        finally:
            journal.close()

    def test_wrong_master_account_policy_or_scope_cannot_skip_consent(self):
        args = self.authorize()
        plan = self.plan()
        for field, bad in [('master_sha256','invalid'), ('account','other@example.invalid'),
                           ('policy','other'), ('excluded_labels',[])]:
            with self.subTest(field=field):
                authorization = {**args._message_authorization, field:bad}
                with self.assertRaisesRegex(d.DeploymentError, '起動時の既存メール承認'):
                    m.execute(self.api, plan, self.local, authorization=authorization)
        self.assertEqual(self.client.writes, [])

    def test_upfront_apply_authorization_never_authorizes_restoration(self):
        args = self.authorize()
        plan = self.plan()
        with self.assertRaisesRegex(d.DeploymentError, '起動時の既存メール承認'):
            m.execute(self.api, plan, self.local, mode='restore', authorization=args._message_authorization)
        self.assertEqual(self.client.writes, [])

    def test_cached_message_connection_is_account_bound(self):
        args = self.authorize()
        with patch.object(d, 'get_client', side_effect=AssertionError('cached')):
            self.assertIs(m.message_client(args), self.api)
            args.expected_email = 'different@example.invalid'
            with self.assertRaisesRegex(d.DeploymentError, 'アカウント'):
                m.message_client(args)

    def test_auto_handoff_refuses_unmatched_filter_snapshot(self):
        args = self.authorize()
        with patch.object(m, 'command', side_effect=AssertionError('no plan or update')):
            m.offer_after_deploy(args, self.master, [])
        self.assertEqual(args.exit_code, 4)
        self.assertEqual(self.client.writes, [])

    def test_resume_is_fixed_master_bound_and_automatic_after_upfront_consent(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=1, batch_size=1))
        args = self.authorize()
        before = len(self.client.writes)
        with patch('sys.stdin.isatty', return_value=True), \
             patch.object(d, 'confirm_interactive', side_effect=AssertionError('already authorized')):
            self.assertTrue(m.resume_before_deploy(args))
        self.assertGreater(len(self.client.writes), before)
        self.assertFalse((self.local/m.PENDING).exists())

    def test_auto_message_batch_limit_reports_partial_exit_code(self):
        args = self.authorize(self.args(max_message_batches=1, message_batch_size=1))
        m.command(args)
        self.assertEqual(args.exit_code, 4)
        self.assertTrue((self.local/m.PENDING).exists())

    def test_default_run_has_no_upfront_message_authentication(self):
        with patch.object(m, 'message_client', side_effect=AssertionError('no message OAuth')):
            self.assertTrue(m.authorize_run(self.args(reclassify_existing=False)))
        self.assertFalse((self.local/'reclassification').exists())
