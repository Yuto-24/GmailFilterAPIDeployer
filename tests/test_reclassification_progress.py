"""Phase progress is based on the work iterator, not verbose API logs."""
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import gmail_message_reclassifier as m


class ProgressTests(TestCase):
    def capture(self, fn):
        out = StringIO()
        with redirect_stdout(out):
            fn()
        return out.getvalue()

    def test_unknown_total_is_not_falsely_zero_percent(self):
        text = self.capture(lambda: m.show_progress("メール列挙", 500))
        self.assertIn("集計中", text)
        self.assertNotIn("0.0%", text)
        self.assertIn("500件", text)

    def test_zero_work_is_a_real_complete_phase(self):
        text = self.capture(lambda: m.show_progress("更新", 0, 0))
        self.assertIn("0/0 (100.0%)", text)

    def test_distinct_progress_percentages(self):
        text = self.capture(lambda: (
            m.show_progress("更新", 9355, 9355),
            m.show_progress("最終照合", 500, 29087),
        ))
        self.assertIn("更新: 9355/9355 (100.0%)", text)
        self.assertIn("最終照合: 500/29087 (1.7%)", text)
        self.assertNotIn("全工程完了", text)

    def test_reconcile_counts_actual_snapshot_without_extra_reads(self):
        ids = [str(i) for i in range(1001)]
        calls = []
        class API:
            def labels(self):
                return {}
            def filters_match(self, *args, **kwargs):
                return None
            def snapshots(self, mids):
                for mid in mids:
                    calls.append(mid)
                    yield mid, {"id": mid}
            def metadata(self, mid, raw, labels):
                return raw

        plan = SimpleNamespace(
            manifest={"summary": {"target": 1001}, "normalization_version": "query-v2"},
            rules=[], labels={}, rows=lambda: ({"id": mid} for mid in ids),
            row=lambda mid: {"id": mid},
        )
        with patch.object(m, "validate_labels"), patch.object(m, "observe") as observe:
            output = self.capture(lambda: m.reconcile(API(), plan, None, "apply", phase="最終照合"))
        self.assertEqual(calls, ids)
        self.assertEqual(observe.call_count, len(ids))
        self.assertIn("最終照合: 500/1001 (50.0%)", output)
        self.assertIn("最終照合: 1001/1001 (100.0%)", output)

    def test_restore_denominator_excludes_unmodified_messages(self):
        # Only previously applied messages are eligible for restore.
        counts = {"new": 7, "applied": 3, "restored": 2}
        total = counts["applied"] + counts["restored"]
        text = self.capture(lambda: m.show_progress("復元", counts["restored"], total, force=True))
        self.assertIn("復元: 2/5 (40.0%)", text)
