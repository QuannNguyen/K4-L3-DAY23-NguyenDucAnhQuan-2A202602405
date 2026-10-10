"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append and print one JSONL record with epoch and UTC timestamps."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {
        "ts": now,
        "iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print("FAILOVER", json.dumps(record))
    return record


def state_of(region: str) -> dict:
    try:
        response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        return {"region": region, "error": f"{type(exc).__name__}: {exc}"}


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm, and cut over only after the target reports ready."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unknown snapshot backend: {backend}")
    if wait < 0:
        raise ValueError("wait must not be negative")

    emit(step="1_verify_target", target=target, state=state_of(target))

    restored = snapshot.get(target, backend)
    primary = restored.get("source_region", "a")
    if primary == target:
        primary = "a" if target == "b" else "b"
    rpo = snapshot.rpo(
        pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
        pathlib.Path(f"state/region-{target}/vectors.sqlite"),
    )
    emit(
        step="2_restore_snapshot",
        target=target,
        **restored,
        **rpo,
    )

    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full\n", encoding="utf-8")
    emit(step="3_scale_pool", target=target, pool_state="full")

    started = time.monotonic()
    deadline = started + wait
    last_status = None
    last_error = None
    while True:
        remaining = deadline - time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=max(0.01, min(1.0, remaining)) if remaining > 0 else 0.01,
            )
            last_status = response.status_code
            last_error = None
            if response.status_code == 200:
                waited = round(time.monotonic() - started, 3)
                emit(
                    step="4_wait_ready",
                    target=target,
                    ready=True,
                    status_code=response.status_code,
                    waited_s=waited,
                )
                break
        except httpx.RequestError as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if time.monotonic() >= deadline:
            waited = round(time.monotonic() - started, 3)
            emit(
                step="4_wait_ready",
                target=target,
                ready=False,
                status_code=last_status,
                error=last_error,
                waited_s=waited,
            )
            return {
                "ok": False,
                "target": target,
                "reason": "target did not become ready before timeout",
                "waited_s": waited,
                "rpo_seconds": rpo.get("rpo_seconds"),
                "docs_lost": rpo.get("docs_lost"),
            }
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(target + "\n", encoding="utf-8")
    emit(step="5_dns_cutover", target=target, active_region=target)
    return {
        "ok": True,
        "target": target,
        "rpo_seconds": rpo.get("rpo_seconds"),
        "docs_lost": rpo.get("docs_lost"),
        "embed_model_version": restored.get("embed_model_version"),
        "waited_s": waited,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
