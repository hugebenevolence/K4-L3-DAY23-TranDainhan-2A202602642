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

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    event = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    return event


def confirm(auto: bool, msg: str) -> bool:
    return auto or input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different valid regions")
    started = time.time()
    failed = 0
    for _ in range(3):
        ready, reason = probe(primary, 2)
        if ready:
            failed = 0
        else:
            failed += 1
        if failed < 3:
            time.sleep(0.5)
    target_ready, target_reason = probe(target, 2)
    step(1, "xac_nhan_outage", primary=primary, consecutive_fails=failed,
         reason=reason, target_ready=target_ready, target_reason=target_reason)
    if failed < 3:
        return {"ok": False, "error": f"region-{primary} outage not confirmed"}
    if not confirm(auto, f"Fail over region-{primary} to region-{target}?"):
        return {"ok": False, "error": "operator declined cutover"}

    outage_ts = _latest_outage_ts(primary)
    if outage_ts is not None and pathlib.Path("reports/health-events.jsonl").exists():
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in pathlib.Path("reports/health-events.jsonl").read_text().splitlines()]
            if any(e.get("region") == primary and e.get("to") == "UNHEALTHY"
                   and e.get("ts", 0) >= outage_ts for e in events):
                break
            time.sleep(0.5)

    incident = step(2, "thong_bao_incident", primary=primary,
                    outage_ts=outage_ts, operator_notified_ts=time.time())
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=result["ok"], target=target)
    state = result.get("state", {})
    step(4, "verify_state_replica", count=state.get("count"), weights=state.get("weights"),
         embed_model_version=result.get("embed_model_version"), docs_lost=result.get("docs_lost"))
    step(5, "dns_cutover", ok=result["ok"], active_region=result.get("active_region"))
    if not result["ok"]:
        step(6, "verify_golden_signals", ok=False, error=result.get("error"))
        step(7, "post_incident", elapsed_s=round(time.time() - started, 2),
             outcome="failover_aborted", measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl")
        return result

    latencies = []
    errors = 0
    for _ in range(10):
        t = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3)
            errors += response.status_code != 200
        except httpx.HTTPError:
            errors += 1
        latencies.append((time.monotonic() - t) * 1000)
    p95 = round(sorted(latencies)[9], 1)
    error_rate = errors / 10
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95,
         error_rate=error_rate, ok=errors == 0)
    step(7, "post_incident", elapsed_s=round(time.time() - started, 2),
         outcome="cutover_complete", incident_ts=incident["ts"],
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl")
    result.update(p95_latency_ms=p95, error_rate=error_rate)
    return result


def _latest_outage_ts(region: str):
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if path.exists():
        for line in reversed(path.read_text().splitlines()):
            event = json.loads(line)
            if event.get("action") == "kill" and event.get("region") == region:
                return event["ts"]
    return None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
