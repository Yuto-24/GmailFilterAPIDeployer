"""Synthetic message API, durable crash recovery, and the independent approval boundary."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
import gmail_message_reclassifier as m
from test_deployer import FakeClient, FakeUsers, Req, write_xml

ACCOUNT = "demo@example.invalid"
RULES = [
    {"from": "a@example.invalid", "label": "Type/A", "shouldMarkAsRead": "true", "shouldStar": "true"},
    {"subject": '{sale "special offer"}', "label": "Type/B"},
    {"to": "recipient@example.invalid", "doesNotHaveTheWord": "subject:invoice", "label": "Action/Delete"},
    {"hasTheWord": "in:spam {phishing}", "shouldTrash": "true"},
]


class Labels:
    def __init__(self, client):
        self.c = client

    def list(self, **kwargs):
        return self.c.request("labels.list", lambda: {"labels": [
            {"id": lid, "name": name, "type": "user"} for lid, name in self.c.id_to_label.items()] +
            [{"id": lid, "name": lid, "type": "system"} for lid in self.c.systems]})


class Messages:
    def __init__(self, client):
        self.c = client

    def list(self, **kwargs):
        def result():
            query = kwargs.get("q")
            self.c.queries.append(query)
            ids = sorted(self.c.mail if query is None else self.c.hits.get(query, set()) & self.c.mail.keys())
            start = int(kwargs.get("pageToken", "0"))
            end = min(start + self.c.page_size, len(ids))
            page = {"messages": [{"id": mid, "threadId": self.c.mail[mid]["threadId"]} for mid in ids[start:end]]}
            if end < len(ids):
                page["nextPageToken"] = str(end)
            return page
        return self.c.request("messages.list", result)

    def get(self, **kwargs):
        assert kwargs["format"] == "minimal"
        assert kwargs["fields"] == "id,threadId,labelIds,historyId"
        return self.c.request("messages.get", lambda: deepcopy(self.c.mail[kwargs["id"]]))

    def batchModify(self, userId, body):
        def mutate():
            assert not (set(body["addLabelIds"]) | set(body["removeLabelIds"])) & self.c.systems
            self.c.writes.append(deepcopy(body))
            if self.c.on_write:
                self.c.on_write(body)
            limit = len(body["ids"]) if self.c.apply_count is None else self.c.apply_count
            for mid in body["ids"][:limit]:
                current = set(self.c.mail[mid]["labelIds"])
                self.c.mail[mid]["labelIds"] = sorted(current - set(body["removeLabelIds"]) | set(body["addLabelIds"]))
            if self.c.write_failure:
                raise self.c.write_failure
            return {}
        return self.c.request("messages.batchModify", mutate)


class History:
    def __init__(self, client):
        self.c = client

    def list(self, **kwargs):
        def result():
            if self.c.history_failure:
                raise self.c.history_failure
            start = int(kwargs.get("pageToken", "0"))
            events = self.c.history_events
            page = {"history": deepcopy(events[start:start + 2]), "historyId": "100"}
            if start + 2 < len(events):
                page["nextPageToken"] = str(start + 2)
            return page
        return self.c.request("history.list", result)


class Users(FakeUsers):
    def labels(self):
        return Labels(self.p)

    def messages(self):
        return Messages(self.p)

    def history(self):
        return History(self.p)

    def getProfile(self, **kwargs):
        return self.p.request("getProfile", lambda: {"emailAddress": ACCOUNT, "historyId": "1"})


class Client(FakeClient):
    def __init__(self, templates):
        super().__init__(templates)
        self.systems = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "SPAM", "TRASH", "CHAT"}
        self.id_to_label["Manual"] = "Manual/Private"
        self.mail, self.hits = {}, {}
        self.writes, self.queries, self.calls, self.history_events = [], [], [], []
        self.page_size = 2
        self.apply_count = self.write_failure = self.history_failure = self.on_write = self.on_request = None

    def users(self):
        return Users(self)

    def request(self, kind, fn):
        def call():
            self.calls.append(kind)
            if self.on_request:
                self.on_request(kind)
            return fn()
        return Req(call)

    def add(self, mid, labels=("INBOX", "UNREAD", "Manual"), thread="thread-1"):
        self.mail[mid] = {"id": mid, "threadId": thread, "labelIds": list(labels), "historyId": "1"}


class SearchTests(unittest.TestCase):
    def spec(self, props):
        return m.search_spec(d.build_template({**props, "label": "Type/Test"}))

    def test_native_criteria_have_message_search_mapping(self):
        result = self.spec({"from": "a@example.invalid OR b@example.invalid", "to": "r@example.invalid",
                            "subject": '{日本語 "special offer"}', "hasTheWord": 'label:old OR is:unread',
                            "doesNotHaveTheWord": 'subject:invoice OR "do not include"'})
        self.assertEqual(result, {"all": [["from:a@example.invalid", "from:b@example.invalid"],
            ["to:r@example.invalid", "cc:r@example.invalid", "bcc:r@example.invalid"],
            ["subject:日本語", 'subject:"special offer"'], ["(label:old OR is:unread)"]],
            "exclude": 'subject:invoice OR "do not include"'})

    def test_phrases_and_quoted_operators_are_preserved(self):
        self.assertEqual(m.native_query("subject", "meeting reminder"), 'subject:"meeting reminder"')
        self.assertEqual(m.native_query("from", "Example Sender"), 'from:"Example Sender"')
        self.assertEqual(m.native_query("subject", '"A OR B"'), 'subject:"A OR B"')

    def test_unsupported_native_forms_and_unbalanced_raw_queries_stop(self):
        for field, value in [("subject", '{a {b c}}'), ("subject", '"unclosed'),
                             ("from", 'a:subject:injection'), ("to", '-someone'),
                             ("query", '(unbalanced'), ("negatedQuery", 'a)'),
                             ("subject", '{a OR b}'), ("query", 'a\\b')]:
            with self.subTest(field=field, value=value), self.assertRaises(d.DeploymentError):
                m.search_spec({"criteria": {field: value}})

    def test_search_compilation_does_not_change_filter_compilation(self):
        t = d.build_template({"from": "a@example.invalid OR b@example.invalid", "subject": "{a b}", "label": "Type/A"})
        original = deepcopy(t)
        m.search_spec(t)
        self.assertEqual(t, original)
        self.assertIn("from", t["criteria"])
        self.assertIn("subject", t["criteria"])


class MessageTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(m.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "master.xml"
        write_xml(self.master, RULES)
        self.client = Client(d.compile_xml(self.master))
        self.ids = {name: lid for lid, name in self.client.id_to_label.items()}
        for mid in ("a", "b", "c", "d"):
            self.client.add(mid)
        self.client.add("sent", ("SENT", "Manual"))
        self.client.add("spam", ("SPAM", "Manual"))
        self.client.add("draft", ("DRAFT", "Manual"))
        self.client.add("trash", ("TRASH", "Manual"))
        self.client.hits = {"from:a@example.invalid": {"a", "b", "sent"},
                            "subject:sale": {"a", "sent"}, 'subject:"special offer"': {"a", "b"},
                            "to:recipient@example.invalid": {"c"}, "cc:recipient@example.invalid": {"d"},
                            "bcc:recipient@example.invalid": {"b"}, "subject:invoice": {"c"}}
        self.api = m.Gmail(self.client)
        self.out = StringIO()
        ctx = redirect_stdout(self.out)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def plan(self):
        directory = m.create_plan(self.api, self.local, self.master, ACCOUNT)
        plan = m.Plan(directory, ACCOUNT)
        self.addCleanup(plan.close)
        return plan

    def execute(self, plan, *, answer=True, tty=True, **kwargs):
        with patch.object(d, "confirm_interactive", return_value=answer), patch("sys.stdin.isatty", return_value=tty):
            return m.execute(self.api, plan, self.local, **kwargs)

    def user_set(self, mid):
        return set(self.client.mail[mid]["labelIds"]) & self.client.id_to_label.keys()

    def test_plan_intersection_union_exclusion_and_message_scope(self):
        before = deepcopy(self.client.mail)
        plan = self.plan()
        self.assertEqual(plan.manifest["summary"], {"target": 4, "changed": 4, "unchanged": 0,
            "unmatched": 1, "multiple": 2, "excluded": 4, "new_arrivals": 0, "skipped_rules": 1, "unsupported": 0})
        expected = {"a": {self.ids["Type/A"], self.ids["Type/B"]},
                    "b": {self.ids["Type/A"], self.ids["Type/B"], self.ids["Action/Delete"]},
                    "c": set(), "d": {self.ids["Action/Delete"]}}
        for mid, labels in expected.items():
            self.assertEqual(plan.desired(plan.row(mid)), labels)
        self.assertEqual(self.client.mail, before)
        self.assertEqual(self.client.writes, [])
        self.assertEqual((plan.directory / "plan.sqlite").stat().st_mode & 0o777, 0o400)
        self.assertFalse((self.local / m.PENDING).exists())

    def test_apply_and_restore_preserve_systems_and_out_of_scope(self):
        before = deepcopy(self.client.mail)
        plan = self.plan()
        self.assertTrue(self.execute(plan))
        for mid in before:
            if mid in {"sent", "spam", "draft", "trash"}:
                self.assertEqual(self.client.mail[mid], before[mid])
            else:
                self.assertEqual(set(self.client.mail[mid]["labelIds"]) & self.client.systems,
                                 set(before[mid]["labelIds"]) & self.client.systems)
        self.assertFalse((self.local / m.PENDING).exists())
        self.client.mail["a"]["labelIds"].remove("UNREAD")  # Reading mail later is preserved by restore.
        self.assertTrue(self.execute(plan, mode="restore"))
        for mid in before:
            self.assertEqual(self.user_set(mid), set(before[mid]["labelIds"]) & self.client.id_to_label.keys())
        self.assertNotIn("UNREAD", self.client.mail["a"]["labelIds"])

    def test_refusal_and_noninteractive_never_write_or_claim_ownership(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, answer=False))
        self.assertFalse(self.execute(plan, tty=False))
        self.assertEqual(self.client.writes, [])
        self.assertFalse((plan.directory / "progress.sqlite").exists())

    def test_changed_during_final_prompt_stops_before_write(self):
        plan = self.plan()
        def approve(prompt):
            self.client.mail["a"]["labelIds"].remove("UNREAD")
            return True
        with patch.object(d, "confirm_interactive", side_effect=approve), patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(d.DeploymentError):
                m.execute(self.api, plan, self.local)
        self.assertFalse(self.client.writes)

    def test_partial_failure_resumes_only_unapplied_owned_messages(self):
        self.client.hits = {"from:a@example.invalid": {"a", "b", "c", "d"}}
        plan = self.plan()
        self.client.apply_count, self.client.write_failure = 2, TimeoutError("response lost")
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.client.writes[0]["ids"], ["a", "b", "c", "d"])
        self.client.apply_count = self.client.write_failure = None
        self.client.hits.clear()  # Resume must never rerun searches.
        query_count = len(self.client.queries)
        self.assertTrue(self.execute(plan))
        self.assertEqual(self.client.writes[-1]["ids"], ["c", "d"])
        self.assertEqual(len(self.client.queries), query_count)

    def test_lost_success_response_does_not_send_twice(self):
        plan = self.plan()
        self.client.write_failure = TimeoutError("lost")
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        done = set(self.client.writes[0]["ids"])
        self.client.write_failure = None
        self.assertTrue(self.execute(plan))
        self.assertTrue(all(not done.intersection(w["ids"]) for w in self.client.writes[1:]))

    def test_readback_failure_retains_intent_and_recovers(self):
        plan = self.plan()
        def on_write(body):
            def hook(kind):
                if kind == "messages.get":
                    raise RuntimeError("readback unavailable")
            self.client.on_request = hook
        self.client.on_write = on_write
        with self.assertRaises(RuntimeError):
            self.execute(plan)
        self.assertTrue((self.local / m.PENDING).exists())
        self.client.on_write = self.client.on_request = None
        already = set(self.client.writes[0]["ids"])
        self.assertTrue(self.execute(plan))
        self.assertTrue(all(not already.intersection(w["ids"]) for w in self.client.writes[1:]))

    def test_unowned_target_state_is_not_accepted_as_our_write(self):
        plan = self.plan()
        self.client.mail["a"]["labelIds"] = sorted(plan.desired(plan.row("a")) | {"INBOX", "UNREAD"})
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertFalse(self.client.writes)

    def test_restore_partial_plan_and_resume_interrupted_restore(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=2))
        applied = {mid for w in self.client.writes for mid in w["ids"]}
        self.assertFalse(self.execute(plan, mode="restore", max_batches=1))
        restored = set(self.client.writes[-1]["ids"])
        calls = len(self.client.writes)
        self.assertTrue(self.execute(plan, mode="restore"))
        self.assertTrue(all(not restored.intersection(w["ids"]) for w in self.client.writes[calls:]))
        self.assertEqual(set().union(*(set(w["ids"]) for w in self.client.writes[2:])), applied)
        for mid in ("a", "b", "c", "d"):
            self.assertEqual(self.user_set(mid), {"Manual"})

    def test_restore_conflict_or_missing_label_never_overwrites(self):
        plan = self.plan()
        self.execute(plan)
        self.client.mail["a"]["labelIds"].append("Manual")
        count = len(self.client.writes)
        with self.assertRaises(d.DeploymentError):
            self.execute(plan, mode="restore")
        self.assertEqual(len(self.client.writes), count)
        self.client.mail["a"]["labelIds"].remove("Manual")
        del self.client.id_to_label["Manual"]
        with self.assertRaises(d.DeploymentError):
            self.execute(plan, mode="restore")
        self.assertEqual(len(self.client.writes), count)

    def test_snapshot_change_and_history_expiry_prevent_complete_plan(self):
        self.client.history_events = [{"labelsAdded": [{"message": {"id": "a"}, "labelIds": ["STARRED"]}]}]
        with self.assertRaises(d.DeploymentError):
            self.plan()
        self.assertFalse(list(self.local.glob("reclassification/*/manifest.json")))
        self.client.history_events = []
        self.client.history_failure = RuntimeError("history 404")
        with self.assertRaises(RuntimeError):
            self.plan()
        self.assertFalse(self.client.writes)

    def test_new_mail_during_enumeration_is_deferred(self):
        def hook(kind):
            if kind == "messages.list" and "new" not in self.client.mail:
                self.client.add("new")
                self.client.history_events.append({"messagesAdded": [{"message": {"id": "new"}}]})
        self.client.on_request = hook
        plan = self.plan()
        self.assertEqual(plan.manifest["summary"]["new_arrivals"], 1)
        self.assertEqual(plan.manifest["summary"]["target"], 4)
        self.execute(plan)
        self.assertEqual(self.user_set("new"), {"Manual"})

    def test_backup_corruption_and_account_mismatch_fail(self):
        plan = self.plan()
        with self.assertRaises(d.DeploymentError):
            m.Plan(plan.directory, "other@example.invalid")
        copied = plan.directory / "master.xml"
        copied.chmod(0o600)
        copied.write_text("tampered")
        with self.assertRaises(d.DeploymentError):
            m.Plan(plan.directory, ACCOUNT)
        self.assertFalse(self.client.writes)

    def test_resume_uses_frozen_master_after_source_changes(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=1))
        self.master.write_text("invalid newer master")
        self.assertTrue(self.execute(plan))

    def test_incomplete_filter_deploy_and_filter_mismatch_block(self):
        (self.local / "run_pending.json").write_text("{}")
        with self.assertRaises(d.DeploymentError):
            self.plan()
        (self.local / "run_pending.json").unlink()
        self.client.filters.pop(next(iter(self.client.filters)))
        with self.assertRaises(d.DeploymentError):
            self.plan()

    def test_large_plan_default_fifty_and_last_partial_batch(self):
        self.client.mail.clear()
        self.client.page_size = 500
        ids = {f"m{i:04}" for i in range(1003)}
        for mid in ids:
            self.client.add(mid)
        self.client.hits = {"from:a@example.invalid": ids}
        plan = self.plan()
        self.assertTrue(self.execute(plan))
        self.assertEqual([len(w["ids"]) for w in self.client.writes], [50] * 20 + [3])

    def test_queries_are_cached_and_unchanged_labels_are_not_removed(self):
        self.client.mail["a"]["labelIds"].append(self.ids["Type/A"])
        plan = self.plan()
        self.execute(plan)
        write = next(w for w in self.client.writes if "a" in w["ids"])
        self.assertNotIn(self.ids["Type/A"], write["removeLabelIds"])
        self.assertNotIn(self.ids["Type/A"], write["addLabelIds"])

    def test_batch_intent_exists_before_http_and_is_bound_to_plan(self):
        plan = self.plan()
        def hook(body):
            db = sqlite3.connect(plan.directory / "progress.sqlite")
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='digest'").fetchone()[0], plan.manifest["digest"])
            for mid in body["ids"]:
                self.assertEqual(db.execute("SELECT status FROM progress WHERE message=?", (mid,)).fetchone()[0], "pending")
            self.assertTrue(db.execute("SELECT count(*) FROM intents").fetchone()[0])
            db.close()
        self.client.on_write = hook
        self.execute(plan)

    def test_crash_before_send_and_process_interrupt_after_intent(self):
        plan = self.plan()
        self.client.apply_count = 0
        self.client.write_failure = KeyboardInterrupt()
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)
        for mid in ("a", "b", "c", "d"):
            self.assertEqual(self.user_set(mid), {"Manual"})
        self.client.apply_count = self.client.write_failure = None
        self.assertTrue(self.execute(plan))

    def test_write_429_and_500_stop_without_automatic_resend(self):
        plan = self.plan()
        for status in (429, 500):
            failure = RuntimeError("synthetic API failure")
            failure.resp = SimpleNamespace(status=status)
            self.client.apply_count, self.client.write_failure = 0, failure
            count = len(self.client.writes)
            with self.assertRaises(d.DeploymentError):
                self.execute(plan)
            self.assertEqual(len(self.client.writes), count + 1)
        self.client.apply_count = self.client.write_failure = None
        self.assertTrue(self.execute(plan))

    def test_missing_mail_and_label_definition_changes_fail_closed(self):
        plan = self.plan()
        original = self.client.mail.pop("a")
        with self.assertRaises(KeyError):
            self.execute(plan)
        self.client.mail["a"] = original
        self.client.id_to_label["Manual"] = "Renamed"
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertFalse(self.client.writes)

    def test_unrelated_new_label_does_not_block_resume_or_restore(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=1))
        self.client.id_to_label["NewLabel"] = "Unrelated/New"
        self.client.add("unrelated", ("SENT", "NewLabel"))
        self.assertTrue(self.execute(plan))
        self.assertTrue(self.execute(plan, mode="restore"))
        for mid in ("a", "b", "c", "d"):
            self.assertEqual(self.user_set(mid), {"Manual"})
        self.assertEqual(self.user_set("unrelated"), {"NewLabel"})
        self.assertNotIn("NewLabel", {lid for body in self.client.writes
                                      for lid in body["addLabelIds"] + body["removeLabelIds"]})

    def test_new_label_attached_to_target_blocks_apply_restore_and_readback(self):
        plan = self.plan()
        self.client.id_to_label["NewLabel"] = "Unrelated/New"
        self.client.mail["a"]["labelIds"].append("NewLabel")
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertFalse(self.client.writes)
        self.client.mail["a"]["labelIds"].remove("NewLabel")
        self.assertTrue(self.execute(plan))
        self.client.mail["a"]["labelIds"].append("NewLabel")
        before = len(self.client.writes)
        with self.assertRaises(d.DeploymentError):
            self.execute(plan, mode="restore")
        self.assertEqual(len(self.client.writes), before)
        self.client.mail["a"]["labelIds"].remove("NewLabel")
        def attach_during_send(body):
            self.client.mail[body["ids"][0]]["labelIds"].append("NewLabel")
        self.client.on_write = attach_during_send
        with self.assertRaises(d.DeploymentError):
            self.execute(plan, mode="restore")
        self.assertEqual(len(self.client.writes), before + 1)
        self.assertTrue((self.local / m.PENDING).exists())

    def test_any_new_definition_during_planning_still_invalidates_search_snapshot(self):
        changed = False
        def new_label(kind):
            nonlocal changed
            if kind == "messages.get" and not changed:
                self.client.id_to_label["NewLabel"] = "Unrelated/New"
                changed = True
        self.client.on_request = new_label
        with self.assertRaises(d.DeploymentError):
            self.plan()
        self.assertFalse(self.client.writes)

    def test_label_dependent_search_is_frozen_before_any_mutation(self):
        write_xml(self.master, [{"hasTheWord": "label:Manual/Private", "label": "Type/A"}])
        self.client = Client(d.compile_xml(self.master))
        self.client.add("a")
        self.client.hits = {"(label:Manual/Private)": {"a"}}
        self.api = m.Gmail(self.client)
        plan = self.plan()
        self.client.hits = {}  # Saved match results, not a sequential rule evaluation.
        self.assertTrue(self.execute(plan))
        self.assertEqual(self.user_set("a"), {lid for lid, name in self.client.id_to_label.items() if name == "Type/A"})

    def test_multifield_and_intersects_and_empty_result_does_not_mean_skipped(self):
        write_xml(self.master, [{"from": "a@example.invalid", "subject": "sale", "label": "Type/A"}])
        self.client = Client(d.compile_xml(self.master))
        for mid in ("a", "b", "c"):
            self.client.add(mid)
        self.client.hits = {"from:a@example.invalid": {"a", "b"}, "subject:sale": {"a", "c"}}
        self.api = m.Gmail(self.client)
        plan = self.plan()
        self.assertEqual(plan.manifest["summary"]["unmatched"], 2)
        self.assertTrue(self.execute(plan))
        self.assertTrue(self.user_set("a"))
        self.assertEqual(self.user_set("b"), set())
        self.assertEqual(self.user_set("c"), set())

    def test_same_query_is_executed_once_across_rules(self):
        write_xml(self.master, [{"from": "a@example.invalid", "label": "Type/A"},
                               {"from": "a@example.invalid", "label": "Type/B"}])
        self.client = Client(d.compile_xml(self.master))
        self.client.add("a")
        self.client.hits = {"from:a@example.invalid": {"a"}}
        self.api = m.Gmail(self.client)
        self.plan()
        self.assertEqual(self.client.queries.count("from:a@example.invalid"), 1)

    def test_restoration_refusal_preserves_previous_verified_record(self):
        plan = self.plan()
        self.execute(plan)
        self.assertFalse(self.execute(plan, mode="restore", answer=False))
        journal = m.Journal(plan)
        try:
            self.assertEqual(journal.meta("complete"), "true")
            self.assertEqual(journal.meta("mode"), "apply")
        finally:
            journal.close()

    def test_unexpected_within_message_partial_state_stays_unknown(self):
        plan = self.plan()
        def hook(body):
            # The HTTP call fails after an unexplained label-only partial result.
            mid = body["ids"][0]
            self.client.mail[mid]["labelIds"].append(self.ids["Type/A"])
            raise TimeoutError("unknown partial state")
        # Choose a message which expects removal only, so adding Type/A is a third state.
        self.client.on_write = hook
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), 1)

    def test_ownership_tampering_is_detected(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=1))
        db = sqlite3.connect(plan.directory / "progress.sqlite")
        db.execute("DELETE FROM intents")
        db.commit()
        db.close()
        with self.assertRaises(d.DeploymentError):
            m.Journal(plan)

    def test_backup_corruption_after_open_and_during_approval_blocks_sends(self):
        for filename, during_approval in [("plan.sqlite", False), ("master.xml", True), ("manifest.json", True)]:
            with self.subTest(filename=filename):
                plan = self.plan()
                path = plan.directory / filename
                def corrupt(*args):
                    path.chmod(0o600)
                    path.write_bytes(b"corrupt")
                    return True
                if not during_approval:
                    corrupt()
                with patch("sys.stdin.isatty", return_value=True), \
                        patch.object(d, "confirm_interactive", side_effect=corrupt), \
                        self.assertRaises((d.DeploymentError, ValueError)):
                    m.execute(self.api, plan, self.local)
                self.assertFalse(self.client.writes)

    def test_crash_after_send_before_progress_commit_resumes_without_resending(self):
        plan = self.plan()
        original = m.Journal.set_state
        def crash(journal, mid, status):
            if status == "applied":
                raise KeyboardInterrupt()
            return original(journal, mid, status)
        with patch.object(m.Journal, "set_state", crash), self.assertRaises(KeyboardInterrupt):
            self.execute(plan)
        first_ids = set(self.client.writes[0]["ids"])
        self.assertTrue(self.execute(plan))
        self.assertFalse(first_ids & {mid for body in self.client.writes[1:] for mid in body["ids"]})

    def test_noninteractive_update_commands_do_not_authenticate_or_prepare(self):
        with patch("sys.stdin.isatty", return_value=False), \
                patch.object(m, "message_client", side_effect=AssertionError("OAuth")):
            for command in (["reclassify"], ["restore-labels", "--run-id", "a" * 32]):
                self.assertEqual(d.main(command + ["--local-dir", str(self.local), "--expected-email", ACCOUNT]), 0)
        self.assertFalse((self.local / "reclassification").exists())

    def test_pending_digest_mismatch_blocks_resume(self):
        plan = self.plan()
        self.assertFalse(self.execute(plan, max_batches=1))
        active = m.pending(self.local)
        active["digest"] = "invalid"
        m.save(self.local / m.PENDING, active)
        before = len(self.client.writes)
        with self.assertRaises(d.DeploymentError):
            self.execute(plan)
        self.assertEqual(len(self.client.writes), before)

    def test_cli_plan_only_and_pending_run_bypass_filter_stage(self):
        with patch.object(m, "message_client", return_value=self.api):
            self.assertEqual(d.main(["reclassify", "--plan-only", "--target", str(self.master),
                                    "--local-dir", str(self.local), "--expected-email", ACCOUNT]), 0)
        directory = next(self.local.glob("reclassification/*"))
        plan = m.Plan(directory, ACCOUNT)
        self.addCleanup(plan.close)
        self.assertFalse(self.execute(plan, max_batches=1))
        with patch.object(m, "message_client", return_value=self.api), patch.object(d, "get_client", side_effect=AssertionError("settings auth")), \
                patch("sys.stdin.isatty", return_value=True), patch.object(d, "confirm_interactive", return_value=True):
            self.assertEqual(d.main(["run", "--local-dir", str(self.local), "--expected-email", ACCOUNT]), 0)

    def test_noop_filter_run_offers_preparation_but_decline_has_no_message_auth(self):
        with patch.object(d, "get_client", return_value=(self.client, object())), \
                patch.object(d, "account_email", return_value=ACCOUNT), patch("sys.stdin.isatty", return_value=True), \
                patch.object(d, "confirm_interactive", return_value=False) as ask, \
                patch.object(m, "message_client", side_effect=AssertionError("unexpected message auth")):
            self.assertEqual(d.main(["run", "--target", str(self.master), "--local-dir", str(self.local), "--expected-email", ACCOUNT]), 0)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("再分類を準備", ask.call_args.args[0])

    def test_lock_blocks_all_mutating_entrypoints(self):
        with m.exclusive_lock(self.local), patch.object(d, "get_client", side_effect=AssertionError("OAuth")), redirect_stderr(StringIO()):
            for command in (["run"], ["reclassify", "--plan-only"], ["restore-labels", "--run-id", "a" * 32],
                            ["apply", "--approve", "unused"]):
                self.assertEqual(d.main(command + ["--local-dir", str(self.local), "--expected-email", ACCOUNT]), 2)


try:
    import googleapiclient.discovery
    import google_auth_oauthlib.flow
    import google.oauth2.credentials
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


@unittest.skipUnless(SDK_AVAILABLE, "Google SDK required")
class MessageOAuthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.token = self.local / "message_token.json"
        self.token.write_text('original token bytes')
        self.secret = self.local / "credentials.json"
        self.secret.write_text('{}')
        self.scopes = d.SCOPES + ["https://www.googleapis.com/auth/gmail.modify"]

    def creds(self, scopes=None, *, granted=None, valid=True):
        scopes = self.scopes if scopes is None else scopes
        return SimpleNamespace(valid=valid, expired=False, refresh_token=None, scopes=scopes,
            granted_scopes=granted, has_scopes=lambda required: set(required) <= set(scopes),
            to_json=lambda: '{"token":"synthetic-only","scopes":' + json.dumps(scopes) + '}')

    def call(self, existing, replacement=None, *, email=ACCOUNT, allow_auth=True):
        with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=existing), \
                patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as flow, \
                patch.object(d, "account_email", return_value=email), patch.object(d, "SingleAttemptHttp"), \
                patch("googleapiclient.discovery.build", return_value=object()):
            flow.return_value.run_local_server.return_value = replacement
            return d.get_client(self.secret, self.token, allow_auth=allow_auth, scopes=self.scopes, expected_email=ACCOUNT)

    def test_valid_old_scope_token_reauthenticates_and_atomically_replaces_only_on_success(self):
        self.call(self.creds(d.SCOPES), self.creds())
        self.assertEqual(json.loads(self.token.read_text())["token"], "synthetic-only")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)

    def test_refused_scope_or_wrong_account_preserves_original_file(self):
        for replacement, email in [(self.creds(granted=d.SCOPES), ACCOUNT), (self.creds(), "wrong@example.invalid")]:
            with self.subTest(email=email), self.assertRaises(d.DeploymentError):
                self.call(self.creds(d.SCOPES), replacement, email=email)
            self.assertEqual(self.token.read_text(), 'original token bytes')

    def test_missing_scopes_in_noninteractive_mode_preserves_original_file(self):
        with self.assertRaises(d.DeploymentError):
            self.call(self.creds(d.SCOPES), allow_auth=False)
        self.assertEqual(self.token.read_text(), 'original token bytes')

    def test_existing_scoped_token_still_checks_account(self):
        with self.assertRaises(d.DeploymentError):
            self.call(self.creds(), email="wrong@example.invalid")
        self.assertEqual(self.token.read_text(), 'original token bytes')

    def test_real_discovery_builds_minimal_reads_and_single_batch_request_offline(self):
        import httplib2
        from urllib.parse import parse_qs, urlparse
        class Http:
            def __init__(self):
                self.requests = []

            def request(self, uri, method="GET", body=None, **kwargs):
                self.requests.append((uri, method, body))
                payload = {"id": "a", "threadId": "thread", "labelIds": ["INBOX"]} if method == "GET" else {}
                return httplib2.Response({"status": "200"}), json.dumps(payload).encode()
        http = Http()
        client = googleapiclient.discovery.build("gmail", "v1", http=http, static_discovery=True)
        api = m.Gmail(client)
        self.assertEqual(api.message("a", {"INBOX": {"type": "system", "name": "INBOX"}})["id"], "a")
        api.modify(["a"], ["Label_new"], ["Label_old"])
        self.assertEqual(len(http.requests), 2)
        self.assertEqual(parse_qs(urlparse(http.requests[0][0]).query)["format"], ["minimal"])
        uri, method, body = http.requests[1]
        self.assertTrue(urlparse(uri).path.endswith("/messages/batchModify"), uri)
        self.assertEqual(method, "POST")
        self.assertEqual(json.loads(body), {"ids": ["a"], "addLabelIds": ["Label_new"], "removeLabelIds": ["Label_old"]})


if __name__ == "__main__":
    unittest.main()
