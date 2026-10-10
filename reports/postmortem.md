# Postmortem - DR Drill Lab 23

This was a controlled local drill. The goal is to improve the system, not assign
blame. Timestamps and counts below come from the recorded run.

## 1. Timeline

| Time (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-10T12:17:35 | Region A stopped; Region B was alive | `chaos/chaos-events.jsonl:2` |
| 2026-10-10T12:17:36 | First failed user request after the outage | `reports/drill-2-withdr.jsonl:3` |
| 2026-10-10T12:17:36 | Health checker marked Region A unhealthy | `reports/health-events.jsonl:2` |
| 2026-10-10T12:17:38 | Operator-confirmed runbook started failover | `reports/runbook-run.jsonl:2` |
| 2026-10-10T12:17:38 | Snapshot restore completed; RPO recorded | `reports/failover-events.jsonl:2` |
| 2026-10-10T12:17:39 | Region B became ready | `reports/failover-events.jsonl:4` |
| 2026-10-10T12:17:39 | DNS cutover completed | `reports/failover-events.jsonl:5` |
| 2026-10-10T12:17:41 | First successful inference request from Region B | `reports/drill-2-withdr.jsonl:6` |

## 2. RTO/RPO and gap analysis against target

- RTO target: 300s; measured RTO: 5.8s; gap to target: 294.2s below the limit.
- RPO target: 300s; measured RPO: 7.21s; 10 documents were absent from the restored snapshot (gap to target: 292.79s below the limit).
- The longest failover stage was target readiness, including 1.094s of pool warm-up.
- Evidence summary: `reports/measure-drill-2.json`.

## 3. Root cause (five whys)

1. Inference requests failed because Region A stopped serving.
2. The edge continued routing to A until the automated failover completed.
3. Region B could not serve immediately because its data, model weights, and pool
   needed restore and warm-up.
4. The snapshot was behind the primary because replication was not continuous.
5. The runbook therefore depends on monitored replication cadence and a measured,
   ready-before-cutover sequence; shortening detection alone would not remove the
   restore and warm-up portions of RTO.

## 4. Action items

| # | Action | Owner | Deadline | Expected impact |
|---|---|---|---|---|
| 1 | Alert on replication age and snapshot failures; rehearse recovery monthly | Storage on-call | 2026-11-10 | Reduce the chance of an unbounded RPO |
| 2 | Measure ready-pool warm-up at production load and tune the warm standby size | Serving on-call | 2026-11-10 | Reduce or better predict the 1.1s pool stage |

## 5. Required questions

1. The configured detection floor is `0.2s x 2 = 0.4s`; this is 0.13% of the
   300s target. The observed detection delay was 1.2s.
2. A 1s polling interval could lower the theoretical floor, but polling more
   frequently increases probe load and can make transient failures/flapping more
   likely. Keep the consecutive-failure threshold and validate under load.
3. If the primary region is permanently lost, `docs_lost=10` means ten customer
   documents committed after the restored snapshot are unavailable and may need
   reconstruction or customer communication.
