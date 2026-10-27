"""Bounded spam wrapper equivalence and immutable pre-query-v2 resume fixtures."""
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
from test_condition_equivalence import raw_client, rule, OR, BRACES
from test_deployer import FakeFilters, FakeLabels, write_xml

BODY = "in:spam {phishing}"
QUERY = "(" + BODY + ")"


def trash(query):
    """Build one synthetic Trash template without normalizing its raw query."""
    return {"criteria": {"query": query}, "action": {"addLabelIds": ["TRASH"], "removeLabelIds": []}}


class SpamCriteriaTests(unittest.TestCase):
    def test_balanced_wrappers_normalize_once_without_mutation(self):
        """Keep case, order, repetitions and all other fields while unifying whole wrappers."""
        for body in (BODY, "in:spam {Phishing 123 Phishing}", "in:spam {offer phishing}"):
            for count in range(5):
                with self.subTest(body=body, count=count):
                    query = "(" * count + body + ")" * count
                    criteria = {"query": query, "negatedQuery": query, "from": "a@example.invalid",
                                "subject": "monthly  notice", "hasAttachment": True}
                    original = deepcopy(criteria)
                    actual, reasons = d.normalize_criteria(criteria, normalization="query-v2")
                    self.assertEqual(criteria, original)
                    expected = {**criteria, "query": "(" + body + ")", "subject": "monthly notice"}
                    self.assertEqual(actual, expected)
                    self.assertEqual(reasons, ([] if count == 1 else ["query_spam_outer_parentheses"])
                                     + ["subject_ascii_spaces"])
                    self.assertEqual(d.normalize_criteria(actual, normalization="query-v2"), (actual, []))
                    for version in ("legacy", "query-v1"):
                        self.assertEqual(d.normalize_criteria({"query": query}, normalization=version),
                                         ({"query": query}, []))
        self.assertEqual(d.normalize_criteria({"query": "((" + OR + "))"}, normalization="query-v2"),
                         ({"query": BRACES}, ["query_sender_or"]))

    def test_unsupported_whole_queries_are_unchanged(self):
        """Reject operator words, malformed wrappers and every syntax outside the complete grammar."""
        unknown = ['in:spam {"phishing"}', 'in:spam {phishing\\ offer}', 'in:spam {-phishing}',
                   'in:spam {phishing*}', 'in:spam {日本語}', 'in:spam {phishing_offer}',
                   'in:spam {phishing.offer}', 'in:spam {phishing  offer}', 'in:spam {phishing\toffer}',
                   'in:spam {phishing\u3000offer}', 'in:spam {phishing\xa0offer}', 'in:spam {}',
                   'IN:SPAM {phishing}', 'in:spam  {phishing}', 'in:spam(phishing)',
                   'in:spam {(phishing)}', 'in:spam {phishing} subject:x', '-in:spam {phishing}',
                   'in:spam {phishing} OR subject:x', '(in:spam) {phishing}', 'in:spam { phishing}',
                   'in:spam {phishing }', '"in:spam {phishing}"', 'in:spam {phishing}\n',
                   *('in:spam {phishing ' + word + ' offer}' for word in ('AND', 'OR', 'AROUND', 'NOT'))]
        for body in unknown:
            for count in range(3):
                query = '(' * count + body + ')' * count
                with self.subTest(query=query):
                    criteria = {"query": query, "negatedQuery": BODY}
                    self.assertEqual(d.normalize_criteria(criteria, normalization="query-v2"), (criteria, []))
        for left, right in ((1, 0), (0, 1), (2, 1), (1, 2)):
            criteria = {"query": '(' * left + BODY + ')' * right}
            self.assertEqual(d.normalize_criteria(criteria, normalization="query-v2"), (criteria, []))

    def test_words_actions_and_fields_remain_distinct(self):
        """Avoid equating reordered words, real action changes or negated-query spelling."""
        pairs = [(trash(QUERY), trash('(in:spam {Phishing})')),
                 (trash('(in:spam {offer phishing})'), trash('(in:spam {phishing offer})')),
                 (trash(QUERY), rule({"query": QUERY})),
                 (trash(QUERY), {**trash(QUERY), "criteria": {"negatedQuery": QUERY}}),
                 (rule({"negatedQuery": QUERY}), rule({"negatedQuery": '(' + QUERY + ')'}))]
        for before, after in pairs:
            with self.subTest(before=before, after=after):
                self.assertEqual(tuple(map(len, d.delta([d.canonical_template(before)],
                                                       [d.canonical_template(after)]))), (1, 1))


class SpamPlanTests(unittest.TestCase):
    def setUp(self):
        """Isolate all plans and journals and silence synthetic CLI output."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.master = self.local / "master.xml"
        self.log = self.local / "journal.json"
        stdout = patch("sys.stdout", StringIO())
        stdout.start()
        self.addCleanup(stdout.stop)

    def plan(self, client):
        """Build a current plan with raw API provenance preserved for audit counts."""
        rows, labels = d.api_fetch(client)
        return d.make_plan(None, self.master, [row["filter"] for row in rows],
                           "test@example.invalid", labels, live_rows=rows), rows

    def test_new_run_noop_covers_both_xml_spellings_and_api_variations(self):
        """Retain original IDs and actions with zero consent or mutation for notation-only differences."""
        for xml_query, api_query, gain in ((BODY, QUERY, 0), (QUERY, QUERY, 1), (BODY, '(' + QUERY + ')', 1)):
            with self.subTest(xml_query=xml_query, api_query=api_query):
                write_xml(self.master, [{"hasTheWord": xml_query, "shouldTrash": "true"}])
                client = raw_client([trash(api_query)], self.log)
                original = deepcopy(client.filters)
                with patch.object(d, "get_client", return_value=(client, object())), \
                        patch.object(d, "account_email", return_value="test@example.invalid"), \
                        patch.object(d, "confirm_interactive", side_effect=AssertionError("unexpected consent")), \
                        patch.object(FakeFilters, "create", side_effect=AssertionError("unexpected create")), \
                        patch.object(FakeFilters, "delete", side_effect=AssertionError("unexpected delete")), \
                        patch.object(FakeLabels, "create", side_effect=AssertionError("unexpected label")):
                    self.assertEqual(d.main(["run", "--target", str(self.master), "--local-dir", str(self.local),
                                             "--expected-email", "test@example.invalid", "-v"]), 0)
                plan = json.loads((self.local / "run_plan.json").read_text())
                self.assertEqual((plan["add"], plan["remove"], plan["normalization_matched"]), ([], [], gain))
                self.assertEqual(d.plan_modes(plan), ("xml-fields-v1", "query-v2"))
                self.assertEqual(plan["schema"], "gmail-filters-deploy-plan/v7")
                self.assertEqual(client.filters, original)
                self.assertFalse((self.local / "run_pending.json").exists())
                if gain:
                    note = plan["normalization_audit"][0]
                    self.assertEqual(note["reasons"], ["query_spam_outer_parentheses"])
                    self.assertEqual(note["before_criteria"]["query"], '(' + QUERY + ')')
                    self.assertEqual(note["after_criteria"]["query"], QUERY)
                    self.assertEqual(note["source"], "xml" if xml_query == QUERY else "api")
                    changed = deepcopy(plan)
                    changed["normalization_audit"] = []
                    with patch.object(d, "api_fetch") as fetch, self.assertRaises(d.DeploymentError):
                        d.perform(client, changed, plan["account"], plan["approval_digest"], self.log)
                    fetch.assert_not_called()

    def test_counter_and_duplicate_id_protection_remain_conservative(self):
        """Use full-rule counters and skip deletions that match multiple live IDs after normalization."""
        write_xml(self.master, [{"hasTheWord": QUERY, "shouldTrash": "true"}])
        client = raw_client([trash(QUERY), trash('(' + QUERY + ')')], self.log)
        plan, rows = self.plan(client)
        self.assertEqual((len(plan["add"]), len(plan["remove"]), plan["normalization_matched"]), (0, 1, 0))
        selected = d.select_plan_groups(plan, ["trash"], current=rows)
        self.assertEqual(selected["skipped_deletions"][0]["reason"], "duplicate_matches")
        self.assertEqual(selected["schema"], "gmail-filters-deploy-plan/v7")
        client = raw_client([rule({"query": QUERY})], self.log)
        plan, _ = self.plan(client)
        self.assertEqual((len(plan["add"]), len(plan["remove"]), plan["normalization_matched"]), (1, 1, 0))

    def test_normalized_xml_duplicates_stop_before_auth(self):
        """Reject duplicate ordinary-label conditions introduced by the new wrapper equivalence."""
        write_xml(self.master, [{"hasTheWord": query, "label": "Type/Test"} for query in (BODY, QUERY)])
        self.assertEqual(len(d.compile_xml(self.master, normalization="query-v1")), 2)
        with patch.object(d, "get_client") as auth, redirect_stderr(StringIO()):
            self.assertEqual(d.main(["run", "--target", str(self.master), "--local-dir", str(self.local),
                                     "--expected-email", "test@example.invalid"]), 2)
        auth.assert_not_called()

    def test_selected_trash_payload_partial_resume_and_external_drift(self):
        """Send the v7 canonical query once, resume deletion checkpoints, and reject changed words."""
        write_xml(self.master, [{"hasTheWord": QUERY, "shouldTrash": "true"}])
        client = raw_client([trash('(in:spam {oldphishing})')], self.log)
        plan, rows = self.plan(client)
        plan = d.select_plan_groups(plan, ["trash"], current=rows)
        d.validate_before_final_approval(plan, plan["account"], self.log, 2, rows)
        self.assertFalse(d.perform(client, plan, plan["account"], plan["approval_digest"], self.log,
                                   batch_size=2, max_batches=1))
        payloads = []
        create = FakeFilters.create
        def capture(filters, userId, body):
            """Capture the prospective create payload before in-memory dispatch."""
            payloads.append(deepcopy(body))
            return create(filters, userId, body)
        with patch.object(FakeFilters, "create", capture), \
                patch.object(FakeFilters, "delete", side_effect=AssertionError("replayed delete")):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        self.assertEqual(payloads, [trash(QUERY)])
        self.assertTrue(json.loads(self.log.read_text())["verified"])
        live = next(iter(client.filters.values()))
        live["criteria"]["query"] = '(' + QUERY + ')'
        with patch.object(FakeFilters, "create", side_effect=AssertionError("replayed create")):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        live["criteria"]["query"] = '(in:spam {changedword})'
        with patch.object(d.time, "sleep"), self.assertRaises(d.DeploymentError):
            d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)

    def test_declined_trash_retains_original_raw_rule_and_id(self):
        """Deploy an ordinary-label change while leaving a declined Trash replacement untouched."""
        write_xml(self.master, [{"hasTheWord": QUERY, "shouldTrash": "true"},
                                {"from": "new@example.invalid", "label": "Type/Test"}])
        client = raw_client([trash('((in:spam {oldphishing}))'),
                             rule({"from": "old@example.invalid"})], self.log)
        original = deepcopy(client.filters["filter_1"])
        plan, rows = self.plan(client)
        plan = d.select_plan_groups(plan, ["other"], current=rows)
        d.perform(client, plan, plan["account"], plan["approval_digest"], self.log, batch_size=2)
        self.assertEqual(client.filters["filter_1"], original)
        self.assertEqual(d.counter(row["filter"] for row in d.api_fetch(client)[0]),
                         d.counter(d.planned_states(plan)[1]))

    def test_unknown_v7_pairs_stop_before_compile_auth_and_consent(self):
        """Reject missing and unrecognized v7 interpretations before either CLI entrypoint reads XML."""
        v7 = {"schema": "gmail-filters-deploy-plan/v7", "compilation_version": "xml-fields-v1",
              "normalization_version": "query-v2"}
        unknown = [{key: value for key, value in v7.items() if key != missing}
                   for missing in ("compilation_version", "normalization_version")]
        unknown += [{**v7, "normalization_version": value} for value in ("legacy", "query-v1", "query-v3")]
        unknown += [{**v7, "compilation_version": value} for value in ("legacy", "xml-fields-v2")]
        for plan in unknown:
            with self.subTest(plan=plan):
                pending = self.local / "run_pending.json"
                pending.write_text(json.dumps(plan))
                with patch.object(d, "compile_xml") as compile_xml, patch.object(d, "get_client") as auth, \
                        patch.object(d, "confirm_interactive") as consent, redirect_stderr(StringIO()):
                    common = ["--local-dir", str(self.local), "--expected-email", "test@example.invalid"]
                    self.assertEqual(d.main(["run", *common]), 2)
                    self.assertEqual(d.main(["apply", "--plan", str(pending), "--approve", "unused", *common]), 2)
                compile_xml.assert_not_called()
                auth.assert_not_called()
                consent.assert_not_called()


class FrozenV6Tests(unittest.TestCase):
    def test_pre_query_v2_v6_apply_and_run_preserve_all_trash_paths(self):
        """Resume retained, created and declined Trash cases with original v6 digests and query-v1 payloads."""
        fixture = json.loads((Path(__file__).parent / "fixtures/spam-wrappers-v6.json").read_text())
        for scenario, frozen in fixture["cases"].items():
            for command in ("apply", "run"):
                with self.subTest(scenario=scenario, command=command), tempfile.TemporaryDirectory() as name, ExitStack() as stack:
                    local = Path(name)
                    master = local / "master.xml"
                    master.write_text(frozen["master_xml"])
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
                        """Map only fixture XML access without rewriting the approved plan or its digest."""
                        modes.append((kwargs.get("compilation"), kwargs.get("normalization")))
                        return compile_xml(master if str(path) == plan["target_path"] else path, **kwargs)
                    def hash_mapped(path):
                        """Return the frozen XML hash through the original approved absolute path."""
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
                        """Capture the remaining old-format create payloads without real Gmail access."""
                        payloads.append(deepcopy(body))
                        return create(filters, userId, body)
                    stack.enter_context(patch.object(FakeFilters, "create", capture))
                    stack.enter_context(patch.object(FakeFilters, "delete", side_effect=AssertionError("replayed delete")))
                    args = [command, "--local-dir", str(local), "--expected-email", plan["account"], "--batch-size", "2"]
                    if command == "apply":
                        args += ["--plan", str(pending), "--approve", digest, "--allow-removals", "--log", str(log)]
                    self.assertEqual(d.main(args), 0, errors.getvalue())
                    self.assertEqual(payloads, frozen["expected_remaining_payloads"])
                    self.assertTrue(modes)
                    self.assertEqual(set(modes), {("xml-fields-v1", "query-v1")})
                    self.assertTrue(fetch.call_args_list)
                    self.assertTrue(all(call.kwargs["normalization"] == "query-v1" for call in fetch.call_args_list))
                    state = json.loads(log.read_text())
                    self.assertTrue(state["verified"])
                    self.assertEqual(state["plan_digest"], digest)
                    self.assertEqual(state["events"][:len(frozen["journal"]["events"])], frozen["journal"]["events"])
                    self.assertNotIn("query_spam_outer_parentheses", json.dumps(state))
                    self.assertEqual(plan, frozen["plan"])
                    if command == "apply":
                        self.assertEqual(json.loads(pending.read_text()), frozen["plan"])
                    else:
                        self.assertFalse(pending.exists())
                    for fid, row in original_ids.items():
                        self.assertEqual(client.filters[fid], row)
                    live = [row["filter"] for row in d.api_fetch(client, normalization="query-v1")[0]]
                    self.assertEqual(d.counter(live), d.counter(frozen["effective_target"]))
                    live_trash = next(row for row in live if "TRASH" in row["action"]["addLabelIds"])
                    self.assertEqual(live_trash["criteria"]["query"],
                                     '((in:spam {oldphishing}))' if scenario == "declined_trash" else '(' + QUERY + ')')
                    d.perform(client, plan, plan["account"], digest, log, batch_size=2)
                    self.assertEqual(payloads, frozen["expected_remaining_payloads"])


if __name__ == "__main__":
    unittest.main()
