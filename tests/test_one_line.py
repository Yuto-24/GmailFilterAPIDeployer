"""End-to-end tests of the interactive one-line command with a fake Gmail API."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_deployer import BASE, TARGET, FakeClient, write_xml
import gmail_filter_deployer as d

class TestOneLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)
        self.xml = self.local/'masters'/'mailFilters_v0.99.0_proposed_master_20261019.xml'
        self.xml.parent.mkdir()
        write_xml(self.xml, TARGET)
        self.base = self.local/'baseline.xml'
        write_xml(self.base, BASE)
        self.client = FakeClient(d.compile_xml(self.base))
        self.auth = patch.object(d, 'get_client', return_value=(self.client, object()))
        self.user = patch.object(d, 'account_email', return_value='demo@example.invalid')
        self.env = patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL':'demo@example.invalid'}, clear=True)
        self.auth.start(); self.user.start(); self.env.start()
        self.addCleanup(self.auth.stop); self.addCleanup(self.user.stop); self.addCleanup(self.env.stop)
        self.argv = ['run','--local-dir',str(self.local),'--batch-size','1']

    def test_run_full_deploy_and_idempotent_second_run(self):
        with patch.object(d,'confirm_interactive',return_value=True) as confirmation:
            self.assertEqual(d.main(self.argv),0)
            confirmation.assert_called_once()
        current = [x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(d.counter(current),d.counter(d.compile_xml(self.xml)))
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertTrue((self.local/'run_plan.json').exists())
        self.assertEqual(len(list((self.local/'logs').glob('*.json'))),1)
        with patch.object(d,'confirm_interactive',side_effect=AssertionError('should not prompt')):
            self.assertEqual(d.main(self.argv),0)

    def test_decline_makes_no_mutations(self):
        before=d.counter([x['filter'] for x in d.api_fetch(self.client)[0]])
        with patch.object(d,'confirm_interactive',return_value=False):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(before,d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]))
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertTrue((self.local/'run_plan.json').exists())

    def test_resume_partial_deploy(self):
        real_perform=d.perform
        def interrupt(client,plan,expected,digest,log,batch,verbose=False):
            live, labels = d.api_fetch(client)
            for name in plan['new_label_names']:
                if name not in labels:
                    lab=client.users().labels().create(userId='me',body={'name':name}).execute()
                    labels[name]=lab['id']
            client.users().settings().filters().create(userId='me',body=d.to_api(plan['add'][0],labels)).execute()
            raise d.DeploymentError('simulated interruption')
        with patch.object(d,'confirm_interactive',return_value=True), patch.object(d,'perform',side_effect=interrupt):
            self.assertEqual(d.main(self.argv),2)
        self.assertTrue((self.local/'run_pending.json').exists())
        with patch.object(d,'confirm_interactive',return_value=True), patch.object(d,'perform',real_perform):
            self.assertEqual(d.main(self.argv),0)
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]),d.counter(d.compile_xml(self.xml)))

    def test_pending_plan_with_unrecognized_live_changes_fails(self):
        with patch.object(d,'confirm_interactive',return_value=True), patch.object(d,'perform',side_effect=d.DeploymentError('interrupted')):
            self.assertEqual(d.main(self.argv),2)
        lid=self.client.users().labels().create(userId='me',body={'name':'Service/Foreign'}).execute()['id']
        self.client.users().settings().filters().create(userId='me',body={'criteria':{'from':'other@example.invalid'},'action':{'addLabelIds':[lid],'removeLabelIds':[]}}).execute()
        with patch.object(d,'confirm_interactive',side_effect=AssertionError('must stop before prompt')):
            self.assertEqual(d.main(self.argv),2)
        self.assertTrue((self.local/'run_pending.json').exists())

    def test_disabled_gmail_api_reports_no_changes_and_help_link(self):
        class ApiDisabled(Exception):
            def __str__(self):
                return ('<HttpError 403: Gmail API has not been used in project 288039233263 '
                        'before or it is disabled. reason=accessNotConfigured>')
        with patch.object(d,'api_fetch',side_effect=ApiDisabled()), \
             patch.object(d,'confirm_interactive',side_effect=AssertionError('must not prompt')):
            with patch('sys.stderr') as stderr:
                self.assertEqual(d.main(self.argv),2)
                text=''.join(str(x) for x in stderr.write.call_args_list)
                self.assertIn('288039233263',text)
                self.assertIn('Gmail設定は一切変更していません',text)
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertFalse((self.local/'run_plan.json').exists())

    def test_other_preflight_api_error_reports_safe_stop(self):
        with patch.object(d,'api_fetch',side_effect=RuntimeError('test 401')), \
             patch.object(d,'confirm_interactive',side_effect=AssertionError('must not prompt')):
            with patch('sys.stderr') as stderr:
                self.assertEqual(d.main(self.argv),2)
                text=''.join(str(x) for x in stderr.write.call_args_list)
                self.assertIn('Gmail設定は変更していません',text)
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_delete_mismatch_preserved_with_non_delete_update(self):
        baseline = d.compile_xml(self.base)
        gmail_only = next(x for x in baseline if d.is_delete(x))
        # The target XML advertises a different Delete policy, which is deferred.
        altered = [dict(x) for x in baseline if not d.is_delete(x)]
        altered.append({'criteria':{'from':'legacy@ads.example.invalid'},
                        'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}})
        self.client = FakeClient(altered)
        with patch.object(d, 'get_client', return_value=(self.client,object())), \
             patch.object(d, 'confirm_interactive',return_value=True):
            self.assertEqual(d.main(self.argv),0)
        live = [x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(len([x for x in live if d.is_delete(x)]),1)
        self.assertEqual(next(x for x in live if d.is_delete(x))['criteria']['from'], 'legacy@ads.example.invalid')
        self.assertNotIn(gmail_only,live)
        self.assertFalse((self.local/'run_pending.json').exists())
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=AssertionError('must not prompt')):
            self.assertEqual(d.main(self.argv),0)

    def test_delete_mismatch_preserved_after_interruption_resume(self):
        baseline = d.compile_xml(self.base)
        altered = [x for x in baseline if not d.is_delete(x)]
        self.client = FakeClient(altered)
        actual_perform=d.perform
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',return_value=True), \
             patch.object(d,'perform',side_effect=d.DeploymentError('interrupted')):
            self.assertEqual(d.main(self.argv),2)
        self.assertTrue((self.local/'run_pending.json').exists())
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',return_value=True), \
             patch.object(d,'perform',actual_perform):
            self.assertEqual(d.main(self.argv),0)
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertEqual(sum(1 for x in d.api_fetch(self.client)[0] if d.is_delete(x['filter'])),0)

    def test_trash_mismatch_remains_hard_stop(self):
        baseline = d.compile_xml(self.base)
        altered = [x for x in baseline if not d.is_trash(x)]
        self.client = FakeClient(altered)
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=AssertionError('must not prompt')):
            self.assertEqual(d.main(self.argv),2)
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_interactive_mode_required_before_mutation(self):
        before=len(self.client.filters)
        with patch('sys.stdin.isatty',return_value=False):
            self.assertEqual(d.main(self.argv),2)
        self.assertEqual(len(self.client.filters),before)
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_account_mismatch_stops(self):
        with patch.object(d,'account_email',return_value='wrong@example.invalid'):
            self.assertEqual(d.main(self.argv),2)
        self.assertFalse((self.local/'run_plan.json').exists())

    def test_run_prints_progress_by_default(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with patch.object(d,'confirm_interactive',return_value=True), redirect_stdout(out):
            self.assertEqual(d.main(self.argv), 0)
        result=out.getvalue()
        for stage in range(1, 7):
            self.assertIn(f'[{stage}/6]', result)
        self.assertIn('追加済み:', result)
        self.assertIn('旧ルール削除済み:', result)
        self.assertNotIn('[詳細] 追加 [',result)

    def test_run_verbose_prints_per_filter_actions(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out=StringIO()
        with patch.object(d,'confirm_interactive',return_value=True), redirect_stdout(out):
            self.assertEqual(d.main(self.argv+['-v']),0)
        result=out.getvalue()
        self.assertIn('[詳細] 追加 [1/2]',result)
        self.assertIn('[詳細] 削除 [1/1]',result)
        self.assertIn('[詳細] 最終API照合中',result)
        self.assertIn('[詳細] Master SHA-256:',result)

    def test_run_verbose_decline_makes_no_mutations(self):
        from contextlib import redirect_stdout
        from io import StringIO
        before=d.counter([x['filter'] for x in d.api_fetch(self.client)[0]])
        with patch.object(d,'confirm_interactive',return_value=False), redirect_stdout(StringIO()):
            self.assertEqual(d.main(self.argv+['--verbose']),0)
        self.assertEqual(before,d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]))
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_apply_verbose_parser(self):
        with patch.object(d,'cmd_apply') as action:
            self.assertEqual(d.main(['apply','--expected-email','demo@example.invalid','--approve','digest','-v']),0)
            self.assertTrue(action.call_args.args[0].verbose)

    def test_no_account_pin_fails_closed(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(d.main(self.argv),2)

    def test_denied_incomplete_plan_does_not_permanently_lock(self):
        with patch.object(d,'confirm_interactive',return_value=False):
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(d.main(self.argv),0)

if __name__=='__main__': unittest.main()
