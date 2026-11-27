# Reusing fixed-plan read evidence must not imply write ownership

- ID: message-reconciliation-checkpoint
- Status: verified
- Scope: `gmail_message_reclassifier.py`, fixed-plan preflight, postapproval and final reconciliation
- Trigger / search terms: Issue #31, interrupted reconciliation, history expiry, repeated events, pending intent
- Last verified: 2026-11-28, synthetic regressions with the Google SDK installed and network disabled
- Evidence: [regression tests](../../../tests/test_reconciliation_checkpoint.py), [canonical specification](../../issue-6-message-reclassification.md#固定計画の照合再開issue-31)

## Observation and cause

An interrupted full scan previously lost its read progress, and interactive approval always caused another full scan. A saved offset alone would miss changes to already scanned messages. Reusing a set difference of changed IDs would also miss a second event for an ID seen in an earlier interval.

There are two separate durable states: evidence that metadata was checked, and the journal's resolution of an owned pending write. If the read evidence is committed first, a crash can retain a checked ID while rolling back the corresponding journal transition. Skipping that ID on resume would then fail to reconstruct its resolved progress.

## Next-time action

- Keep disposable read evidence separate from the immutable plan, approval records and write intents; bind it to plan digest, mode and journal state.
- Invalidate changed IDs and advance the cursor in one transaction. Capture a fresh cursor before each scan and inspect that entire interval afterward, including repeat events for the same ID.
- Commit a resolved journal transition before saving that ID as checked. If journal state differs on resume, discard the old read evidence.
- Treat an expired history cursor as requiring full reads. Other history failures and invalid responses must stop, never stand in for an empty delta.
- Discard reusable evidence before new write intents; retain fresh prewrite, readback and final checks and invocation-specific approval.

## Limits

These are synthetic safety and request-count results, not production timing or a guarantee of atomic Gmail state. Planning checkpoint v2 has different rules; use [message-planning-consistency](message-planning-consistency.md) for unfinished plans. Issue #35 extends history validation to interrupted final scans. Use final-stage evidence across renewed approval only when the journal has no remaining writes. Arbitrary Gmail queries may depend on time without a history event, so drift checks relying on them need fresh evaluation. See the [system drift regressions](../../../tests/test_message_system_drift.py).
