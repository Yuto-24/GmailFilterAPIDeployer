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

    def test_group_approvals_once_at_end(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out=StringIO()
        approvals=[]
        def fake_confirm(prompt=""):
            self.assertIn("[5/6]",out.getvalue())
            approvals.append(prompt)
            return True
        with patch.object(d,"confirm_interactive",side_effect=fake_confirm),redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(len(approvals),1)  # Ordinary changes only.
        self.assertIn("上記の内容でGmailのフィルタ設定を変更",approvals[0])
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]),
                         d.counter(d.compile_xml(self.xml)))

    def test_decline_delete_but_approve_other_then_resume_next_run(self):
        base=d.compile_xml(self.base)
        custom_delete={'criteria':{'from':'custom.delete@example.invalid'},
                       'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        self.client=FakeClient([x for x in base if not d.is_delete(x)]+[custom_delete])
        def approve_except_delete(prompt):
            return "Action/Delete" not in prompt
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=approve_except_delete) as ask:
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(ask.call_count,2)
        now=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertIn(custom_delete,now)
        self.assertNotEqual(d.counter(now),d.counter(d.compile_xml(self.xml)))
        self.assertFalse((self.local/'run_pending.json').exists())
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',return_value=True) as ask:
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(ask.call_count, 2)
            self.assertIn("Action/Delete", ask.call_args_list[0].args[0])
            self.assertIn("上記の内容で", ask.call_args_list[1].args[0])
        now=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(d.counter(now),d.counter(d.compile_xml(self.xml)))

    def test_approve_delete_and_basic_with_final_confirmation(self):
        """Apply approved groups after final consent and preserve the native subject criterion."""
        base=d.compile_xml(self.base)
        foreign={'criteria':{'from':'obsolete.delete@example.invalid'},
                 'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        self.client=FakeClient([x for x in base if not d.is_delete(x)] + [foreign])
        def approve_only_delete(prompt):
            """Approve the Delete group and the final confirmation."""
            return "Action/Delete" in prompt or "上記の内容で" in prompt
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=approve_only_delete) as ask:
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(ask.call_count,2)
        now=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(sum(d.is_delete(x) for x in now),1)
        self.assertTrue(any(x["criteria"].get("from")=="ads@example.invalid" for x in now))
        self.assertTrue(any(x["criteria"].get("from")=="service@example.invalid" and
                            x["criteria"].get("subject")=="{sale coupon}" for x in now))
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_all_groups_declined_no_gmail_changes(self):
        base=d.compile_xml(self.base)
        foreign={'criteria':{'from':'obsolete.delete@example.invalid'},
                 'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        self.client=FakeClient(base+[foreign])
        before=d.counter([x['filter'] for x in d.api_fetch(self.client)[0]])
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',return_value=False) as ask:
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(ask.call_count,2)
        self.assertEqual(before,d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]))
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_order_details_and_final_confirmation_before_api_mutation(self):
        from contextlib import redirect_stdout
        from io import StringIO
        base=d.compile_xml(self.base)
        custom_delete={'criteria':{'from':'custom-delete@example.invalid'},
                       'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        custom_trash={'criteria':{'from':'custom-trash@example.invalid'},
                      'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}
        self.client=FakeClient([x for x in base if not d.is_delete(x) and not d.is_trash(x)] +
                               [custom_delete,custom_trash])
        output=StringIO()
        prompts=[]
        count_before=len(self.client.filters)
        def confirm(prompt):
            txt=output.getvalue()
            prompts.append(prompt)
            self.assertEqual(len(self.client.filters), count_before)
            self.assertIn('【基本（Type・Service・Org等）】',txt)
            self.assertIn('【Action/Delete（ラベル付与）】',txt)
            if len(prompts)>1:
                self.assertIn('【Trash（実際のゴミ箱移動）】',txt)
            return True
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=confirm),redirect_stdout(output):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(len(prompts),3)
        self.assertIn('Action/Delete',prompts[0])
        self.assertIn('Trash',prompts[1])
        self.assertIn('上記の内容で',prompts[2])
        txt=output.getvalue()
        self.assertLess(txt.index('【基本（Type・Service・Org等）】'),
                        txt.index('【Action/Delete（ラベル付与）】'))
        self.assertLess(txt.index('【Action/Delete（ラベル付与）】'),
                        txt.index('【Trash（実際のゴミ箱移動）】'))
        self.assertIn('custom-delete@example.invalid',txt)
        self.assertIn('custom-trash@example.invalid',txt)
        self.assertIn('ads@example.invalid',txt)
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]),
                         d.counter(d.compile_xml(self.xml)))

    def test_final_decline_after_both_group_approvals_is_noop(self):
        from contextlib import redirect_stdout
        from io import StringIO
        baseline=d.compile_xml(self.base)
        custom_delete={'criteria':{'from':'custom-delete@example.invalid'},
                       'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        custom_trash={'criteria':{'from':'custom-trash@example.invalid'},
                      'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}
        self.client=FakeClient(baseline+[custom_delete,custom_trash])
        before=d.counter([x['filter'] for x in d.api_fetch(self.client)[0]])
        prompts=[]
        def choice(prompt):
            prompts.append(prompt)
            return len(prompts)<3
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=choice), \
             redirect_stdout(StringIO()):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(len(prompts),3)
        self.assertIn('Action/Delete',prompts[0])
        self.assertIn('Trash',prompts[1])
        self.assertIn('上記の内容で',prompts[2])
        self.assertEqual(before,d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]))
        self.assertFalse((self.local/'run_pending.json').exists())
        self.assertFalse((self.local/'logs').exists())

    def test_sensitive_declined_but_basic_applied_after_final_consent(self):
        """Keep declined sensitive groups while applying the approved native-subject change."""
        baseline=d.compile_xml(self.base)
        custom_delete={'criteria':{'from':'custom-delete@example.invalid'},
                       'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}
        custom_trash={'criteria':{'from':'custom-trash@example.invalid'},
                      'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}
        self.client=FakeClient([x for x in baseline if not d.is_delete(x) and not d.is_trash(x)] +
                               [custom_delete,custom_trash])
        prompts=[]
        def choice(prompt):
            """Decline sensitive groups and approve only the final ordinary-change confirmation."""
            prompts.append(prompt)
            return '上記の内容で' in prompt
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=choice):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(len(prompts),3)
        live=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertIn(custom_delete,live)
        self.assertIn(custom_trash,live)
        self.assertTrue(any(x['criteria'].get('from')=='service@example.invalid'
                            and x['criteria'].get('subject')=='{sale coupon}' for x in live))

    def test_basic_default_summary_without_rule_details(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out=StringIO()
        with patch.object(d,'confirm_interactive',return_value=False),redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
        result=out.getvalue()
        self.assertIn('【基本（Type・Service・Org等）】',result)
        self.assertIn('基本ルールの全件詳細は -v',result)
        self.assertNotIn('new@example.invalid',result)
        self.assertNotIn('service@example.invalid',result)

    def test_real_api_operation_order_is_deletion_before_creation(self):
        from test_deployer import FakeFilters
        from contextlib import redirect_stdout
        from io import StringIO
        sequence=[]
        original_delete=FakeFilters.delete
        original_create=FakeFilters.create
        def record_delete(instance,*args,**kwargs):
            sequence.append('delete')
            return original_delete(instance,*args,**kwargs)
        def record_create(instance,*args,**kwargs):
            sequence.append('create')
            return original_create(instance,*args,**kwargs)
        with patch.object(FakeFilters,'delete',record_delete), \
             patch.object(FakeFilters,'create',record_create), \
             patch.object(d,'confirm_interactive',return_value=True), \
             redirect_stdout(StringIO()):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(sequence.count('delete'),1)
        self.assertEqual(sequence.count('create'),2)
        self.assertEqual(sequence,['delete','create','create'])

    def test_ambiguous_deletion_skipped_and_partial_summary(self):
        from contextlib import redirect_stdout
        from io import StringIO
        original=d.compile_xml(self.base)
        duplicated=original[1]
        self.client=FakeClient(original+[duplicated])
        out=StringIO()
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',return_value=True) as confirm, \
             redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
        confirm.assert_called_once()
        output=out.getvalue()
        self.assertIn('スキップ予定：2件',output)
        self.assertIn('duplicate_matches',output)
        self.assertIn('結果: 一部未適用',output)
        self.assertIn('スキップ（未適用）: 2件',output)
        after=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(sum(x==duplicated for x in after),2)
        self.assertEqual(len(after),7)
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_ambiguous_deletion_preflight_then_final_declined(self):
        from contextlib import redirect_stdout
        from io import StringIO
        original=d.compile_xml(self.base)
        self.client=FakeClient(original+[original[1]])
        before=d.counter([x['filter'] for x in d.api_fetch(self.client)[0]])
        out=StringIO()
        def decline(prompt):
            self.assertIn('スキップ予定：2件',out.getvalue())
            return False
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=decline), \
             redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
        self.assertEqual(before,d.counter([x['filter'] for x in d.api_fetch(self.client)[0]]))
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_verbose_run_reports_operation_timestamps_and_durations(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(d, 'confirm_interactive', return_value=True), redirect_stdout(output):
            self.assertEqual(d.main(self.argv + ['-v']), 0)
        log = output.getvalue()
        self.assertIn('[処理開始] Gmail labels.list', log)
        self.assertIn('[処理完了] Gmail filters.list', log)
        self.assertIn('[処理開始] Gmail filters.delete', log)
        self.assertIn('[処理完了] Gmail filters.create', log)
        self.assertIn('[処理完了] 承認済み計画の配備', log)
        self.assertIn('[計測] run全体（対話確認を含む）:', log)
        self.assertRegex(
            log,
            r'\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\] '
            r'\[処理完了\] Gmail filters.delete \[1/1\] \(\d+\.\d{2}s\)')

    def test_operation_timer_reports_failure_without_swallowing_exception(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        def fail():
            raise RuntimeError('simulated')
        with redirect_stdout(out):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                d.timed_operation('Gmail filters.delete', fail, verbose=True)
        self.assertIn('[処理失敗] Gmail filters.delete', out.getvalue())

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
        def interrupt(client,plan,expected,digest,log,batch,verbose=False,max_batches=None):
            current, _ = d.api_fetch(client)
            original = plan['remove'][0]
            matches=[x for x in current if x['filter']==original]
            self.assertEqual(len(matches),1)
            client.users().settings().filters().delete(userId='me',id=matches[0]['id']).execute()
            raise d.DeploymentError('simulated interruption immediately after first deletion')
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

    def test_delete_mismatch_master_is_applied_with_non_delete_update(self):
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
        self.assertEqual(next(x for x in live if d.is_delete(x))['criteria']['from'], 'ads@example.invalid')
        self.assertIn(gmail_only,live)
        self.assertFalse((self.local/'run_pending.json').exists())
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=AssertionError('must not prompt')):
            self.assertEqual(d.main(self.argv),0)

    def test_delete_mismatch_master_applies_after_interruption_resume(self):
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
        self.assertEqual(sum(1 for x in d.api_fetch(self.client)[0] if d.is_delete(x['filter'])),1)

    def test_preview_has_all_changes_before_single_prompt(self):
        from contextlib import redirect_stdout
        from io import StringIO
        rows = d.compile_xml(self.base)
        more = list(rows)
        for i in range(21):
            more.append({'criteria':{'from':f'spammer{i}@example.invalid'},
                         'action':{'addLabelIds':['label:Service/Extra'],'removeLabelIds':[]}})
        self.client=FakeClient(more)
        output=StringIO()
        def confirm_once(_prompt):
            content=output.getvalue()
            self.assertIn('[5/6]',content)
            self.assertIn('spammer0@example.invalid',content)
            self.assertIn('spammer20@example.invalid',content)
            self.assertIn('Action/Delete',content)
            self.assertIn('削除する既存ルール:',content)
            return False
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=confirm_once) as prompt, redirect_stdout(output):
            self.assertEqual(d.main(self.argv+['-v']),0)
            prompt.assert_called_once()
        self.assertFalse((self.local/'run_pending.json').exists())

    def test_delete_label_only_no_trash_or_mark_read(self):
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{'from':'x@example.invalid','label':'Action/Delete','shouldNeverSpam':'true'}])
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{'from':'x@example.invalid','label':'Action/Delete','shouldMarkAsRead':'true'}])
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{'from':'x@example.invalid','label':'Action/Delete','shouldTrash':'true'}])

    def test_trash_mismatch_preview_last_prompt_then_apply(self):
        baseline=d.compile_xml(self.base)
        self.client=FakeClient([x for x in baseline if not d.is_trash(x)])
        from contextlib import redirect_stdout
        from io import StringIO
        out=StringIO()
        def approval(_prompt):
            text=out.getvalue()
            self.assertIn('【Trash（実際のゴミ箱移動）】',text)
            self.assertIn('TRASH',text)
            self.assertIn('[5/6]',text)
            self.assertIn('新たに作成するルール（全件）:',text)
            return True
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=approval) as confirm,redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(confirm.call_count, 2)
            self.assertIn("Trash", confirm.call_args_list[0].args[0])
            self.assertIn("上記の内容で", confirm.call_args_list[1].args[0])
        current=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(d.counter(current),d.counter(d.compile_xml(self.xml)))
        self.assertEqual(sum(d.is_trash(x) for x in current),1)

    def test_trash_extra_live_rule_removed_after_single_approval(self):
        from contextlib import redirect_stdout
        from io import StringIO
        baseline=d.compile_xml(self.base)
        extra={'criteria':{'from':'one-off@example.invalid'},
               'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}
        self.client=FakeClient(baseline+[extra])
        out=StringIO()
        def approval(_prompt):
            self.assertIn('【Trash（実際のゴミ箱移動）】',out.getvalue())
            self.assertIn('one-off@example.invalid',out.getvalue())
            return True
        with patch.object(d,'get_client',return_value=(self.client,object())), \
             patch.object(d,'confirm_interactive',side_effect=approval) as confirm,redirect_stdout(out):
            self.assertEqual(d.main(self.argv),0)
            self.assertEqual(confirm.call_count, 2)
            self.assertIn("Trash", confirm.call_args_list[0].args[0])
            self.assertIn("上記の内容で", confirm.call_args_list[1].args[0])
        current=[x['filter'] for x in d.api_fetch(self.client)[0]]
        self.assertEqual(d.counter(current),d.counter(d.compile_xml(self.xml)))


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
