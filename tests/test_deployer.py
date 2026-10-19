"""Synthetic fixtures only; never check personal filter exports into Git."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gmail_filter_deployer as d


def write_xml(path, rows):
    ET.register_namespace('', d.ATOM)
    ET.register_namespace('apps', d.APPS)
    feed=ET.Element(f'{{{d.ATOM}}}feed')
    for props in rows:
        entry=ET.SubElement(feed, f'{{{d.ATOM}}}entry')
        for k,v in props.items():
            ET.SubElement(entry, f'{{{d.APPS}}}property', {'name':k,'value':v})
    ET.ElementTree(feed).write(path, encoding='utf-8', xml_declaration=True)

BASE=[
    {'from':'service@example.invalid','label':'Service/Example'},
    {'from':'service@example.invalid','label':'Type/Promotion'},
    {'from':'ads@example.invalid','label':'Action/Delete'},
    {'hasTheWord':'in:spam {phishing}', 'shouldTrash':'true'},
]
TARGET=[
    BASE[0],
    {'from':'service@example.invalid','subject':'{sale coupon}', 'label':'Type/Promotion'},
    BASE[2], BASE[3],
    {'from':'new@example.invalid','label':'Service/New'},
]

class Req:
    def __init__(self, fn): self.fn=fn
    def execute(self): return self.fn()

class FakeLabels:
    def __init__(self,parent): self.p=parent
    def list(self,userId): return Req(lambda:{'labels':[{'id':k,'name':v,'type':'user'} for k,v in self.p.id_to_label.items()]})
    def create(self,userId,body):
        def act():
            i=f'Label_{len(self.p.id_to_label)+1}'
            self.p.id_to_label[i]=body['name']
            return {'id':i,'name':body['name']}
        return Req(act)
class FakeFilters:
    def __init__(self,parent): self.p=parent
    def list(self,userId): return Req(lambda:{'filter':list(self.p.filters.values())})
    def create(self,userId,body):
        def act():
            self.p.n+=1
            i=f'filter_{self.p.n}'
            self.p.filters[i]={'id':i,**body}
            return {'id':i}
        return Req(act)
    def delete(self,userId,id):
        def act(): del self.p.filters[id]; return {}
        return Req(act)
class FakeUsers:
    def __init__(self,parent): self.p=parent
    def labels(self): return FakeLabels(self.p)
    def settings(self): return self
    def filters(self): return FakeFilters(self.p)
class FakeClient:
    def __init__(self,templates):
        names=sorted(set(v[6:] for t in templates for v in t['action']['addLabelIds'] if v.startswith('label:')))
        self.id_to_label={f'Label_{i+1}':v for i,v in enumerate(names)}
        ids={v:k for k,v in self.id_to_label.items()}
        self.filters={f'filter_{i+1}':{'id':f'filter_{i+1}',**d.to_api(t,ids)} for i,t in enumerate(templates)}
        self.n=len(templates)
    def users(self): return FakeUsers(self)

class TestDeployer(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)/'base.xml'
        self.target=Path(self.tmp.name)/'target.xml'
        write_xml(self.base,BASE)
        write_xml(self.target,TARGET)
        self.b=d.compile_xml(self.base)
        self.t=d.compile_xml(self.target)

    def test_select_latest_master_without_renaming(self):
        masters=Path(self.tmp.name)/'masters'
        masters.mkdir()
        write_xml(masters/'mailFilters_v0.13.0_proposed_master_20261012.xml',BASE)
        new=masters/'mailFilters_v0.14.0_proposed_master_20261019.xml'
        write_xml(new,TARGET)
        write_xml(masters/'mailFilters_v9.99.0_incremental_master_20990101.xml',BASE)
        write_xml(masters/'mailFilters_v9.99.0_patch_20990101.xml',BASE)
        self.assertEqual(d.select_master(Path(self.tmp.name)),new)
        self.assertEqual(d.select_master(Path(self.tmp.name), self.base), self.base)
        self.assertEqual(len(d.compile_xml(d.select_master(Path(self.tmp.name)))),5)

    def test_same_version_and_date_requires_explicit_selection(self):
        masters=Path(self.tmp.name)/'masters'
        masters.mkdir()
        write_xml(masters/'mailFilters_v0.14.0_proposed_master_20261019.xml',BASE)
        write_xml(masters/'mailFilters_v0.14.0_revised_master_20261019.xml',TARGET)
        with self.assertRaises(d.DeploymentError):
            d.select_master(Path(self.tmp.name))

    def test_live_baseline_plan_requires_no_legacy_file(self):
        client=FakeClient(self.b)
        live,labels=d.api_fetch(client)
        plan=d.make_plan(None,self.target,[x['filter'] for x in live], 'test@example.invalid', labels)
        self.assertEqual(plan['schema'],'gmail-filters-deploy-plan/v3')
        self.assertIsNone(plan['baseline_path'])
        self.assertEqual((len(plan['add']),len(plan['remove'])),(2,1))
        self.assertEqual(d.counter(d.planned_states(plan)[0]),d.counter(self.b))
        log=Path(self.tmp.name)/'live-log.json'
        d.perform(client,plan,'test@example.invalid',plan['approval_digest'],log,batch_size=1)
        d.perform(client,plan,'test@example.invalid',plan['approval_digest'],log,batch_size=1)
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(client)[0]]),d.counter(self.t))

    def test_live_baseline_master_updates_delete_and_rejects_unknown_state(self):
        extra=[{'from':'other@example.invalid','label':'Action/Delete'}]
        live=self.b+d.compile_xml(self._write_extra(extra))
        plan=d.make_plan(None,self.target,live,'test@example.invalid',{})
        self.assertEqual(plan['current_delete_count'],2)
        self.assertEqual(plan['target_delete_count'],1)
        self.assertEqual(sum(d.is_delete(x) for x in plan['remove']),1)
        self.assertEqual(sum(d.is_delete(x) for x in plan['add']),0)
        altered=self.b[1:]+[{'criteria':{'from':'unrelated@example.invalid'},
                              'action':{'addLabelIds':['label:Service/X'],'removeLabelIds':[]}}]
        with self.assertRaises(d.DeploymentError):
            d.detect_position(d.planned_states(plan)[0],plan,altered)

    def _write_extra(self, rows):
        out=Path(self.tmp.name)/'extra.xml'
        write_xml(out, rows)
        return out

    def test_xml_compilation_and_diff(self):
        self.assertEqual((len(self.b),len(self.t)),(4,5))
        add,remove=d.delta(self.b,self.t)
        self.assertEqual((len(add),len(remove)),(2,1))
        d.protect(self.b,self.t)

    def test_compound_from_query(self):
        f=d.build_template({'from':'a@example.invalid OR b@example.invalid','subject':'{one two}','label':'Type/Account'})
        self.assertEqual(f['criteria']['query'],'{from:a@example.invalid from:b@example.invalid} subject:{one two}')

    def test_reject_unsafe_delete(self):
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{'from':'a@example.invalid','label':'Action/Delete','shouldTrash':'true'}])
        changed=[dict(x) for x in self.b]
        changed[2]['criteria']={'from':'changed@example.invalid'}
        d.protect(self.b, changed)  # Master may change Action/Delete conditions.
        with self.assertRaises(d.DeploymentError):
            d.validate_delete_label_only([{'criteria':{'from':'x'},'action':
                {'addLabelIds':['label:Action/Delete'],'removeLabelIds':['UNREAD']}}])

    def test_unsupported_from(self):
        with self.assertRaises(d.DeploymentError):
            d.build_template({'from':'a@example.invalid OR (b@example.invalid AND c@example.invalid)','label':'Type/Account'})

    def test_unknown_live_state_rejected(self):
        with self.assertRaises(d.DeploymentError):
            d.make_plan(self.base,self.target,self.b[:-1],'test@example.invalid',{})

    def test_plan_and_resume(self):
        labels={v[6:]:'id' for f in self.b for v in f['action']['addLabelIds'] if v.startswith('label:')}
        plan=d.make_plan(self.base,self.target,self.b,'test@example.invalid',labels)
        self.assertEqual((len(plan['add']),len(plan['remove'])),(2,1))
        self.assertEqual(d.detect_position(self.b,plan,self.b),(0,0))
        self.assertEqual(d.detect_position(self.b,plan,self.t),(2,1))
        self.assertEqual(d.detect_position(self.b,plan,self.b+plan['add'][:1]),(1,0))
        with self.assertRaises(d.DeploymentError):
            d.detect_position(self.b,plan,self.b+[{'criteria':{'from':'x'},'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}])

    def test_full_mock_and_idempotent(self):
        client=FakeClient(self.b)
        live,labels=d.api_fetch(client)
        plan=d.make_plan(self.base,self.target,[x['filter'] for x in live],'test@example.invalid',labels)
        log=Path(self.tmp.name)/'log.json'
        d.perform(client,plan,'test@example.invalid',plan['approval_digest'],log,batch_size=1)
        d.perform(client,plan,'test@example.invalid',plan['approval_digest'],log,batch_size=1)
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(client)[0]]),d.counter(self.t))
        self.assertTrue(json.loads(log.read_text())['verified'])

class TestExpectedEmail(unittest.TestCase):
    def test_environment_value_is_default(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': ' User@Example.Com '}, clear=True):
            self.assertEqual(d.resolve_expected_email(None), 'user@example.com')

    def test_cli_flag_overrides_environment(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': 'other@example.com'}, clear=True):
            self.assertEqual(d.resolve_expected_email(' Selected@Example.com '), 'selected@example.com')

    def test_missing_account_fails_closed(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(d.DeploymentError, 'GMAIL_EXPECTED_EMAIL'):
                d.resolve_expected_email(None)
            self.assertEqual(d.main(['plan']), 2)

    def test_invalid_account_fails_closed(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': 'not-an-email'}, clear=True):
            with self.assertRaises(d.DeploymentError):
                d.resolve_expected_email(None)

    def test_cli_plan_uses_environment_without_flag(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': 'account@example.com'}, clear=True):
            with patch.object(d, 'cmd_plan') as fake_plan:
                self.assertEqual(d.main(['plan']), 0)
                self.assertEqual(fake_plan.call_args.args[0].expected_email, 'account@example.com')

    def test_cli_flag_overrides_environment_at_dispatch(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': 'wrong@example.com'}, clear=True):
            for command in ('plan', 'verify', 'apply'):
                with self.subTest(command=command):
                    handler={'plan':'cmd_plan','verify':'cmd_verify','apply':'cmd_apply'}[command]
                    argv=[command, '--expected-email','chosen@example.com']
                    if command=='apply':
                        argv.extend(['--approve','digest'])
                    with patch.object(d, handler) as fake:
                        self.assertEqual(d.main(argv),0)
                        self.assertEqual(fake.call_args.args[0].expected_email,'chosen@example.com')

    def test_all_gmail_commands_can_use_environment(self):
        with patch.dict('os.environ', {'GMAIL_EXPECTED_EMAIL': 'account@example.com'}, clear=True):
            for command in ('plan', 'apply', 'verify'):
                with self.subTest(command=command):
                    handler={'plan':'cmd_plan','apply':'cmd_apply','verify':'cmd_verify'}[command]
                    argv=[command]
                    if command=='apply': argv.extend(['--approve','digest'])
                    with patch.object(d, handler) as fake:
                        self.assertEqual(d.main(argv),0)
                        self.assertEqual(fake.call_args.args[0].expected_email,'account@example.com')


if __name__=='__main__': unittest.main()