"""Only known, unchanged message metadata can become writable plan rows."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
import test_messages as fixtures


class UnknownMessageLabelsTests(unittest.TestCase):
    setUp = fixtures.MessageTests.setUp
    execute = fixtures.MessageTests.execute

    def plan(self):
        self.client.mail['a']['labelIds'].append('Label_missing')
        directory = m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT,
                                  skip_unknown_labels=True)
        result = m.Plan(directory, fixtures.ACCOUNT)
        self.addCleanup(result.close)
        return result

    def test_default_stops_before_any_write(self):
        self.client.mail['a']['labelIds'].append('Label_missing')
        with self.assertRaises(m.UnknownMessageLabels):
            m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT)
        self.assertEqual(self.client.writes, [])
        self.assertFalse(list((self.local/'reclassification').glob('*/manifest.json')))

    def test_deferred_metadata_is_hashed_and_never_sent_or_reported_complete(self):
        original = deepcopy(self.client.mail['a'])
        plan = self.plan()
        self.assertEqual(plan.manifest['schema'], m.DEFERRED_SCHEMA)
        self.assertEqual(plan.deferred_count, 1)
        self.assertEqual(plan.manifest['summary']['target'], 3)
        row = plan.db.execute('SELECT * FROM deferred').fetchone()
        self.assertEqual(json.loads(row['metadata']), {**original, 'labelIds':original['labelIds']+['Label_missing']})
        self.assertFalse(self.execute(plan))
        self.assertTrue(self.client.writes)
        self.assertTrue(all('a' not in body['ids'] for body in self.client.writes))
        self.assertEqual(self.client.mail['a']['labelIds'], original['labelIds']+['Label_missing'])
        self.assertFalse((self.local/m.PENDING).exists())
        journal = m.Journal(plan)
        try:
            self.assertEqual(journal.meta('complete'), 'false')
            self.assertEqual(journal.meta('eligible_verified'), 'true')
        finally:
            journal.close()
        before = len(self.client.writes)
        self.assertFalse(self.execute(plan))
        self.assertEqual(len(self.client.writes), before)

    def test_invalid_id_thread_or_duplicate_label_cannot_be_deferred(self):
        for field, value in [('id','wrong'), ('threadId',None), ('labelIds',['Manual','Manual']),
                             ('labelIds',[None]), ('labelIds',[''])]:
            with self.subTest(field=field, value=value):
                self.client.mail['a'][field] = value
                with self.assertRaises(d.DeploymentError) as caught:
                    m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT, skip_unknown_labels=True)
                self.assertNotIsInstance(caught.exception, m.UnknownMessageLabels)
                self.client.add('a')
        self.assertEqual(self.client.writes, [])

    def test_read_failure_is_not_silently_deferred(self):
        with patch.object(self.api, 'snapshots', side_effect=RuntimeError('read failed')):
            with self.assertRaisesRegex(RuntimeError, 'read failed'):
                m.create_plan(self.api, self.local, self.master, fixtures.ACCOUNT, skip_unknown_labels=True)
        self.assertEqual(self.client.writes, [])

    def test_unknown_label_added_after_snapshot_stops_before_writes(self):
        plan = self.plan()
        self.client.mail['b']['labelIds'].append('new_unknown')
        with self.assertRaises(m.UnknownMessageLabels):
            self.execute(plan)
        self.assertEqual(self.client.writes, [])

    def test_command_option_creates_partial_plan_and_exit_four(self):
        self.client.mail['a']['labelIds'].append('Label_missing')
        with patch('sys.stdin.isatty', return_value=True), \
             patch.object(m, 'message_client', return_value=self.api), \
             patch.object(d, 'confirm_interactive', return_value=True):
            result = d.main(['reclassify', '--target', str(self.master), '--local-dir', str(self.local),
                             '--expected-email', fixtures.ACCOUNT, '--skip-unknown-message-labels'])
        self.assertEqual(result, 4)
        self.assertTrue(self.client.writes)
        self.assertTrue(all('a' not in body['ids'] for body in self.client.writes))
