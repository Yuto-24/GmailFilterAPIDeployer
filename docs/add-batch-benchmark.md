# Filter ADD batch experiment (#12)

This is a separate, explicitly authorized live experiment, not a deployment.
It uses the one-send transport from #9. The reconciliation interval decision
in #11 does not change this experiment or the current deployment implementation.

Do not edit Gmail filters or labels through another client during a trial.
The script holds `local/run.lock`, pins the account using `GMAIL_EXPECTED_EMAIL`,
and refuses an existing deployment or unfinished trial. Each temporary rule
requires and excludes the same unique Message-ID predicate, with STARRED as
the action. It cannot match a message. Existing filters and Master XML are
never selected for writes.

Build the current image first, then run the sequential baseline:

```bash
docker compose build deployer
docker compose run --rm --no-deps --entrypoint python \
  -v "$PWD/scripts:/app/scripts:ro" deployer \
  /app/scripts/benchmark_filter_add.py --batch-size 1 --count 2
```

After the summary says `restored: true`, run the same command with
`--batch-size 2 --count 2`. Compare `send_s` per applied filter separately
from `readbacks_s` and cleanup. Increase to `--batch-size 5 --count 5`, then
10 only if every request is acknowledged and confirmed, there are no errors,
restoration succeeds, and per-item send time improves. Stop on unreliable or
slower results. These are small samples, not a general throughput guarantee.

Each create intent and response, cleanup intent and response, full baseline,
and readback timings are saved privately in `local/add_batch_trial_pending.json`.
Completed evidence is archived under `local/add_batch_trials/`. Do not upload
these files: they include the account and existing filter settings. The stdout
summary excludes those fields and filter IDs.

If interrupted or cleanup fails, run the same command with `--cleanup` only.
Recovery reads the actual state before deciding which owned IDs still exist;
it never sends additional creates. Unexpected drift stops cleanup for inspection.
Do not remove or edit a pending journal to bypass the check. Readback errors
leave the journal intact for a later cleanup attempt. Complete restoration
means the baseline IDs, criteria, actions and label metadata match exactly.

Offline verification:

```bash
python3 -m unittest discover -s tests -p test_add_benchmark.py -v
```
