# Interrupted message planning must preserve a complete classification snapshot

- ID: message-planning-consistency
- Status: verified
- Scope: `gmail_message_reclassifier.py`, planning checkpoint v2 introduced in PR #30
- Trigger / search terms: planning resume, history cursor, mutable query, deferred count, manifest publication
- Last verified: 2026-11-18, PR #30 review remediation
- Evidence: [PR #30](https://github.com/Yuto-24/GmailFilterAPIDeployer/pull/30), [checkpoint regression tests](../../../tests/test_plan_checkpoint.py)

## Observation and cause

Seven synthetic regressions fail on reviewed commit `4f94a7a`: an expired time-dependent query hit survives resume; a missing message row goes undetected; a label event permits dropping a missing ID; a refreshed query's changed hit does not update that message's desired labels; repeated same-ID events during search and final metadata verification escape detection; and a committed deferred transition followed by interrupted finalization produces a manifest with the wrong count.

A set of IDs changed since the initial cursor cannot establish that a later reconciliation interval was quiet. Gmail history also does not capture passage of time. Separately committed query hits and classifications, or counters recovered from the original pointer, do not describe one consistent snapshot.

## Next-time action

- Keep an explicit disposition for every initial ID and validate target/deferred rows against it on resume. Aggregate final counts from those durable states.
- Re-evaluate queries not proven immutable even without history changes. Reclassify the symmetric difference of old/new hits together with metadata changes. Commit dispositions, hits and classifications together.
- Capture a fresh cursor before reconciliation, and check interval events after the final paced metadata read. A repeated event for an already-known ID still invalidates the pass.
- Validate a published manifest and all backups before completing an interrupted pointer release. Never use this recovery to release approved pending work.

## Limits

Evidence is from the synthetic Gmail API; it does not establish live search-index latency or production performance. A v1 checkpoint lacks per-ID dispositions and cannot be upgraded by guessing missing states. Keep its backups and use the documented explicit reset. Finished immutable plans retain their existing schema.
