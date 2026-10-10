# Measured RTO/RPO Evidence - Lab 23

These values are from the local Windows drill on 2026-10-10. The drill used
two local serving processes, the local edge proxy, filesystem snapshots, and
`stop` mode. Every evidence reference below identifies an existing source line.

## Drill 1 - No disaster recovery

| Measurement | Result | Evidence |
|---|---:|---|
| Outage start | 2026-10-10 12:15:01 UTC | `chaos/chaos-events.jsonl:1` |
| First failed request after outage | +0.2s | `reports/drill-1-nodr.jsonl:6` |
| Failed requests | 17 | `reports/measure-drill-1.json:28` |
| Recovery | No successful recovery during the run | `reports/measure-drill-1.json:25` |
| Verdict | NO_RECOVERY | `reports/measure-drill-1.json:25` |

## Drill 2 - Automated disaster recovery

| Milestone | Offset from outage | Evidence |
|---|---:|---|
| Outage start | 0s | `chaos/chaos-events.jsonl:2` |
| User sees first error | +1.3s | `reports/drill-2-withdr.jsonl:3` |
| Health check detects Region A as unhealthy | +1.2s | `reports/health-events.jsonl:2` |
| Snapshot restore and RPO measurement complete | +3.3s | `reports/failover-events.jsonl:2` |
| Region B becomes ready | +4.3s | `reports/failover-events.jsonl:4` |
| DNS cutover to Region B | +4.4s | `reports/failover-events.jsonl:5` |
| First successful request served by Region B | +5.8s | `reports/drill-2-withdr.jsonl:6` |
| Measured RTO | 5.8s; target 300s; PASS | `reports/measure-drill-2.json:20` |
| Measured RPO | 7.21s; 10 documents lost | `reports/measure-drill-2.json:23` |
| RPO document-loss count | 10 | `reports/measure-drill-2.json:24` |

## RTO breakdown

| Component | Seconds | Evidence |
|---|---:|---|
| Health-check detection (outage to unhealthy event) | 1.2s | `reports/health-events.jsonl:2` |
| Detection to snapshot restore completion | 2.0s | `reports/failover-events.jsonl:2` |
| Pool scale-up and readiness wait | 1.1s (`waited_s=1.094`) | `reports/failover-events.jsonl:4` |
| Target ready to first successful request | 1.5s | `reports/drill-2-withdr.jsonl:6` |
| **Total measured RTO** | **5.8s** | `reports/measure-drill-2.json:20` |

The health checker was configured with `interval_s=0.2` and `threshold=2`, giving
a theoretical detection floor of 0.4s. The observed detection delay was 1.2s.
The restored snapshot records `rpo_seconds=7.21`, `docs_lost=10`, and the embedding
model version in `reports/failover-events.jsonl:2`.
