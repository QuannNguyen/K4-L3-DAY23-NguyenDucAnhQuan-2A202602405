# Region Failover Runbook

Use this from the repository root. The serving processes for regions A and B and
the edge proxy must already be running. The normal command asks the operator to
confirm before it performs failover.

**Important:** Step 2 runs the automated failover and all verification steps. Steps
3-7 below are read-only checks afterward; do not invoke `dr/failover.py` a second
time after the runbook has completed.

| # | Step and copy-paste command | Know it is complete when | Owner | Stop/rollback condition |
|---|---|---|---|---|
| 1 | Check both regions: `python chaos/kill_region.py status` | Primary A is unavailable and target B is alive; confirm the outage with repeated checks, not a single failure. | On-call | If B is not alive, stop. Restore or escalate B before attempting failover. |
| 2 | Confirm and execute: `python dr/runbook.py --primary a --target b --backend fs` | Confirm `y` when prompted. The runbook completes and prints an `ok: true` result; its event log records all seven steps. | Incident commander | Answer `n` if the outage is unconfirmed. If failover returns `ok: false`, leave the active region unchanged and escalate. |
| 3 | Verify restore and readiness sequence: `Get-Content reports\failover-events.jsonl -Tail 5` (PowerShell) | Events are ordered `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready`, `5_dns_cutover`; restore records RPO, documents lost, and model version. | Storage / serving on-call | If restore fails or step 4 is not ready, do not cut over. Preserve the primary and escalate. |
| 4 | Check readiness: `curl.exe http://127.0.0.1:8002/readyz` | HTTP 200 with `ready: true`, pool full, weights present, and vectors present. | Serving on-call | If readiness is not HTTP 200, keep traffic off B; failover must abort before DNS cutover. |
| 5 | Check routing: `curl.exe http://127.0.0.1:8080/edge/state` | `active_region` is `b`; event log contains `5_dns_cutover`. | Network on-call | If B is not ready or user traffic fails, the incident commander may approve failback only after A is restored and verified. |
| 6 | Review golden signals: `Get-Content reports\runbook-run.jsonl -Tail 2` (PowerShell) | `verify_golden_signals` shows 10 requests, 10 successes, and error rate below 1%. | Service owner | If requests fail or exceed the error budget, alert the incident commander and consider rollback only to a verified-ready A. |
| 7 | Measure and retain evidence: `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output has `valid: true`, no warnings, and `rto_verdict: PASS`; copy measured RTO/RPO into the reports. | Incident commander | If invalid, warnings are present, or RTO fails the target, retain logs and open follow-up actions; do not claim a passing recovery. |

## Rollback and escalation

The failover implementation must not change `edge/active_region` unless B reports
ready. If B does not become ready before the timeout, keep traffic on the existing
route and escalate; never force a cutover to an unready region.

Fail back to A only after A is restored, its data and model are verified, `/readyz`
returns HTTP 200, and the incident commander approves. Confirm replication direction
and data freshness first to avoid losing writes or triggering a failback loop.

## Windows note

Bare `--mode stop` is supported on Windows. Bare `--mode netblock` needs POSIX
`SIGSTOP` and must be run under Linux or Git Bash. The recorded drill used `stop`.
