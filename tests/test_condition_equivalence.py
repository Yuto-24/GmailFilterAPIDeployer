"""Synthetic criteria only: bounded equivalence and frozen legacy resume contracts."""
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
from test_deployer import FakeFilters, FakeLabels, write_xml

OR = "from:a@example.invalid OR from:b@example.invalid"
BRACES = "{from:a@example.invalid from:b@example.invalid}"


def rule(criteria, label="Type/Test"):
    """Build a synthetic template with one user label and the supplied criteria."""
    return {"criteria": criteria, "action": {"addLabelIds": ["label:" + label], "removeLabelIds": []}}


def raw_client(templates, log):
    """Keep raw API spelling; FakeClient's normal constructor canonicalizes it."""
    client = BatchClient([], log)
    names = sorted({v[6:] for row in templates for v in row["action"]["addLabelIds"] if v.startswith("label:")})
    client.id_to_label = {f"Label_{i}": name for i, name in enumerate(names, 1)}
    ids = {name: key for key, name in client.id_to_label.items()}
    client.filters = {f"filter_{i}": {"id": f"filter_{i}", **d.to_api(row, ids, normalization="legacy")}
                      for i, row in enumerate(templates, 1)}
    client.n = len(templates)
    return client


class CriteriaTests(unittest.TestCase):
    def normalize(self, criteria):
        """Normalize criteria while asserting input preservation and idempotence."""
        original = deepcopy(criteria)
        normalized, reasons = d.normalize_criteria(criteria, normalization="query-v1")
        self.assertEqual(criteria, original)
        self.assertEqual(d.normalize_criteria(normalized, normalization="query-v1"), (normalized, []))
        return normalized, reasons

    def test_whole_sender_or_and_wrappers(self):
        """Recognize only a complete sender disjunction, including whole-expression wrappers."""
        for query in (OR, BRACES, "(" + OR + ")", "((" + OR + "))", "((" + BRACES + "))"):
            with self.subTest(query=query):
                normalized, reasons = self.normalize({"query": query, "hasAttachment": True})
                self.assertEqual(normalized, {"query": BRACES, "hasAttachment": True})
                self.assertEqual(reasons, [] if query == BRACES else ["query_sender_or"])

    def test_address_subset_keeps_case_order_and_duplicates(self):
        """Preserve address spelling, repeated terms, and semantically unproven ordering differences."""
        query = "from:Ab.cd+tag-2@Mail-1.Example.invalid OR from:a@example.invalid OR from:a@example.invalid"
        result, _ = self.normalize({"query": query})
        self.assertEqual(result["query"], "{" + query.replace(" OR ", " ") + "}")
        reverse = d.canonical_template(rule({"query": "from:b@example.invalid OR from:a@example.invalid"}))
        forward = d.canonical_template(rule({"query": OR}))
        self.assertEqual(tuple(map(len, d.delta([forward], [reverse]))), (1, 1))

    def test_unknown_query_is_preserved_whole(self):
        """Leave unsupported syntax, addresses, whitespace, and malformed wrappers untouched."""
        queries = [
            '"' + OR + '"', OR + " subject:x", OR + " AND has:attachment", "-" + OR,
            "from:(a@example.invalid OR b@example.invalid)", "(" + OR + ") (subject:x)",
            "(" + OR, OR + ")", "((" + OR + ")", "(" + OR + "))",
            "from:a@example.invalid or from:b@example.invalid", OR.replace(" OR ", "  OR "),
            OR.replace(" OR ", "\tOR\t"), OR.replace(" OR ", "\u3000OR\u3000"),
            "{from:a@example.invalid}", "{from:a@example.invalid  from:b@example.invalid}",
            "from:a..b@example.invalid OR from:b@example.invalid",
            "from:a@localhost OR from:b@example.invalid", "from:a@-bad.invalid OR from:b@example.invalid",
            "from:日本@example.invalid OR from:b@example.invalid", "in:spam(phishing)",
            "((in:spam {phishing}))", 'subject:"日本語  通知"', '"日本語  通知"',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.normalize({"query": query}), ({"query": query}, []))

    def test_native_subject_only_ascii_spaces(self):
        """Collapse native subject ASCII spaces without rewriting other fields or Unicode whitespace."""
        criteria = {"subject": '日本語  "monthly   notice"\t\t続き\n\n次\u3000\u3000文\xa0\xa0末',
                    "query": 'subject:"日本語  通知"', "negatedQuery": OR,
                    "from": "a@example.invalid OR b@example.invalid", "to": "a@example.invalid"}
        expected = dict(criteria, subject='日本語 "monthly notice"\t\t続き\n\n次\u3000\u3000文\xa0\xa0末')
        self.assertEqual(self.normalize(criteria), (expected, ["subject_ascii_spaces"]))

    def test_legacy_never_adds_new_transform(self):
        """Keep sender queries and repeated subject spaces unchanged in legacy mode."""
        original = {"query": "((" + OR + "))", "subject": "monthly  notice"}
        self.assertEqual(d.normalize_criteria(original, normalization="legacy"), (original, []))
        self.assertEqual(d.canonical_template(rule(original), normalization="legacy"), rule(original))

    def test_actions_and_field_boundaries_remain_different(self):
        """Retain action changes and unsupported cross-field or search-expression differences."""
        examples = [
            (rule({"query": OR}), rule({"query": BRACES}, "Type/Other")),
            (rule({"from": "a@example.invalid OR b@example.invalid"}), rule({"query": BRACES})),
            (rule({"subject": "{スカウトが届きました}"}), rule({"query": "subject:{スカウトが届きました}"})),
            (rule({"to": "a@example.invalid"}), rule({"query": "to:a@example.invalid"})),
            (rule({"negatedQuery": OR}), rule({"query": "-(" + OR + ")"})),
            (rule({"query": "((in:spam {phishing}))"}), rule({"query": "(in:spam {phishing})"})),
            (rule({"subject": "monthly  notice"}), rule({"subject": "weekly notice"})),
        ]
        for before, after in examples:
            with self.subTest(before=before):
                self.assertEqual(tuple(map(len, d.delta([d.canonical_template(before)],
                                                       [d.canonical_template(after)]))), (1, 1))


class NormalizedPlanTests(unittest.TestCase):
    def setUp(self):
        """Create an isolated synthetic workspace and capture command output."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "master.xml"
        self.log = self.local / "journal.json"
        self.output = StringIO()
        out = patch("sys.stdout", self.output)
        out.start()
        self.addCleanup(out.stop)

    def plan(self, client):
        """Build a current-mode plan from API rows carrying raw normalization provenance."""
        rows, labels = d.api_fetch(client)
        return d.make_plan(None, self.master, [x["filter"] for x in rows], "test@example.invalid",
                           labels, live_rows=rows), rows

    def test_xml_api_payload_audit_and_noop(self):
        """Match normalized XML to live settings while separating audit data from API payloads."""
        write_xml(self.master, [{"hasTheWord": "((" + OR + "))", "subject": "meeting  reminder", "label": "Type/Test"}])
        client = raw_client([rule({"query": BRACES, "subject": "meeting reminder"})], self.log)
        plan, rows = self.plan(client)
        self.assertEqual((plan["add"], plan["remove"], plan["normalization_matched"]), ([], [], 1))
        self.assertEqual(plan["schema"], "gmail-filters-deploy-plan/v6")
        note = plan["normalization_audit"][0]
        self.assertEqual((note["source"], note["entry"]), ("xml", 1))
        self.assertEqual(note["reasons"], ["query_sender_or", "subject_ascii_spaces"])
        self.assertNotIn("normalization", rows[0]["filter"])
        self.assertEqual(set(d.to_api(rows[0]["filter"], {"Type/Test": "Label_1"})), {"criteria", "action"})

    def test_api_only_variation_counts_once_and_retains_raw_source(self):
        """Count API spelling changes once, preserve raw criteria, and reject inconsistent provenance."""
        write_xml(self.master, [{"hasTheWord": BRACES, "subject": "meeting reminder", "label": "Type/Test"}])
        client = raw_client([rule({"query": OR, "subject": "meeting  reminder"})], self.log)
        # Real API raw text may include outer whitespace too.
        client.filters["filter_1"]["criteria"]["query"] = "  " + OR + "  "
        plan, rows = self.plan(client)
        self.assertEqual(plan["normalization_matched"], 1)
        api_note = next(x for x in plan["normalization_audit"] if x["source"] == "api")
        self.assertEqual(api_note["filter_id"], "filter_1")
        self.assertEqual(api_note["before_criteria"]["query"], "  " + OR + "  ")
        self.assertEqual(d.filter_snapshot(rows)[0], {"filter_1": rows[0]["filter"]})
        rows[0]["normalization"]["before_criteria"]["query"] = "unrelated"
        with self.assertRaises(d.DeploymentError):
            d.make_plan(None, self.master, [x["filter"] for x in rows], plan["account"], {}, live_rows=rows)

    def test_counter_gain_does_not_count_duplicates_or_action_mismatches(self):
        """Count only newly matched target slots and retain duplicate-ID deletion protection."""
        write_xml(self.master, [{"hasTheWord": BRACES, "label": "Type/Test"}])
        client = raw_client([rule({"query": OR}), rule({"query": "(" + BRACES + ")"})], self.log)
        plan, rows = self.plan(client)
        # One exact match existed already; normalization does not add another target slot.
        self.assertEqual(plan["normalization_matched"], 0)
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (0, 1))
        selected = d.select_plan_groups(plan, ["other"], current=rows)
        self.assertEqual(selected["schema"], "gmail-filters-deploy-plan/v6")
        self.assertEqual(selected["skipped_deletions"][0]["reason"], "duplicate_matches")
        client = raw_client([rule({"query": OR}, "Type/Other")], self.log)
        plan, _ = self.plan(client)
        self.assertEqual(plan["normalization_matched"], 0)
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (1, 1))

    def test_normalized_master_duplicates_stop_before_authentication(self):
        """Reject Master rules that become duplicates before any authentication attempt."""
        write_xml(self.master, [{"hasTheWord": x, "label": "Type/Test"} for x in (OR, BRACES)])
        self.assertEqual(len(d.compile_xml(self.master, normalization="legacy")), 2)
        with patch.object(d, "get_client") as auth, redirect_stderr(StringIO()):
            self.assertEqual(d.main(["run", "--local-dir", str(self.local), "--target", str(self.master),
                                    "--expected-email", "test@example.invalid"]), 2)
        auth.assert_not_called()

    def test_new_run_noop_has_no_approval_or_mutation_and_saves_audit(self):
        """Complete a notation-only run without consent or writes while preserving IDs and audit."""
        write_xml(self.master, [{"hasTheWord": BRACES, "label": "Type/Test"}])
        client = raw_client([rule({"query": OR})], self.log)
        with patch.object(d, "get_client", return_value=(client, object())), \
                patch.object(d, "account_email", return_value="test@example.invalid"), \
                patch.object(d, "confirm_interactive", side_effect=AssertionError("unexpected consent")), \
                patch.object(FakeFilters, "create", side_effect=AssertionError("unexpected create")), \
                patch.object(FakeFilters, "delete", side_effect=AssertionError("unexpected delete")), \
                patch.object(FakeLabels, "create", side_effect=AssertionError("unexpected label")):
            self.assertEqual(d.main(["run", "--local-dir", str(self.local), "--target", str(self.master),
                                    "--expected-email", "test@example.invalid", "-v"]), 0)
        self.assertIn("表記を統一して一致したルール: 1件", self.output.getvalue())
        self.assertIn("[正規化] api", self.output.getvalue())
        self.assertTrue((self.local / "run_plan.json").exists())
        self.assertFalse((self.local / "run_pending.json").exists())
        self.assertEqual(list(client.filters), ["filter_1"])

    def test_unknown_remaining_differences_are_reported_without_pairing(self):
        """Show unsupported equivalence as unresolved additions and removals."""
        write_xml(self.master, [{"hasTheWord": BRACES, "label": "Type/Test"}])
        client = raw_client([rule({"from": "a@example.invalid OR b@example.invalid"})], self.log)
        plan, _ = self.plan(client)
        d.show_normalization_summary(plan)
        self.assertIn("同等性未確認", self.output.getvalue())
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (1, 1))
        self.assertEqual(plan["normalization_matched"], 0)

    def test_selected_plan_payload_readback_and_normalized_external_drift(self):
        """Keep selected-plan payloads and readback consistent, accepting notation but rejecting real drift."""
        write_xml(self.master, [{"hasTheWord": OR, "subject": "meeting  reminder", "label": "Type/Test"}])
        client = raw_client([rule({"from": "old@example.invalid"})], self.log)
        plan, rows = self.plan(client)
        plan = d.select_plan_groups(plan, ["other"], current=rows)
        d.validate_before_final_approval(plan, plan["account"], self.log, 2, rows)
        payloads = []
        create = FakeFilters.create
        def capture(filters, userId, body):
            """Record each prospective create payload before delegating to the in-memory transport."""
            payloads.append(deepcopy(body))
            return create(filters, userId, body)
        with patch.object(FakeFilters, "create", capture):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        self.assertEqual(payloads[0]["criteria"], {"query": BRACES, "subject": "meeting reminder"})
        self.assertEqual(set(payloads[0]), {"criteria", "action"})
        self.assertTrue(json.loads(self.log.read_text())["verified"])
        # Gmail may serialize the same sender query differently on later reads.
        next(iter(client.filters.values()))["criteria"]["query"] = "((" + OR + "))"
        d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        next(iter(client.filters.values()))["criteria"]["subject"] = "different message"
        with patch.object(d.time, "sleep"), self.assertRaises(d.DeploymentError):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)

    def test_audit_is_digest_protected_before_reads(self):
        """Reject edited normalization provenance before fetching deployment state."""
        write_xml(self.master, [{"hasTheWord": OR, "label": "Type/Test"}])
        client = raw_client([], self.log)
        plan, _ = self.plan(client)
        plan["normalization_audit"][0]["before_criteria"]["query"] = "tampered"
        with patch.object(d, "api_fetch") as fetch, self.assertRaises(d.DeploymentError):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log)
        fetch.assert_not_called()

    def test_unknown_plan_modes_reject_before_compile_auth_and_consent(self):
        """Reject unknown or missing plan interpretations before processing either entrypoint."""
        unknown = [{"schema": "gmail-filters-deploy-plan/v99"},
                   {"schema": "gmail-filters-deploy-plan/v5"},
                   {"schema": "gmail-filters-deploy-plan/v5", "normalization_version": "query-v2"},
                   {"schema": "gmail-filters-deploy-plan/v3", "normalization_version": "query-v1"},
                   {"schema": "gmail-filters-deploy-plan/v4", "normalization_version": "legacy"}]
        for plan in unknown:
            with self.subTest(plan=plan):
                path = self.local / "run_pending.json"
                path.write_text(json.dumps(plan))
                with patch.object(d, "compile_xml") as compile_xml, patch.object(d, "get_client") as auth, \
                        patch.object(d, "confirm_interactive") as consent, redirect_stderr(StringIO()):
                    common = ["--local-dir", str(self.local), "--expected-email", "test@example.invalid"]
                    self.assertEqual(d.main(["run", *common]), 2)
                    self.assertEqual(d.main(["apply", "--plan", str(path), "--approve", "unused", *common]), 2)
                compile_xml.assert_not_called()
                auth.assert_not_called()
                consent.assert_not_called()


class LegacyResumeTests(unittest.TestCase):
    def test_frozen_v3_v4_apply_and_pending_run_preserve_approved_meaning(self):
        """Resume frozen legacy plans without changing digests, payload meaning, IDs, or prior events."""
        fixture = json.loads((Path(__file__).parent / "fixtures/condition-equivalence-legacy.json").read_text())
        for version, frozen in fixture["cases"].items():
            for command in ("apply", "run"):
                with self.subTest(version=version, command=command), tempfile.TemporaryDirectory() as name, ExitStack() as stack:
                    local = Path(name)
                    master = local / "master.xml"
                    master.write_text(fixture["master_xml"])
                    plan = deepcopy(frozen["plan"])
                    frozen_digest = plan["approval_digest"]
                    self.assertEqual(d.json_hash({k: v for k, v in plan.items()
                                                 if k not in {"created_utc", "approval_digest"}}), frozen_digest)
                    log = local / "logs" / (frozen_digest + ".json")
                    log.parent.mkdir()
                    log.write_text(json.dumps(frozen["journal"]))
                    pending = local / "run_pending.json"
                    pending.write_text(json.dumps(plan))
                    client = BatchClient([], log)
                    client.filters = {x["id"]: deepcopy(x) for x in frozen["api_filters"]}
                    client.id_to_label = deepcopy(frozen["id_to_label"])
                    client.n = frozen["next_id"]
                    # Substitute only filesystem access; never edit/re-sign the frozen plan.
                    compile_xml, file_hash = d.compile_xml, d.file_hash
                    modes = []
                    compilers = []
                    def compile_mapped(path, **kwargs):
                        """Compile the temporary fixture XML without editing the approved plan path or mode."""
                        modes.append(kwargs.get("normalization"))
                        compilers.append(kwargs.get("compilation"))
                        return compile_xml(master if str(path) == plan["target_path"] else path, **kwargs)
                    def hash_mapped(path):
                        """Hash the temporary XML for the frozen plan path without re-signing the plan."""
                        return file_hash(master if str(path) == plan["target_path"] else path)
                    stack.enter_context(patch.object(d, "compile_xml", side_effect=compile_mapped))
                    stack.enter_context(patch.object(d, "file_hash", side_effect=hash_mapped))
                    stack.enter_context(patch.object(d, "select_master", return_value=Path(plan["target_path"])))
                    fetch = stack.enter_context(patch.object(d, "api_fetch", wraps=d.api_fetch))
                    stack.enter_context(patch.object(d, "get_client", return_value=(client, object())))
                    stack.enter_context(patch.object(d, "account_email", return_value=plan["account"]))
                    stack.enter_context(patch.object(d, "confirm_interactive", return_value=True))
                    stack.enter_context(redirect_stdout(StringIO()))
                    stack.enter_context(redirect_stderr(StringIO()))
                    payloads = []
                    create = FakeFilters.create
                    def capture(filters, userId, body):
                        """Record resumed create payloads for comparison with the frozen legacy output."""
                        payloads.append(deepcopy(body))
                        return create(filters, userId, body)
                    stack.enter_context(patch.object(FakeFilters, "create", capture))
                    delete = stack.enter_context(patch.object(FakeFilters, "delete", side_effect=AssertionError("replayed delete")))
                    args = [command, "--local-dir", str(local), "--expected-email", plan["account"], "--batch-size", "2"]
                    if command == "apply":
                        args += ["--plan", str(pending), "--approve", frozen_digest, "--allow-removals", "--log", str(log)]
                    self.assertEqual(d.main(args), 0)
                    self.assertEqual(payloads, frozen["expected_remaining_payloads"])
                    self.assertTrue(modes)
                    self.assertEqual(set(modes), {"legacy"})
                    self.assertEqual(set(compilers), {"legacy"})
                    self.assertTrue(fetch.call_args_list)
                    self.assertTrue(all(call.kwargs["normalization"] == "legacy" for call in fetch.call_args_list))
                    delete.assert_not_called()
                    state = json.loads(log.read_text())
                    self.assertTrue(state["verified"])
                    self.assertEqual(state["plan_digest"], frozen_digest)
                    self.assertEqual(state["events"][:len(frozen["journal"]["events"])], frozen["journal"]["events"])
                    self.assertNotIn("normalization", json.dumps(state))
                    self.assertNotIn("compilation", json.dumps(state))
                    self.assertEqual(plan, frozen["plan"])
                    self.assertEqual(d.counter(x["filter"] for x in d.api_fetch(client, normalization="legacy")[0]),
                                     d.counter(fixture["target_templates"]))
                    self.assertIn("filter_1", client.filters)
                    self.assertEqual(client.filters["filter_1"], frozen["api_filters"][0])


if __name__ == "__main__":
    unittest.main()
