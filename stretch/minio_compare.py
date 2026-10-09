"""Compare filesystem and real MinIO snapshots, then fail over via MinIO."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import boto3
import httpx
from botocore.exceptions import ClientError


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DR_BUCKET"] = "dr-artifacts-stretch"
from dr import failover  # noqa: E402
from state import seed_vectors, snapshot  # noqa: E402


def wait_health(url, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(url)


def measured(fn, *args):
    started = time.perf_counter()
    value = fn(*args)
    return round((time.perf_counter() - started) * 1000, 2), value


def main():
    endpoint = os.environ.get("MINIO_URL", "http://day23-minio:9000")
    os.environ["MINIO_URL"] = endpoint
    client = boto3.client("s3", endpoint_url=endpoint,
                          aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin")
    try:
        client.head_bucket(Bucket=snapshot.BUCKET)
    except ClientError:
        client.create_bucket(Bucket=snapshot.BUCKET)

    old_cwd = pathlib.Path.cwd()
    with tempfile.TemporaryDirectory(prefix="day23-minio-") as temp:
        work = pathlib.Path(temp)
        for name in ("serving", "edge", "state", "dr"):
            shutil.copytree(ROOT / name, work / name,
                            ignore=shutil.ignore_patterns("__pycache__", "region-a", "region-b",
                                                          "_replica", "active_region"))
        (work / "reports").mkdir()
        os.chdir(work)
        processes = []
        logs = []
        try:
            seed_vectors.seed("a", 200, 2)
            seed_vectors.seed("b", 0, 0)
            pathlib.Path("edge/active_region").write_text("a")
            for region, port in (("a", 8001), ("b", 8002)):
                env = dict(os.environ, REGION=region, STATE_DIR=f"state/region-{region}", WARMUP_SECONDS="2")
                log = open(work / f"region-{region}.log", "w", encoding="utf-8")
                logs.append(log)
                processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "serving.app:app",
                                                   "--host", "127.0.0.1", "--port", str(port),
                                                   "--log-level", "warning"], env=env, stdout=log, stderr=log))
            edge_log = open(work / "edge.log", "w", encoding="utf-8")
            logs.append(edge_log)
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "edge.proxy:app",
                                               "--host", "127.0.0.1", "--port", "8080",
                                               "--log-level", "warning"],
                                              env=dict(os.environ, EDGE_TTL_SECONDS="1"),
                                              stdout=edge_log, stderr=edge_log))
            for port in (8001, 8002):
                wait_health(f"http://127.0.0.1:{port}/healthz")
            wait_health("http://127.0.0.1:8080/edge/state")

            fs_put, _ = measured(snapshot.put, "a", "fs")
            fs_get, _ = measured(snapshot.get, "b", "fs")
            s3_put, _ = measured(snapshot.put, "a", "minio")
            s3_get, meta = measured(snapshot.get, "b", "minio")
            result = failover.failover("b", "minio", wait=15)
            if not result["ok"]:
                raise RuntimeError(result["error"])
            time.sleep(1.1)
            responses = [httpx.get("http://127.0.0.1:8080/v1/infer", timeout=3) for _ in range(10)]
            served = [response.json().get("region") for response in responses]
            if any(response.status_code != 200 for response in responses) or set(served) != {"b"}:
                raise AssertionError(f"cutover failed: {served}")
            data = {"backend": "real_minio", "fs_put_ms": fs_put, "fs_get_ms": fs_get,
                    "minio_put_ms": s3_put, "minio_get_ms": s3_get,
                    "embed_model_version": meta["embed_model_version"],
                    "failover_ok": result["ok"], "served_by": "b", "verified_requests": 10}
            output = ROOT / "reports/stretch/minio.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(data, indent=2))
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            for log in logs:
                log.close()
            os.chdir(old_cwd)


if __name__ == "__main__":
    main()
