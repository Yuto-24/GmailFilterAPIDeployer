#!/usr/bin/env python3
"""Compare real perform() paths offline with 88 fake writes and a controlled clock.

Every fake Gmail API request advances the clock by exactly one second. No real
sleep, credentials, Gmail client, or network is used. The reported deployment
time models API waiting only, not CPU, filesystem, OAuth, or human input time.
Run from a checkout that includes tests/, which supplies synthetic resources.
"""
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import gmail_filter_deployer as d
from test_deployer import FakeClient, FakeFilters, FakeLabels, Req, write_xml


def run_trial(interval):
    """Deploy identical synthetic rules and return measured counts and state hashes."""
    with tempfile.TemporaryDirectory() as directory:
        local = Path(directory)
        baseline, target = local / "base.xml", local / "target.xml"
        common = [{"from": "keep@example.invalid", "label": "Type/Test"}]
        write_xml(baseline, common + [
            {"from": f"old{i}@example.invalid", "label": "Type/Test"} for i in range(45)])
        write_xml(target, common + [
            {"from": f"new{i}@example.invalid", "label": "Type/Test"} for i in range(43)])
        base = d.compile_xml(baseline)
        client = FakeClient(base)
        _, labels = d.api_fetch(client)
        plan = d.make_plan(None, target, base, "test@example.invalid", labels)
        calls = Counter()
        mutations = []
        seconds = 0.0

        def wrap(resource, method, kind):
            """Advance time at execution, preserving each synthetic API operation."""
            original = getattr(resource, method)

            def request(instance, **kwargs):
                """Build a fake request without counting unexecuted requests."""
                underlying = original(instance, **kwargs)

                def execute():
                    """Count one API attempt and apply its original fixture behavior."""
                    nonlocal seconds
                    calls[kind] += 1
                    seconds += 1.0
                    if method != "list":
                        mutations.append({"api": kind, "request": kwargs})
                    return underlying.execute(num_retries=0)

                return Req(execute)

            return request

        with ExitStack() as stack:
            for resource, method, kind in (
                (FakeLabels, "list", "labels.list"),
                (FakeLabels, "create", "labels.create"),
                (FakeFilters, "list", "filters.list"),
                (FakeFilters, "create", "filters.create"),
                (FakeFilters, "delete", "filters.delete"),
            ):
                stack.enter_context(patch.object(resource, method, wrap(resource, method, kind)))
            stack.enter_context(patch.object(d.time, "perf_counter", lambda: seconds))
            stack.enter_context(redirect_stdout(StringIO()))
            with d.observe_command("apply") as observation:
                d.start_phase("synthetic deployment", deployment=True)
                result = d.perform(client, plan, plan["account"], plan["approval_digest"],
                                   local / "journal.json", readback_interval=interval)
        return {
            "readback_interval": interval,
            "api_attempts": dict(sorted(calls.items())),
            "read_requests": calls["labels.list"] + calls["filters.list"],
            "write_requests": len(mutations),
            "total_requests": sum(calls.values()),
            "controlled_deployment_seconds": observation.deployment_seconds,
            "mutation_trace_sha256": d.json_hash(mutations),
            "final_state_sha256": d.json_hash(result["rows"]),
            "target_matches": d.counter(row["filter"] for row in result["rows"]) ==
                              d.counter(d.compile_xml(target)),
        }


def compare():
    """Compare one-per-operation and 50-operation reconciliation without live API use."""
    trials = [run_trial(interval) for interval in (1, 50)]
    return {
        "measurement": "offline controlled clock; exactly 1 second per synthetic API attempt",
        "scope": "perform() only; excludes fixture setup, OAuth, consent, CPU and filesystem time",
        "trials": trials,
        "same_mutations": trials[0]["mutation_trace_sha256"] == trials[1]["mutation_trace_sha256"],
        "same_final_state": trials[0]["final_state_sha256"] == trials[1]["final_state_sha256"],
    }


if __name__ == "__main__":
    print(json.dumps(compare(), ensure_ascii=False, indent=2))
