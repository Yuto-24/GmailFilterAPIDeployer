"""Freeze pre-query-v2 v6 plans with retained, newly created, and declined Trash rules.

Run from the repository root: python3 tests/fixtures/generate_spam_wrappers_v6.py
The old module comes from local Git; all transport and account data are synthetic.
"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REVISION = "76e5407bfa86d6a89cad947cbf665a695f272a78"
sys.path.insert(0, str(ROOT / "tests"))
from test_deployer import write_xml
from test_batch import BatchClient


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        """Return one instant so repeated generation produces identical plan and journal data."""
        return datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc).astimezone(tz)


def generate():
    """Use the real v6 module to checkpoint deletion before any pending additions are sent."""
    with tempfile.TemporaryDirectory() as name, redirect_stdout(StringIO()):
        root = Path(name)
        source = root / "v6_source.py"
        source.write_bytes(subprocess.check_output(
            ["git", "show", REVISION + ":gmail_filter_deployer.py"], cwd=ROOT))
        spec = importlib.util.spec_from_file_location("spam_wrappers_fixture_source", source)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        old.datetime = FrozenDatetime
        result = {"generated_from": REVISION, "cases": {}}
        common = {"from": "keep1@example.invalid OR keep2@example.invalid",
                  "subject": "{keep message}", "label": "Type/Test"}
        new_other = {"from": "new1@example.invalid OR new2@example.invalid",
                     "subject": "new  reminder", "label": "Type/Test"}
        for scenario in ("retained_trash", "created_trash", "declined_trash"):
            base, master = root / (scenario + "-base.xml"), root / (scenario + "-master.xml")
            before_word = "phishing" if scenario == "retained_trash" else "oldphishing"
            old_trash = {"hasTheWord": "(in:spam {" + before_word + "})", "shouldTrash": "true"}
            new_trash = {"hasTheWord": "(in:spam {phishing})", "shouldTrash": "true"}
            write_xml(base, [common, old_trash, {"from": "obsolete@example.invalid", "label": "Type/Test"}])
            write_xml(master, [common, new_trash, new_other])
            baseline, target = old.compile_xml(base), old.compile_xml(master)
            log = root / (scenario + ".json")
            client = BatchClient([], log)
            client.id_to_label = {"Label_1": "Type/Test"}
            labels = {"Type/Test": "Label_1"}
            client.filters = {f"filter_{i}": {"id": f"filter_{i}", **old.to_api(row, labels)}
                              for i, row in enumerate(baseline, 1)}
            client.n = len(baseline)
            rows, _ = old.api_fetch(client)
            plan = old.make_plan(None, master, baseline, "v6@example.invalid", labels, live_rows=rows)
            if scenario != "retained_trash":
                groups = ["other"] if scenario == "declined_trash" else ["other", "trash"]
                plan = old.select_plan_groups(plan, groups, current=rows)
            effective = old.planned_states(plan)[1]
            assert old.perform(client, plan, plan["account"], plan["approval_digest"],
                               log, batch_size=2, max_batches=1) is False
            journal = json.loads(log.read_text())
            # Finalize portability with the old hash function before freezing the artifact.
            plan["target_path"] = "/synthetic-v6/" + scenario + "/master.xml"
            plan["approval_digest"] = old.json_hash({k: v for k, v in plan.items()
                                                     if k not in {"created_utc", "approval_digest"}})
            journal["plan_digest"] = plan["approval_digest"]
            result["cases"][scenario] = {"master_xml": master.read_text(),
                "baseline_templates": baseline, "target_templates": target,
                "plan": plan, "journal": journal, "api_filters": list(client.filters.values()),
                "id_to_label": client.id_to_label, "next_id": client.n, "effective_target": effective,
                "expected_remaining_payloads": [old.to_api(row, labels) for row in plan["add"]]}
        return result


if __name__ == "__main__":
    Path(__file__).with_name("spam-wrappers-v6.json").write_text(
        json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
