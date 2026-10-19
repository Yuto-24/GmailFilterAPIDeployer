"""Synthetic-only tests: never publish personal Gmail filter exports."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gmail_filter_deployer as d

BASE = [
    {"from": "service@example.invalid", "label": "Service/Example"},
    {"from": "service@example.invalid", "label": "Type/Promotion"},
    {"from": "ads@example.invalid", "label": "Action/Delete"},
    {"hasTheWord": "in:spam {phishing}", "shouldTrash": "true"},
]
TARGET = [
    BASE[0],
    {"from": "service@example.invalid", "subject": "{sale coupon}", "label": "Type/Promotion"},
    BASE[2], BASE[3],
    {"from": "new@example.invalid", "label": "Service/New"},
]

def write_xml(path, rows):
    ET.register_namespace("", d.ATOM)
    ET.register_namespace("apps", d.APPS)
    feed = ET.Element(f"{{{d.ATOM}}}feed")
    for props in rows:
        entry = ET.SubElement(feed, f"{{{d.ATOM}}}entry")
        for name, value in props.items():
            ET.SubElement(entry, f"{{{d.APPS}}}property", {"name": name, "value": value})
    ET.ElementTree(feed).write(path, encoding="utf-8", xml_declaration=True)

class Req:
    def __init__(self, fn):
        self.fn = fn
    def execute(self):
        return self.fn()

class FakeLabels:
    def __init__(self, parent):
        self.p = parent
    def list(self, userId):
        return Req(lambda: {"labels": [{"id": k, "name": v, "type": "user"} for k, v in self.p.id_to_label.items()]})
    def create(self, userId, body):
        def act():
            key = f"Label_{len(self.p.id_to_label) + 1}"
            self.p.id_to_label[key] = body["name"]
            return {"id": key, "name": body["name"]}
        return Req(act)

class FakeFilters:
    def __init__(self, parent):
        self.p = parent
    def list(self, userId):
        return Req(lambda: {"filter": list(self.p.filters.values())})
    def create(self, userId, body):
        def act():
            self.p.n += 1
            key = f"filter_{self.p.n}"
            self.p.filters[key] = {"id": key, **body}
            return {"id": key}
        return Req(act)
    def delete(self, userId, id):
        def act():
            del self.p.filters[id]
            return {}
        return Req(act)

class FakeUsers:
    def __init__(self, parent):
        self.p = parent
    def labels(self):
        return FakeLabels(self.p)
    def settings(self):
        return self
    def filters(self):
        return FakeFilters(self.p)

class FakeClient:
    def __init__(self, templates):
        names = sorted(set(x[6:] for t in templates for x in t["action"]["addLabelIds"] if x.startswith("label:")))
        self.id_to_label = {f"Label_{i + 1}": x for i, x in enumerate(names)}
        ids = {name: lid for lid, name in self.id_to_label.items()}
        self.filters = {f"filter_{i + 1}": {"id": f"filter_{i + 1}", **d.to_api(t, ids)} for i, t in enumerate(templates)}
        self.n = len(templates)
    def users(self):
        return FakeUsers(self)

class TestDeployer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "baseline.xml"
        self.target = Path(self.temp.name) / "target.xml"
        write_xml(self.base, BASE)
        write_xml(self.target, TARGET)
        self.b = d.compile_xml(self.base)
        self.t = d.compile_xml(self.target)

    def test_compile_delta_and_protection(self):
        self.assertEqual((len(self.b), len(self.t)), (4, 5))
        add, remove = d.delta(self.b, self.t)
        self.assertEqual((len(add), len(remove)), (2, 1))
        d.protect(self.b, self.t)

    def test_compound_from_query(self):
        template = d.build_template({"from": "a@example.invalid OR b@example.invalid", "subject": "{one two}", "label": "Type/Account"})
        self.assertEqual(template["criteria"]["query"], "{from:a@example.invalid from:b@example.invalid} subject:{one two}")

    def test_delete_and_unlabeled_filter_rejection(self):
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{"from": "ads@example.invalid", "label": "Action/Delete", "shouldTrash": "true"}])
        with self.assertRaises(d.DeploymentError):
            d.check_xml_rules([{"from": "all@example.invalid", "shouldNeverSpam": "true"}])
        changed = [dict(x) for x in self.b]
        changed[2]["criteria"] = {"from": "different@example.invalid"}
        with self.assertRaises(d.DeploymentError):
            d.protect(self.b, changed)

    def test_unknown_state_refused(self):
        with self.assertRaises(d.DeploymentError):
            d.make_plan(self.base, self.target, self.b[:-1], "test@example.invalid", {})

    def test_plan_resume_and_end_to_end(self):
        mock = FakeClient(self.b)
        rows, labels = d.api_fetch(mock)
        plan = d.make_plan(self.base, self.target, [x["filter"] for x in rows], "test@example.invalid", labels)
        self.assertEqual((len(plan["add"]), len(plan["remove"])), (2, 1))
        self.assertEqual(d.detect_position(self.b, plan, self.b), (0, 0))
        self.assertEqual(d.detect_position(self.b, plan, self.b + plan["add"][:1]), (1, 0))
        self.assertEqual(d.detect_position(self.b, plan, self.t), (2, 1))
        log = Path(self.temp.name) / "deploy.json"
        d.perform(mock, plan, "test@example.invalid", plan["approval_digest"], log, batch_size=1)
        d.perform(mock, plan, "test@example.invalid", plan["approval_digest"], log, batch_size=1)
        self.assertEqual(d.counter([x["filter"] for x in d.api_fetch(mock)[0]]), d.counter(self.t))
        self.assertTrue(json.loads(log.read_text())["verified"])

if __name__ == "__main__":
    unittest.main()
