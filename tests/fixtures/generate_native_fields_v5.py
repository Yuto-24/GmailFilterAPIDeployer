"""Freeze synthetic v5 plans and partial journals with the pre-native-fields module.

Run from the repository root: python3 tests/fixtures/generate_native_fields_v5.py
Uses local Git history and in-memory transport only; no SDK, credentials, or network.
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
REVISION = "7331e42eb49ec5ea7a9d09cd34e77e4576c1573d"
sys.path.insert(0, str(ROOT / "tests"))
from test_deployer import write_xml
from test_batch import BatchClient


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        """Return a stable instant so regenerating the fixture produces identical data."""
        return datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc).astimezone(tz)


def generate():
    """Create old v5 plans, then stop after the first confirmed deletion batch."""
    with tempfile.TemporaryDirectory() as name, redirect_stdout(StringIO()):
        root = Path(name)
        source = root / "v5_source.py"
        source.write_bytes(subprocess.check_output(
            ["git", "show", REVISION + ":gmail_filter_deployer.py"], cwd=ROOT))
        spec = importlib.util.spec_from_file_location("native_fields_fixture_source", source)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        old.datetime = FrozenDatetime
        master, base = root / "master.xml", root / "base.xml"
        common = {"from": "keep1@example.invalid OR keep2@example.invalid",
                  "subject": "{keep message}", "hasTheWord": '"original phrase"', "label": "Type/Test"}
        write_xml(base, [common, {"from": "obsolete@example.invalid", "label": "Type/Test"},
                        {"from": "delete-old@example.invalid", "label": "Action/Delete"}])
        write_xml(master, [common,
            {"from": "a@example.invalid OR b@example.invalid", "subject": "meeting  reminder", "label": "Type/Test"},
            {"subject": "{new message}", "label": "Type/Test"},
            {"from": "delete-new@example.invalid", "label": "Action/Delete"}])
        baseline, target = old.compile_xml(base), old.compile_xml(master)
        result = {"generated_from": REVISION, "master_xml": master.read_text(),
                  "baseline_templates": baseline, "target_templates": target, "cases": {}}
        for selection in ("unselected", "selected_other"):
            log = root / (selection + ".json")
            client = BatchClient([], log)
            client.id_to_label = {"Label_1": "Type/Test", "Label_2": "Action/Delete"}
            labels = {name: lid for lid, name in client.id_to_label.items()}
            client.filters = {f"filter_{i}": {"id": f"filter_{i}", **old.to_api(row, labels)}
                              for i, row in enumerate(baseline, 1)}
            client.n = len(baseline)
            rows, _ = old.api_fetch(client)
            plan = old.make_plan(None, master, baseline, "v5@example.invalid", labels, live_rows=rows)
            if selection == "selected_other":
                plan = old.select_plan_groups(plan, ["other"], current=rows)
            effective_target = old.planned_states(plan)[1]
            assert old.perform(client, plan, plan["account"], plan["approval_digest"],
                               log, batch_size=2, max_batches=1) is False
            journal = json.loads(log.read_text())
            # Finalize a portable path using the old implementation before freezing.
            plan["target_path"] = "/synthetic-v5/master.xml"
            plan["approval_digest"] = old.json_hash({k: v for k, v in plan.items()
                                                     if k not in {"created_utc", "approval_digest"}})
            journal["plan_digest"] = plan["approval_digest"]
            result["cases"][selection] = {"plan": plan, "journal": journal,
                "api_filters": list(client.filters.values()), "id_to_label": client.id_to_label,
                "next_id": client.n, "effective_target": effective_target,
                "expected_remaining_payloads": [old.to_api(row, labels) for row in plan["add"]]}
        return result


if __name__ == "__main__":
    Path(__file__).with_name("native-fields-v5.json").write_text(
        json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
