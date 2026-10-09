"""Edge proxy = lớp "DNS / Global LB" giả lập. [CÓ SẴN — không sửa]

Không cần Route53: "DNS record" ở đây là file text edge/active_region chứa "a" hoặc "b".
Proxy đọc lại file này ở MỖI request -> cutover = ghi 1 byte, không cần restart.
TTL giả lập bằng EDGE_TTL_SECONDS (mặc định 5s) để sinh viên thấy §2 "DNS cache
không tôn trọng TTL -> cộng thêm giây vào RTO".
"""
import os
import pathlib
import threading
import time

import httpx
from fastapi import FastAPI, Response

ACTIVE_FILE = pathlib.Path(os.environ.get("ACTIVE_REGION_FILE", "edge/active_region"))
TTL = float(os.environ.get("EDGE_TTL_SECONDS", "5"))
TIMEOUT = float(os.environ.get("EDGE_TIMEOUT_SECONDS", "2"))
MODE = os.environ.get("EDGE_MODE", "active_passive")
UPSTREAM = {"a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
            "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002")}

app = FastAPI(title="edge-proxy")
_cache = {"region": None, "at": 0.0}
_turn = {"n": 0}
_turn_lock = threading.Lock()


def resolve() -> str:
    """Giả lập DNS cache: chỉ đọc lại file sau khi TTL hết hạn."""
    now = time.time()
    if _cache["region"] is None or now - _cache["at"] >= TTL:
        _cache["region"] = ACTIVE_FILE.read_text().strip() if ACTIVE_FILE.exists() else "a"
        _cache["at"] = now
    return _cache["region"]


def choose_region() -> str:
    if MODE != "active_active":
        return resolve()
    with _turn_lock:
        region = "ab"[_turn["n"] % 2]
        _turn["n"] += 1
    return region


@app.get("/v1/infer")
def infer(q: str = "hoa don thang 7", response: Response = None):
    region = choose_region()
    t0 = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(f"{UPSTREAM[region]}/v1/infer", params={"q": q})
        response.status_code = r.status_code
        return {"edge_region": region, "upstream_status": r.status_code,
                "edge_latency_ms": round((time.time() - t0) * 1000, 1), **r.json()}
    except Exception as e:  # region chết: refused (stop) hoac timeout (netblock)
        response.status_code = 503
        return {"edge_region": region, "upstream_status": None,
                "error": type(e).__name__,
                "edge_latency_ms": round((time.time() - t0) * 1000, 1)}


@app.get("/edge/state")
def edge_state():
    if MODE == "active_active":
        return {"mode": MODE, "regions": ["a", "b"], "split": "round_robin_50_50"}
    return {"active_region": resolve(), "ttl_seconds": TTL,
            "cache_age_s": round(time.time() - _cache["at"], 2)}
