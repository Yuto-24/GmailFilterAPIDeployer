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
    def execute(self, num_retries=0):
        assert num_retries == 0
        return self.fn()

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
        """Plan from live state without a baseline XML and verify idempotent deployment."""
        client=FakeClient(self.b)
        live,labels=d.api_fetch(client)
        plan=d.make_plan(None,self.target,[x['filter'] for x in live], 'test@example.invalid', labels)
        self.assertEqual(plan['schema'],'gmail-filters-deploy-plan/v6')
        self.assertEqual(plan['normalization_version'],'query-v1')
        self.assertEqual(plan['compilation_version'],'xml-fields-v1')
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

    def test_compound_from_query(self):
        """Keep accepted native fields by default while retaining the legacy query compiler."""
        f=d.build_template({'from':'a@example.invalid OR b@example.invalid','subject':'{one two}','label':'Type/Account'})
        self.assertEqual(f['criteria'],{'from':'a@example.invalid OR b@example.invalid','subject':'{one two}'})
        legacy=d.build_template({'from':'a@example.invalid OR b@example.invalid','subject':'{one two}',
                                 'label':'Type/Account'}, compilation='legacy')
        self.assertEqual(legacy['criteria']['query'],'{from:a@example.invalid from:b@example.invalid} subject:{one two}')

    def test_reject_unsafe_delete(self):
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{'from':'a@example.invalid','label':'Action/Delete','shouldTrash':'true'}])
        changed=[dict(x) for x in self.b]
        changed[2]['criteria']={'from':'changed@example.invalid'}
        with self.assertRaises(d.DeploymentError):
            d.validate_delete_label_only([{'criteria':{'from':'x'},'action':
                {'addLabelIds':['label:Action/Delete'],'removeLabelIds':['UNREAD']}}])

    def test_unsupported_from(self):
        with self.assertRaises(d.DeploymentError):
            d.build_template({'from':'a@example.invalid OR (b@example.invalid AND c@example.invalid)','label':'Type/Account'})

    def test_unknown_live_state_rejected(self):
        with self.assertRaises(d.DeploymentError):
            d.make_plan(self.base,self.target,self.b[:-1],'test@example.invalid',{})

    def test_selected_target_preserves_declined_group(self):
        from pathlib import Path
        original = self.b + [{'criteria':{'from':'extra@example.invalid'},
                              'action':{'addLabelIds':['label:Action/Delete'],'removeLabelIds':[]}}]
        initial=d.make_plan(None,self.target,original,'test@example.invalid',{})
        subset=d.select_plan_groups(initial,['other'])
        self.assertEqual(subset['selected_groups'],['other'])
        base,target=d.planned_states(subset)
        self.assertEqual(d.counter(base),d.counter(original))
        self.assertTrue(any(x['criteria'].get('from')=='extra@example.invalid' for x in target))
        self.assertEqual(sum(d.is_delete(x) for x in target),2)
        self.assertEqual(sum(d.is_delete(x) for x in subset['add']),0)
        self.assertEqual(sum(d.is_delete(x) for x in subset['remove']),0)
        self.assertEqual(subset['target_size'],len(target))

    def test_ambiguous_deletion_is_preserved_and_other_updates_apply(self):
        duplicated=self.b[1]
        snapshot=self.b+[duplicated]
        client=FakeClient(snapshot)
        raw,labels=d.api_fetch(client)
        plan=d.make_plan(None,self.target,[x['filter'] for x in raw],
                         'test@example.invalid',labels)
        conflicts=d.deletion_preview(plan,raw)
        self.assertEqual(len(conflicts),2)
        self.assertTrue(all(x['reason']=='duplicate_matches' and x['matches']==2
                            for x in conflicts))
        selected=d.select_plan_groups(plan,['other'],current=raw)
        self.assertEqual(len(selected['skipped_deletions']),2)
        self.assertEqual(len(selected['remove']),0)
        base,effective=d.planned_states(selected)
        self.assertEqual(d.counter(base),d.counter(snapshot))
        self.assertEqual(d.counter(effective),d.counter(self.t+[duplicated,duplicated]))
        log=Path(self.tmp.name)/'skip-log.json'
        d.perform(client,selected,'test@example.invalid',
                  selected['approval_digest'],log,batch_size=1)
        self.assertEqual(d.counter([x['filter'] for x in d.api_fetch(client)[0]]),
                         d.counter(effective))

    def test_missing_filter_id_is_excluded_from_approved_deletions(self):
        client=FakeClient(self.b)
        original,_=d.api_fetch(client)
        for item in original:
            if item['filter']==self.b[1]:
                item['id']=None
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        conflicts=d.deletion_preview(plan,original)
        self.assertEqual(len(conflicts),1)
        self.assertEqual(conflicts[0]['reason'],'missing_filter_id')
        subset=d.select_plan_groups(plan,['other'],current=original)
        self.assertEqual(len(subset['remove']),0)
        self.assertEqual(d.counter(d.planned_states(subset)[1]),
                         d.counter(self.t+[self.b[1]]))

    def test_live_drift_during_preflight_is_detected(self):
        client=FakeClient(self.b)
        original,_=d.api_fetch(client)
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        extra={'criteria':{'from':'external@example.invalid'},
               'action':{'addLabelIds':['label:Type/Promotion'],'removeLabelIds':[]}}
        with self.assertRaises(d.DeploymentError):
            d.deletion_preview(plan,original+[{'id':'external','filter':extra}])

    def test_missing_delete_already_satisfied_reconciles_next_state(self):
        client=FakeClient(self.b)
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        log=Path(self.tmp.name)/'already-absent.json'
        # Already-absent work is recognized by the initial/resume snapshot.
        # There is intentionally no per-delete pre-read between checkpoints.
        rows,_=d.api_fetch(client)
        old=plan["remove"][0]
        fid=next(row["id"] for row in rows if row["filter"]==old)
        client.users().settings().filters().delete(userId='me',id=fid).execute()
        with patch.object(FakeFilters,'delete',side_effect=AssertionError('must not delete again')):
            d.perform(client,plan,'test@example.invalid',plan["approval_digest"],log,batch_size=1)
        self.assertEqual(d.counter([x["filter"] for x in d.api_fetch(client)[0]]),d.counter(self.t))
        self.assertTrue(json.loads(log.read_text())["verified"])

    def test_pre_final_check_catches_stale_journal_and_bad_batch(self):
        client=FakeClient(self.b)
        original,_=d.api_fetch(client)
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        log=Path(self.tmp.name)/'pre-final-log.json'
        with self.assertRaises(d.DeploymentError):
            d.validate_before_final_approval(plan,'test@example.invalid',log,0,original)
        log.write_text(json.dumps({'plan_digest':'other-plan'}))
        with self.assertRaises(d.DeploymentError):
            d.validate_before_final_approval(plan,'test@example.invalid',log,1,original)
        log.unlink()
        d.validate_before_final_approval(plan,'test@example.invalid',log,1,original)

    def test_pre_final_check_rejects_unexpected_deleted_filter_id(self):
        client=FakeClient(self.b)
        original,_=d.api_fetch(client)
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        for item in original:
            if item["filter"]==plan["remove"][0]:
                item["id"]=None
        with self.assertRaises(d.DeploymentError):
            d.validate_before_final_approval(plan,'test@example.invalid',
                Path(self.tmp.name)/'pre-final-log.json',1,original)

    def test_plan_and_resume(self):
        labels={v[6:]:'id' for f in self.b for v in f['action']['addLabelIds'] if v.startswith('label:')}
        plan=d.make_plan(self.base,self.target,self.b,'test@example.invalid',labels)
        self.assertEqual((len(plan['add']),len(plan['remove'])),(2,1))
        self.assertEqual(d.detect_position(self.b,plan,self.b),(0,0))
        self.assertEqual(d.detect_position(self.b,plan,self.t),(2,1))
        self.assertEqual(plan["operation_order"], "remove_first")
        after_first_remove=[row for row in self.b if row != plan["remove"][0]]
        self.assertEqual(d.detect_position(self.b,plan,after_first_remove),(0,1))
        after_all_remove=[row for row in self.b if row not in plan["remove"]]
        self.assertEqual(d.detect_position(self.b,plan,after_all_remove),(0,len(plan["remove"])))
        self.assertEqual(d.detect_position(self.b,plan,after_all_remove+plan["add"][:1]),(1,len(plan["remove"])))
        with self.assertRaises(d.DeploymentError):
            d.detect_position(self.b,plan,self.b+[{'criteria':{'from':'x'},'action':{'addLabelIds':['TRASH'],'removeLabelIds':[]}}])

    def test_trash_missing_in_live_is_planned_from_master(self):
        original=[x for x in self.b if not d.is_trash(x)]
        plan=d.make_plan(None,self.target,original,'test@example.invalid',{})
        self.assertEqual(plan['trash_add_count'],1)
        self.assertEqual(plan['trash_remove_count'],0)
        self.assertEqual(plan['target_trash_count'],1)
        self.assertEqual(len(plan['add']),3)
        self.assertEqual(len(plan['remove']),1)
        self.assertEqual(d.counter(d.planned_states(plan)[1]),d.counter(self.t))

    def test_extra_live_trash_removed_by_master(self):
        extra={'criteria':{'from':'unsolicited@example.invalid'},'action':
               {'addLabelIds':['TRASH'],'removeLabelIds':[]}}
        plan=d.make_plan(None,self.target,self.b+[extra],'test@example.invalid',{})
        self.assertEqual(plan['trash_remove_count'],1)
        self.assertIn(extra,plan['remove'])

    def test_846_to_930_plan_uses_delete_first_and_avoids_limit(self):
        common = [{"from":f"shared{i}@example.invalid", "label":"Type/Account"}
                  for i in range(589)]
        old = [{"from":f"old{i}@example.invalid", "label":"Type/Account"}
               for i in range(257)]
        new = [{"from":f"new{i}@example.invalid", "label":"Type/Account"}
               for i in range(341)]
        old_path=Path(self.tmp.name)/"old846.xml"
        master_path=Path(self.tmp.name)/"target930.xml"
        write_xml(old_path,common+old)
        write_xml(master_path,common+new)
        base=d.compile_xml(old_path)
        plan=d.make_plan(None,master_path,base,'test@example.invalid',{})
        self.assertEqual((len(base),len(plan['add']),len(plan['remove']),
                          plan['target_size']),(846,341,257,930))
        approved=d.select_plan_groups(plan,['other'])
        self.assertEqual(approved["operation_order"],"remove_first")
        self.assertEqual(d.counter(d.planned_states(approved)[1]),
                         d.counter(d.compile_xml(master_path)))
        removed=[row for row in base if row not in approved["remove"]]
        self.assertEqual(len(removed),589)
        self.assertEqual(d.detect_position(base,approved,removed),(0,257))
        self.assertEqual(d.detect_position(base,approved,removed+approved["add"][:100]),(100,257))
        self.assertEqual(max(len(base),len(removed)+len(approved["add"])),930)

    def test_legacy_add_first_plan_resume_is_not_reinterpreted(self):
        plan=d.make_plan(None,self.target,self.b,'test@example.invalid',{})
        del plan['operation_order']
        plan['approval_digest']=d.json_hash({
            k:v for k,v in plan.items() if k not in {'created_utc','approval_digest'}})
        self.assertEqual(d.detect_position(self.b,plan,self.b+plan['add'][:1]),(1,0))
        self.assertEqual(d.detect_position(self.b,plan,self.t),(2,1))

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
