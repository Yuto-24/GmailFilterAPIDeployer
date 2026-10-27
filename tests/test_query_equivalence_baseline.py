"""Issue #1: freeze currently supported XML/API translation without guessing query equivalence.

These tests use only synthetic addresses and never contact Gmail.
"""
import unittest

import gmail_filter_deployer as d


class QueryEquivalenceBaselineTests(unittest.TestCase):
    def test_sender_or_is_compiled_as_query(self):
        template = d.build_template({
            "from": "alpha@example.invalid OR beta@example.invalid",
            "label": "Type/Promotion",
        })
        self.assertEqual(
            template["criteria"],
            {"query": "{from:alpha@example.invalid from:beta@example.invalid}"},
        )

    def test_subject_brace_is_compiled_as_query(self):
        template = d.build_template({
            "subject": "{スカウトが届きました}",
            "label": "Type/Career",
        })
        self.assertEqual(
            template["criteria"],
            {"query": "subject:{スカウトが届きました}"},
        )

    def test_has_the_word_parentheses_are_preserved(self):
        template = d.build_template({
            "hasTheWord": "in:spam {phishing}",
            "shouldTrash": "true",
        })
        self.assertEqual(template["criteria"]["query"], "(in:spam {phishing})")

    def test_uncertified_query_rewriting_is_not_done(self):
        a = d.canonical_template({
            "criteria": {"query": "(in:spam {phishing})"},
            "action": {"addLabelIds": ["TRASH"]},
        })
        b = d.canonical_template({
            "criteria": {"query": "((in:spam {phishing}))"},
            "action": {"addLabelIds": ["TRASH"]},
        })
        self.assertNotEqual(a, b)
        adds, removes = d.delta([a], [b])
        self.assertEqual((len(adds), len(removes)), (1, 1))

    def test_negation_changes_are_not_suppressed(self):
        common = {"addLabelIds": ["label:Type/Promotion"]}
        before = d.canonical_template({
            "criteria": {"query": "{from:a@example.invalid from:b@example.invalid}"},
            "action": common,
        })
        after = d.canonical_template({
            "criteria": {
                "query": "{from:a@example.invalid from:b@example.invalid}",
                "negatedQuery": "invoice",
            },
            "action": common,
        })
        self.assertEqual(tuple(len(x) for x in d.delta([before], [after])), (1, 1))

    def test_action_delete_and_trash_remain_distinct(self):
        criteria = {"query": "from:sender@example.invalid"}
        delete = d.canonical_template({
            "criteria": criteria,
            "action": {"addLabelIds": ["label:Action/Delete"]},
        })
        trash = d.canonical_template({
            "criteria": criteria,
            "action": {"addLabelIds": ["TRASH"]},
        })
        self.assertEqual(tuple(len(x) for x in d.delta([delete], [trash])), (1, 1))


if __name__ == "__main__":
    unittest.main()
