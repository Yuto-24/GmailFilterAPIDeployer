"""Synthetic XML mapping, versioned audit boundaries, and frozen pre-change v5 resumes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d
from test_batch import BatchClient
from test_condition_equivalence import raw_client, rule
from test_deployer import FakeFilters, FakeLabels, write_xml

SENDERS = "a@example.invalid OR b@example.invalid"
QUERY_SENDERS = "{from:a@example.invalid from:b@example.invalid}"
SUBJECT = "{スカウトが届きました}"


class NativeCompilerTests(unittest.TestCase):
    def test_all_criteria_and_actions_keep_their_fields(self):
        """Retain accepted native fields, query wrappers, negation, and actions without mutating XML input."""
        properties = {"from": " " + SENDERS + " ", "subject": " " + SUBJECT + " ",
                      "to": "recipient@example.invalid", "hasTheWord": '(in:spam {phishing})',
                      "doesNotHaveTheWord": '"invoice  notice"', "label": "Type/Test",
                      "shouldMarkAsRead": "true", "shouldStar": "true"}
        original = deepcopy(properties)
        legacy = d.build_template(properties, compilation="legacy")
        native = d.build_template(properties, compilation="xml-fields-v1")
        self.assertEqual(properties, original)
        self.assertEqual(native["action"], legacy["action"])
        self.assertEqual(native["criteria"], {"from": SENDERS, "subject": SUBJECT,
            "to": "recipient@example.invalid", "query": "((in:spam {phishing}))",
            "negatedQuery": '"invoice  notice"'})
        self.assertEqual(legacy["criteria"]["query"],
                         QUERY_SENDERS + " subject:" + SUBJECT + " ((in:spam {phishing}))")
        self.assertEqual(d.canonical_template(native, normalization="query-v1")["criteria"], native["criteria"])

    def test_rejected_forms_remain_rejected_with_the_same_error(self):
        """Preserve the existing compiler's rejected input domain in both modes."""
        cases = [{"from": "a@example.invalid OR (b@example.invalid AND c@example.invalid)"},
                 {"from": '{a@example.invalid b@example.invalid}'},
                 {"subject": "one OR two"}, {"subject": '"one OR two"'},
                 {"from": '"a b" OR c@example.invalid'}, {}]
        for criteria in cases:
            with self.subTest(criteria=criteria):
                properties = {**criteria, "label": "Type/Test"}
                with self.assertRaises(d.DeploymentError) as old:
                    d.build_template(properties, compilation="legacy")
                with self.assertRaises(d.DeploymentError) as new:
                    d.build_template(properties, compilation="xml-fields-v1")
                self.assertEqual(str(old.exception), str(new.exception))

    def test_has_the_word_and_trash_brackets_do_not_change(self):
        """Retain compiler wrappers and preserve the pre-query-v2 interpretation."""
        for text in ("in:spam {phishing}", "(in:spam {phishing})", "((in:spam {phishing}))",
                     "in:spam(phishing)", '"two  words"', "-invoice OR reminder"):
            with self.subTest(text=text):
                properties = {"hasTheWord": text, "shouldTrash": "true"}
                old = d.build_template(properties, compilation="legacy")
                new = d.build_template(properties, compilation="xml-fields-v1")
                self.assertEqual(new, old)
                self.assertEqual(new["criteria"], {"query": "(" + text + ")"})
        compiled = d.canonical_template(d.build_template(
            {"hasTheWord": "(in:spam {phishing})", "shouldTrash": "true"}), normalization="query-v1")
        live = {**compiled, "criteria": {"query": "(in:spam {phishing})"}}
        self.assertEqual(tuple(map(len, d.delta([live], [compiled]))), (1, 1))

    def test_query_v1_still_applies_without_moving_native_fields(self):
        """Apply existing query/subject normalization only after native compilation."""
        compiled = d.build_template({"from": SENDERS, "subject": '"meeting  reminder"',
            "hasTheWord": "from:c@example.invalid OR from:d@example.invalid", "label": "Type/Test"})
        expected = {"from": SENDERS, "subject": '"meeting reminder"',
                    "query": "{from:c@example.invalid from:d@example.invalid}"}
        normalized = d.canonical_template(compiled)
        self.assertEqual(normalized["criteria"], expected)
        self.assertEqual(d.canonical_template(normalized), normalized)


class NativePlanTests(unittest.TestCase):
    def setUp(self):
        """Use temporary synthetic XML and an in-memory API with captured output."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "master.xml"
        self.log = self.local / "journal.json"
        self.output = StringIO()
        stdout = patch("sys.stdout", self.output)
        stdout.start()
        self.addCleanup(stdout.stop)

    def plan(self, client, **kwargs):
        """Create a plan from raw API provenance so both comparison counters are reliable."""
        rows, labels = d.api_fetch(client)
        plan = d.make_plan(None, self.master, [row["filter"] for row in rows],
                           "test@example.invalid", labels, live_rows=rows, **kwargs)
        return plan, rows

    def run_args(self, *extra):
        """Build a run command whose files and account are all synthetic."""
        return ["run", "--target", str(self.master), "--local-dir", str(self.local),
                "--expected-email", "test@example.invalid", *extra]

    def test_native_matches_are_noop_and_not_counted_as_normalization(self):
        """Keep matching IDs without consent or writes and audit compilation separately."""
        write_xml(self.master, [{"from": SENDERS, "label": "Type/Test"},
                                {"subject": SUBJECT, "label": "Type/Subject"}])
        client = raw_client([rule({"from": SENDERS}), rule({"subject": SUBJECT}, "Type/Subject")], self.log)
        original = deepcopy(client.filters)
        with patch.object(d, "get_client", return_value=(client, object())), \
                patch.object(d, "account_email", return_value="test@example.invalid"), \
                patch.object(d, "confirm_interactive", side_effect=AssertionError("unexpected consent")), \
                patch.object(FakeFilters, "create", side_effect=AssertionError("unexpected create")), \
                patch.object(FakeFilters, "delete", side_effect=AssertionError("unexpected delete")), \
                patch.object(FakeLabels, "create", side_effect=AssertionError("unexpected label")):
            self.assertEqual(d.main(self.run_args()), 0)
        self.assertEqual(client.filters, original)
        saved = json.loads((self.local / "run_plan.json").read_text())
        self.assertEqual(d.plan_modes(saved), ("xml-fields-v1", "query-v2"))
        self.assertEqual(saved["schema"], "gmail-filters-deploy-plan/v7")
        self.assertEqual((saved["add"], saved["remove"], saved["normalization_matched"]), ([], [], 0))
        self.assertEqual(saved["normalization_audit"], [])
        self.assertEqual(len(saved["compilation_audit"]), 2)
        self.assertEqual([entry["entry"] for entry in saved["compilation_audit"]], [1, 2])
        self.assertEqual(saved["compilation_audit"][0]["before_criteria"], {"query": QUERY_SENDERS})
        self.assertEqual(saved["compilation_audit"][0]["after_criteria"], {"from": SENDERS})
        comparison = saved["compilation_comparison"]
        self.assertEqual(comparison["scope"], "master_vs_live_at_planning")
        self.assertEqual(comparison["previous"], {"compilation_version": "legacy", "matched": 0, "add": 2, "remove": 2})
        self.assertEqual(comparison["current"], {"compilation_version": "xml-fields-v1", "matched": 2, "add": 0, "remove": 0})
        self.assertIn("変更不要", self.output.getvalue())
        self.assertNotIn("XMLのfrom", self.output.getvalue())
        self.assertNotIn("[XML変換]", self.output.getvalue())
        self.assertNotIn("表記を統一して一致したルール", self.output.getvalue())
        d.show_normalization_summary(saved, verbose=True)
        self.assertIn("[XML変換]", self.output.getvalue())
        self.assertNotIn("[正規化]", self.output.getvalue())

    def test_old_query_matches_become_explicit_differences(self):
        """Retain additions/removals when old compiler output differs from the native target."""
        write_xml(self.master, [{"from": SENDERS, "label": "Type/Test"},
                                {"subject": SUBJECT, "label": "Type/Subject"}])
        client = raw_client([rule({"query": QUERY_SENDERS}),
                             rule({"query": "subject:" + SUBJECT}, "Type/Subject")], self.log)
        plan, _ = self.plan(client)
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (2, 2))
        self.assertEqual(plan["normalization_matched"], 0)
        comparison = plan["compilation_comparison"]
        self.assertEqual(comparison["previous"]["matched"], 2)
        self.assertEqual(comparison["current"]["matched"], 0)
        d.show_normalization_summary(plan)
        self.assertIn("同等性未確認", self.output.getvalue())
        self.assertNotIn("[XML変換]", self.output.getvalue())

    def test_normalization_counter_uses_one_compiler_and_full_rules(self):
        """Count subject spacing independently from field mapping, without double-counting duplicate IDs."""
        write_xml(self.master, [{"from": SENDERS, "subject": "meeting  reminder", "label": "Type/Test"}])
        client = raw_client([rule({"from": SENDERS, "subject": "meeting reminder"})], self.log)
        plan, _ = self.plan(client)
        self.assertEqual(plan["normalization_matched"], 1)
        self.assertEqual(plan["normalization_audit"][0]["reasons"], ["subject_ascii_spaces"])
        self.assertEqual(plan["normalization_audit"][0]["before_criteria"],
                         {"from": SENDERS, "subject": "meeting  reminder"})
        self.assertEqual(plan["compilation_comparison"]["previous"]["matched"], 0)
        self.assertEqual(plan["compilation_comparison"]["current"]["matched"], 1)
        client = raw_client([rule({"from": SENDERS, "subject": "meeting reminder"}),
                             rule({"from": SENDERS, "subject": "meeting  reminder"})], self.log)
        plan, rows = self.plan(client)
        self.assertEqual(plan["normalization_matched"], 0)
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (0, 1))
        selected = d.select_plan_groups(plan, ["other"], current=rows)
        self.assertEqual(selected["skipped_deletions"][0]["reason"], "duplicate_matches")
        self.assertEqual(selected["compilation_comparison"], plan["compilation_comparison"])

    def test_true_field_and_action_changes_remain_visible(self):
        """Never use compiler provenance to suppress genuine criteria or action differences."""
        write_xml(self.master, [{"from": SENDERS, "subject": SUBJECT,
                                "doesNotHaveTheWord": "invoice", "label": "Type/Test"}])
        cases = [rule({"from": SENDERS, "subject": SUBJECT, "negatedQuery": "receipt"}),
                 rule({"from": "other@example.invalid", "subject": SUBJECT, "negatedQuery": "invoice"}),
                 rule({"from": SENDERS, "subject": "{different}", "negatedQuery": "invoice"}),
                 rule({"from": SENDERS, "subject": SUBJECT, "negatedQuery": "invoice"}, "Type/Other")]
        for live in cases:
            with self.subTest(live=live):
                plan, _ = self.plan(raw_client([live], self.log))
                self.assertEqual((len(plan["add"]), len(plan["remove"])), (1, 1))
                self.assertEqual(plan["normalization_matched"], 0)

    def test_both_previous_and_new_compiler_duplicates_stop_before_oauth(self):
        """Keep old duplicate rejection and also reject new native-subject normalization collisions."""
        cases = [
            [{"from": SENDERS, "label": "Type/Test"}, {"hasTheWord": QUERY_SENDERS, "label": "Type/Test"}],
            [{"subject": "{one  two}", "label": "Type/Test"}, {"subject": "{one two}", "label": "Type/Test"}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                write_xml(self.master, rows)
                with patch.object(d, "get_client") as auth, redirect_stderr(StringIO()):
                    self.assertEqual(d.main(self.run_args()), 2)
                auth.assert_not_called()

    def test_selected_run_keeps_declined_delete_and_sends_native_payload(self):
        """Deploy ordinary native changes only after final consent while preserving declined Delete rules."""
        write_xml(self.master, [{"from": SENDERS, "subject": SUBJECT, "hasTheWord": '"two words"', "label": "Type/Test"},
                                {"from": "delete-new@example.invalid", "label": "Action/Delete"}])
        baseline = [rule({"from": "old@example.invalid"}),
                    rule({"from": "delete-old@example.invalid"}, "Action/Delete")]
        client = raw_client(baseline, self.log)
        payloads, prompts = [], []
        create = FakeFilters.create
        def capture(filters, userId, body):
            """Record the actual create body after approval and delegate to the fake API."""
            payloads.append(deepcopy(body))
            return create(filters, userId, body)
        def confirm(prompt):
            """Decline the sensitive group and accept the final ordinary-change confirmation."""
            self.assertEqual(payloads, [])
            prompts.append(prompt)
            return "上記の内容で" in prompt
        with patch.object(d, "get_client", return_value=(client, object())), \
                patch.object(d, "account_email", return_value="test@example.invalid"), \
                patch.object(d, "confirm_interactive", side_effect=confirm), \
                patch.object(FakeFilters, "create", capture):
            self.assertEqual(d.main(self.run_args()), 0)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Action/Delete", prompts[0])
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["criteria"], {"from": SENDERS, "subject": SUBJECT, "query": '("two words")'})
        self.assertEqual(set(payloads[0]), {"criteria", "action"})
        saved = json.loads((self.local / "run_plan.json").read_text())
        self.assertEqual(saved["schema"], "gmail-filters-deploy-plan/v7")
        self.assertEqual(saved["selected_groups"], ["other"])
        live = [row["filter"] for row in d.api_fetch(client)[0]]
        self.assertIn(baseline[1], live)
        self.assertNotIn(rule({"from": "delete-new@example.invalid"}, "Action/Delete"), live)
        self.assertFalse((self.local / "run_pending.json").exists())
        journal = self.local / "logs" / (saved["approval_digest"] + ".json")
        self.assertTrue(json.loads(journal.read_text())["verified"])

    def test_new_v6_partial_batch_resumes_with_native_payload_and_detects_drift(self):
        """Resume a v6 deletion checkpoint once, then reject unrelated external changes."""
        write_xml(self.master, [{"from": SENDERS, "label": "Type/Test"},
                                {"subject": SUBJECT, "label": "Type/Subject"}])
        client = raw_client([rule({"from": "old@example.invalid"}), rule({"subject": "old"}, "Type/Subject")], self.log)
        plan, rows = self.plan(client)
        plan = d.select_plan_groups(plan, ["other"], current=rows)
        self.assertFalse(d.perform(client, plan, plan["account"], plan["approval_digest"], self.log,
                                   batch_size=2, max_batches=1))
        with patch.object(FakeFilters, "delete", side_effect=AssertionError("repeated delete")):
            result = d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        self.assertEqual(d.counter(row["filter"] for row in result["rows"]), d.counter(d.compile_xml(self.master)))
        before = deepcopy(client.filters)
        with patch.object(FakeFilters, "create", side_effect=AssertionError("repeated create")):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        self.assertEqual(client.filters, before)
        next(iter(client.filters.values()))["criteria"]["negatedQuery"] = "changed outside"
        with patch.object(d.time, "sleep"), self.assertRaises(d.DeploymentError):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)

    def test_new_audit_and_compilation_comparison_are_digest_protected(self):
        """Reject edits to either compiler audit field before reading or mutating Gmail."""
        write_xml(self.master, [{"from": SENDERS, "label": "Type/Test"}])
        client = raw_client([], self.log)
        plan, _ = self.plan(client)
        for field in ("compilation_audit", "compilation_comparison"):
            with self.subTest(field=field):
                changed = deepcopy(plan)
                changed[field] = {} if field.endswith("comparison") else []
                with patch.object(d, "api_fetch") as fetch, self.assertRaises(d.DeploymentError):
                    d.perform(client, changed, plan["account"], plan["approval_digest"], self.log)
                fetch.assert_not_called()

    def test_unknown_pairs_stop_before_xml_authentication_or_consent(self):
        """Reject injected, missing, and incompatible versions at both CLI entrypoints."""
        v6 = {"schema": "gmail-filters-deploy-plan/v6", "compilation_version": "xml-fields-v1",
              "normalization_version": "query-v1"}
        unknown = [{key: value for key, value in v6.items() if key != missing}
                   for missing in ("compilation_version", "normalization_version")]
        unknown += [{**v6, "compilation_version": "legacy"}, {**v6, "normalization_version": "legacy"},
                    {**v6, "compilation_version": "xml-fields-v2"}, {**v6, "normalization_version": "query-v2"},
                    {**v6, "schema": "gmail-filters-deploy-plan/v5"},
                    {"schema": "gmail-filters-deploy-plan/v5", "normalization_version": "query-v1", "compilation_version": None}]
        unknown += [{"schema": "gmail-filters-deploy-plan/" + version, "compilation_version": value}
                    for version in ("v3", "v4") for value in ("legacy", "xml-fields-v1", None)]
        for plan in unknown:
            with self.subTest(plan=plan):
                pending = self.local / "run_pending.json"
                pending.write_text(json.dumps(plan))
                with patch.object(d, "compile_xml") as compile_xml, patch.object(d, "get_client") as auth, \
                        patch.object(d, "confirm_interactive") as consent, redirect_stderr(StringIO()):
                    self.assertEqual(d.main(self.run_args()), 2)
                    self.assertEqual(d.main(["apply", "--plan", str(pending), "--approve", "unused",
                        "--local-dir", str(self.local), "--expected-email", "test@example.invalid"]), 2)
                compile_xml.assert_not_called()
                auth.assert_not_called()
                consent.assert_not_called()


class FrozenV5Tests(unittest.TestCase):
    def test_pre_native_v5_apply_and_run_keep_approved_compiler_and_payloads(self):
        """Resume original unselected/selected v5 journals using untouched digests and the legacy compiler."""
        fixture = json.loads((Path(__file__).parent / "fixtures/native-fields-v5.json").read_text())
        for selection, frozen in fixture["cases"].items():
            for command in ("apply", "run"):
                with self.subTest(selection=selection, command=command), tempfile.TemporaryDirectory() as name, ExitStack() as stack:
                    local = Path(name)
                    master = local / "master.xml"
                    master.write_text(fixture["master_xml"])
                    plan = deepcopy(frozen["plan"])
                    digest = plan["approval_digest"]
                    self.assertEqual(d.json_hash({k: v for k, v in plan.items()
                                                 if k not in {"created_utc", "approval_digest"}}), digest)
                    log = local / "logs" / (digest + ".json")
                    log.parent.mkdir()
                    log.write_text(json.dumps(frozen["journal"]))
                    pending = local / "run_pending.json"
                    pending.write_text(json.dumps(plan))
                    client = BatchClient([], log)
                    client.filters = {row["id"]: deepcopy(row) for row in frozen["api_filters"]}
                    client.id_to_label = deepcopy(frozen["id_to_label"])
                    client.n = frozen["next_id"]
                    original_ids = deepcopy(client.filters)
                    compile_xml, file_hash = d.compile_xml, d.file_hash
                    modes = []
                    def compile_mapped(path, **kwargs):
                        """Substitute XML access only while recording both explicitly propagated modes."""
                        modes.append((kwargs.get("compilation"), kwargs.get("normalization")))
                        return compile_xml(master if str(path) == plan["target_path"] else path, **kwargs)
                    def hash_mapped(path):
                        """Hash the matching temporary XML without modifying the frozen plan path."""
                        return file_hash(master if str(path) == plan["target_path"] else path)
                    stack.enter_context(patch.object(d, "compile_xml", side_effect=compile_mapped))
                    stack.enter_context(patch.object(d, "file_hash", side_effect=hash_mapped))
                    stack.enter_context(patch.object(d, "select_master", return_value=Path(plan["target_path"])))
                    fetch = stack.enter_context(patch.object(d, "api_fetch", wraps=d.api_fetch))
                    stack.enter_context(patch.object(d, "get_client", return_value=(client, object())))
                    stack.enter_context(patch.object(d, "account_email", return_value=plan["account"]))
                    stack.enter_context(patch.object(d, "confirm_interactive", return_value=True))
                    stack.enter_context(redirect_stdout(StringIO()))
                    errors = stack.enter_context(redirect_stderr(StringIO()))
                    payloads = []
                    create = FakeFilters.create
                    def capture(filters, userId, body):
                        """Record exact remaining payloads sent through the in-memory transport."""
                        payloads.append(deepcopy(body))
                        return create(filters, userId, body)
                    stack.enter_context(patch.object(FakeFilters, "create", capture))
                    stack.enter_context(patch.object(FakeFilters, "delete", side_effect=AssertionError("replayed deletion")))
                    args = [command, "--local-dir", str(local), "--expected-email", plan["account"], "--batch-size", "2"]
                    if command == "apply":
                        args += ["--plan", str(pending), "--approve", digest, "--allow-removals", "--log", str(log)]
                    self.assertEqual(d.main(args), 0, errors.getvalue())
                    self.assertEqual(payloads, frozen["expected_remaining_payloads"])
                    self.assertTrue(modes)
                    self.assertEqual(set(modes), {("legacy", "query-v1")})
                    self.assertTrue(all(call.kwargs["normalization"] == "query-v1" for call in fetch.call_args_list))
                    state = json.loads(log.read_text())
                    self.assertTrue(state["verified"])
                    self.assertEqual(state["plan_digest"], digest)
                    self.assertEqual(state["events"][:len(frozen["journal"]["events"])], frozen["journal"]["events"])
                    self.assertNotIn("compilation", json.dumps(state))
                    self.assertEqual(plan, frozen["plan"])
                    if command == "apply":
                        self.assertEqual(json.loads(pending.read_text()), frozen["plan"])
                    else:
                        self.assertFalse(pending.exists())
                    for fid, row in original_ids.items():
                        self.assertEqual(client.filters[fid], row)
                    live = [row["filter"] for row in d.api_fetch(client, normalization="query-v1")[0]]
                    self.assertEqual(d.counter(live), d.counter(frozen["effective_target"]))
                    if selection == "selected_other":
                        self.assertIn(rule({"from": "delete-old@example.invalid"}, "Action/Delete"), live)
                        self.assertNotIn(rule({"from": "delete-new@example.invalid"}, "Action/Delete"), live)
                    d.perform(client, plan, plan["account"], digest, log, batch_size=2)
                    self.assertEqual(payloads, frozen["expected_remaining_payloads"])

    def test_selecting_an_old_v5_plan_preserves_its_schema_and_interpretation(self):
        """Match the old module's selected plan exactly when selecting a frozen unselected v5 plan."""
        fixture = json.loads((Path(__file__).parent / "fixtures/native-fields-v5.json").read_text())
        plan = fixture["cases"]["unselected"]["plan"]
        with tempfile.TemporaryDirectory() as name:
            master = Path(name) / "master.xml"
            master.write_text(fixture["master_xml"])
            client = raw_client(fixture["baseline_templates"], Path(name) / "unused.json")
            rows, _ = d.api_fetch(client)
            compile_xml, file_hash = d.compile_xml, d.file_hash
            def compile_mapped(path, **kwargs):
                """Read the frozen fixture XML while checking the old compiler is selected."""
                self.assertEqual(kwargs["compilation"], "legacy")
                return compile_xml(master, **kwargs)
            with patch.object(d, "compile_xml", side_effect=compile_mapped), \
                    patch.object(d, "file_hash", side_effect=lambda path: file_hash(master)):
                selected = d.select_plan_groups(plan, ["other"], current=rows)
            self.assertEqual(selected, fixture["cases"]["selected_other"]["plan"])
            self.assertNotIn("compilation_version", selected)


if __name__ == "__main__":
    unittest.main()
