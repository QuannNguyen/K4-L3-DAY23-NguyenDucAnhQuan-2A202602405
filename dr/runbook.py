"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append and print one timestamped runbook event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {
        "ts": now,
        "iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print("RUNBOOK", json.dumps(record))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Automatically confirm in CI, otherwise ask the operator for y/N."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def _outage_timestamp(region: str) -> float | None:
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    matching = []
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("action") == "kill" and record.get("region") == region:
            matching.append(record["ts"])
    return matching[-1] if matching else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, operator-confirmed regional failover runbook."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be region 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    started = time.monotonic()
    primary_ready, primary_reason = probe(primary, timeout=0.5)
    target_state = fo.state_of(target)
    target_alive = "error" not in target_state
    outage_confirmed = not primary_ready and target_alive
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=primary_ready,
        primary_reason=primary_reason,
        target=target,
        target_alive=target_alive,
        target_state=target_state,
        outage_confirmed=outage_confirmed,
    )
    if not outage_confirmed:
        result = {
            "ok": False,
            "reason": "primary is not confirmed down or target process is unavailable",
        }
        step(7, "post_incident", **result, elapsed_s=round(time.monotonic() - started, 3))
        return result

    if not confirm(auto, f"Region {primary} is unavailable. Fail over to {target}?"):
        result = {"ok": False, "reason": "operator declined failover"}
        step(2, "thong_bao_incident", confirmed=False, primary=primary, target=target)
        step(7, "post_incident", **result, elapsed_s=round(time.monotonic() - started, 3))
        return result

    outage_ts = _outage_timestamp(primary)
    step(
        2,
        "thong_bao_incident",
        confirmed=True,
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        notification_delay_s=None if outage_ts is None else round(time.time() - outage_ts, 3),
    )
    failover_result = fo.failover(target, backend, wait=60.0)
    replica_state = fo.state_of(target) if failover_result.get("ok") else {}
    step(
        3,
        "scale_gpu_pool",
        failover_ok=failover_result.get("ok", False),
        failover_result=failover_result,
    )
    step(
        4,
        "verify_state_replica",
        state=replica_state,
        rpo_seconds=failover_result.get("rpo_seconds"),
        docs_lost=failover_result.get("docs_lost"),
        embed_model_version=failover_result.get("embed_model_version"),
    )
    step(
        5,
        "dns_cutover",
        ok=failover_result.get("ok", False),
        active_region=target if failover_result.get("ok") else None,
    )

    samples = []
    if failover_result.get("ok"):
        for _ in range(10):
            request_started = time.monotonic()
            try:
                response = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0)
                samples.append({
                    "ok": response.status_code == 200,
                    "status_code": response.status_code,
                    "latency_ms": round((time.monotonic() - request_started) * 1000, 1),
                })
            except httpx.RequestError as exc:
                samples.append({
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": round((time.monotonic() - request_started) * 1000, 1),
                })
    successful = sum(sample["ok"] for sample in samples)
    latencies = sorted(sample["latency_ms"] for sample in samples)
    p95_index = min(len(latencies) - 1, int(len(latencies) * 0.95)) if latencies else None
    step(
        6,
        "verify_golden_signals",
        requests=len(samples),
        successful=successful,
        error_rate=0.0 if not samples else (len(samples) - successful) / len(samples),
        p95_latency_ms=None if p95_index is None else latencies[p95_index],
        samples=samples,
    )

    result = {
        **failover_result,
        "golden_signal_requests": len(samples),
        "golden_signal_successful": successful,
    }
    step(
        7,
        "post_incident",
        **result,
        elapsed_s=round(time.monotonic() - started, 3),
        measurement_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl",
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
