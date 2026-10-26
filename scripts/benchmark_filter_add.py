#!/usr/bin/env python3
"""Explicitly authorized, bounded live ADD benchmark; private evidence in local/.

Run with --batch-size 1 --count 2, then --batch-size 2 --count 2.
Only increase to 5/10 after reviewing reliability and per-item timings.
An interrupted trial MUST be recovered with --cleanup before another trial.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gmail_filter_deployer as d


def body(row):
    return {key: row[key] for key in ("criteria", "action")}


def fetch(client):
    started = time.perf_counter()
    rows = client.users().settings().filters().list(userId="me").execute(num_retries=0).get("filter", [])
    mapped = {row["id"]: body(row) for row in rows}
    d.demand(len(mapped) == len(rows), "Duplicate IDs in readback")
    labels = client.users().labels().list(userId="me").execute(num_retries=0).get("labels", [])
    return {"filters": mapped, "labels": sorted(labels, key=lambda row: row["id"])}, time.perf_counter() - started


def test_rules(count):
    result = []
    for _ in range(count):
        query = "rfc822msgid:" + uuid.uuid4().hex + "@benchmark.invalid"
        # The same predicate is required AND excluded: no message can match.
        result.append({"criteria": {"query": query, "negatedQuery": query},
                       "action": {"addLabelIds": ["STARRED"]}})
    return result


def owned_ids(state, current):
    """Require intact baseline and exact, unique trial additions before deletion."""
    base = state["baseline"]
    d.demand(current["labels"] == base["labels"], "Labels changed; stop for inspection")
    d.demand(all(current["filters"].get(fid) == rule for fid, rule in base["filters"].items()),
             "Existing filters changed; stop for inspection")
    allowed = [state["rules"][i] for i in state["attempted"]]
    owned = {}
    for fid, rule in current["filters"].items():
        if fid in base["filters"]:
            continue
        d.demand(rule in allowed, "Unexpected addition; stop for inspection")
        index = state["rules"].index(rule)
        d.demand(index not in owned, "Duplicate trial addition; stop for inspection")
        owned[index] = fid
    return owned


def cleanup(client, state, path):
    current, elapsed = fetch(client)
    state.setdefault("readbacks_s", []).append(elapsed)
    owned = owned_ids(state, current)
    state.setdefault("applied", {str(i): fid for i, fid in owned.items()})
    state.setdefault("cleanup", [])
    d.save_json(path, state)
    filters = client.users().settings().filters()
    for index, fid in sorted(owned.items()):
        # A new readback on --cleanup decides whether an uncertain delete applied.
        entry = {"index": index, "id": fid, "status": "intent"}
        state["cleanup"].append(entry)
        d.save_json(path, state)
        started = time.perf_counter()
        try:
            filters.delete(userId="me", id=fid).execute(num_retries=0)
        except Exception as exc:
            entry.update(status="uncertain", error=d.api_error_details(exc))
            entry["elapsed_s"] = time.perf_counter() - started
            d.save_json(path, state)
            # Never replay here. Preserve the journal for readback-only recovery.
            raise d.DeploymentError("Cleanup response uncertain; rerun with --cleanup") from exc
        entry.update(status="acknowledged", elapsed_s=time.perf_counter() - started)
        d.save_json(path, state)
    current, elapsed = fetch(client)
    state["readbacks_s"].append(elapsed)
    d.demand(current == state["baseline"], "Baseline restoration not confirmed")
    state["restored"] = True
    d.save_json(path, state)


def run_trial(client, account, path, batch_size, count):
    d.demand(not path.exists(), "Pending trial exists; use --cleanup")
    baseline, elapsed = fetch(client)
    d.demand(len(baseline["filters"]) + count <= 1000, "Insufficient filter capacity")
    rules = test_rules(count)
    d.demand(not any(rule in baseline["filters"].values() for rule in rules), "Trial collision")
    state = {"schema": 1, "trial": uuid.uuid4().hex, "account": account,
             "batch_size": batch_size, "count": count, "baseline": baseline,
             "rules": rules, "attempted": [], "responses": {},
             "readbacks_s": [elapsed], "restored": False}
    d.save_json(path, state)
    filters = client.users().settings().filters()

    def callback(index, response, exception):
        state["responses"][index] = ({"error": d.api_error_details(exception)} if exception
                                     else {"id": response.get("id")})
        d.save_json(path, state)

    started = time.perf_counter()
    try:
        if batch_size == 1:
            for i, rule in enumerate(rules):
                state["attempted"].append(i)
                d.save_json(path, state)
                d.execute_filter_single(i, filters.create(userId="me", body=rule), callback)
                if not state["responses"].get(str(i), {}).get("id"):
                    break
        else:
            state["attempted"] = list(range(count))
            d.save_json(path, state)
            requests = [(i, filters.create(userId="me", body=rule)) for i, rule in enumerate(rules)]
            d.execute_filter_batch(client, requests, callback)
    except Exception as exc:
        state["send_error"] = d.api_error_details(exc)
    finally:
        state["send_s"] = time.perf_counter() - started
        d.save_json(path, state)
    cleanup(client, state, path)
    return state


def summary(state):
    applied = state.get("applied", {})
    responses = state["responses"]
    acknowledged = {i: r.get("id") for i, r in responses.items() if r.get("id")}
    confirmed = sum(applied.get(i) == fid for i, fid in acknowledged.items())
    errors = [r["error"]["code"] for r in responses.values() if "error" in r]
    return {"batch_size": state["batch_size"], "requested": state["count"],
            "attempted": len(state["attempted"]), "applied": len(applied),
            "acknowledged_and_confirmed": confirmed,
            "applied_without_success_response": len(applied) - confirmed,
            "send_s": state.get("send_s"), "readbacks_s": state["readbacks_s"],
            "errors": errors, "send_error": state.get("send_error", {}).get("code"),
            "restored": state["restored"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", type=Path, default=Path("/app/local"))
    parser.add_argument("--batch-size", type=int, choices=(1, 2, 5, 10), default=1)
    parser.add_argument("--count", type=int, choices=range(1, 11), default=2)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    d.demand(args.batch_size == 1 or args.count == args.batch_size, "Batch count must equal size")
    local = args.local_dir.resolve()
    os.umask(0o077)
    local.mkdir(parents=True, exist_ok=True)
    path = local / "add_batch_trial_pending.json"
    with (local / "run.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        d.demand(not (local / "run_pending.json").exists(), "Deployment pending; trial refused")
        expected = d.resolve_expected_email(None)
        client, creds = d.get_client(local / "credentials.json", local / "token.json", allow_auth=False)
        d.demand(d.account_email(creds) == expected, "Account mismatch")
        if args.cleanup:
            state = json.loads(path.read_text())
            d.demand(state["account"] == expected and state["schema"] == 1, "Trial account/schema mismatch")
            cleanup(client, state, path)
        else:
            state = run_trial(client, expected, path, args.batch_size, args.count)
        archive = local / "add_batch_trials" / (state["trial"] + ".json")
        d.save_json(archive, state)
        path.unlink()
        print(json.dumps(summary(state), sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (d.DeploymentError, BlockingIOError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(1)
