"""Regenerate synthetic v3/v4 evidence with the pre-normalization implementation.

Run from the repository root: python3 tests/fixtures/generate_condition_equivalence_legacy.py
Requires only local Git history and the standard library. No credentials or network.
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
REVISION = "f2240c9b662fbc4c510ac3f353540f133b8d1c00"
sys.path.insert(0, str(ROOT / "tests"))
from test_deployer import write_xml
from test_batch import BatchClient


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        """Return a fixed instant in the requested zone for reproducible plan and journal timestamps."""
        return datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc).astimezone(tz)


def generate():
    """Use the old implementation to freeze synthetic v3/v4 plans after one confirmed batch."""
    with tempfile.TemporaryDirectory() as name, redirect_stdout(StringIO()):
        root = Path(name)
        source = root / "legacy.py"
        source.write_bytes(subprocess.check_output(
            ["git", "show", REVISION + ":gmail_filter_deployer.py"], cwd=ROOT))
        spec = importlib.util.spec_from_file_location("legacy_fixture_source", source)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        old.datetime = FrozenDatetime
        master, base = root / "master.xml", root / "base.xml"
        common = {"hasTheWord": "from:a@example.invalid OR from:b@example.invalid",
                  "subject": "meeting  reminder", "label": "Type/Test"}
        write_xml(base, [common, {"from": "obsolete@example.invalid", "label": "Type/Test"}])
        write_xml(master, [common, {"from": "new@example.invalid", "label": "Type/Test"},
            {"hasTheWord": "from:c@example.invalid OR from:d@example.invalid",
             "subject": "new  reminder", "label": "Type/Test"}])
        baseline, target = old.compile_xml(base), old.compile_xml(master)
        result = {"generated_from": REVISION, "master_xml": master.read_text(),
                  "baseline_templates": baseline, "target_templates": target, "cases": {}}
        for version in ("v3", "v4"):
            log = root / (version + ".json")
            client = BatchClient([], log)
            client.id_to_label = {"Label_1": "Type/Test"}
            labels = {"Type/Test": "Label_1"}
            client.filters = {f"filter_{i}": {"id": f"filter_{i}", **old.to_api(row, labels)}
                              for i, row in enumerate(baseline, 1)}
            client.n = len(baseline)
            plan = old.make_plan(None, master, baseline, "legacy@example.invalid", labels)
            if version == "v4":
                plan = old.select_plan_groups(plan, ["other"], current=old.api_fetch(client)[0])
            assert old.perform(client, plan, plan["account"], plan["approval_digest"],
                               log, batch_size=2, max_batches=1) is False
            journal = json.loads(log.read_text())
            # Make the source location portable before freezing the old digest.
            plan["target_path"] = "/synthetic-legacy/master.xml"
            plan["approval_digest"] = old.json_hash({k: v for k, v in plan.items()
                                                     if k not in {"created_utc", "approval_digest"}})
            journal["plan_digest"] = plan["approval_digest"]
            result["cases"][version] = {"plan": plan, "journal": journal,
                "api_filters": list(client.filters.values()), "id_to_label": client.id_to_label,
                "next_id": client.n,
                "expected_remaining_payloads": [old.to_api(row, labels) for row in plan["add"]]}
        return result


if __name__ == "__main__":
    Path(__file__).with_name("condition-equivalence-legacy.json").write_text(
        json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
