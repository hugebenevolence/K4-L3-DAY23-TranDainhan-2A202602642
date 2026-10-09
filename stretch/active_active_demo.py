"""Serve 50/50 traffic from two ready regions in an isolated local stack."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import httpx


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from state.seed_vectors import seed  # noqa: E402
from stretch.conflicts import merge  # noqa: E402


def ready(port):
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"port {port}")


def main():
    old_cwd = pathlib.Path.cwd()
    with tempfile.TemporaryDirectory(prefix="day23-active-") as temp:
        work = pathlib.Path(temp)
        for name in ("serving", "edge", "state"):
            shutil.copytree(ROOT / name, work / name,
                            ignore=shutil.ignore_patterns("__pycache__", "region-a", "region-b",
                                                          "_replica", "active_region"))
        os.chdir(work)
        processes = []
        logs = []
        try:
            for region in "ab":
                seed(region, 200, 2)
                pathlib.Path(f"state/region-{region}/pool_state").write_text("full")
            for region, port in (("a", 8001), ("b", 8002)):
                env = dict(os.environ, REGION=region, STATE_DIR=f"state/region-{region}")
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
                                              env=dict(os.environ, EDGE_MODE="active_active"),
                                              stdout=edge_log, stderr=edge_log))
            for port in (8001, 8002):
                ready(port)
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:8080/edge/state", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("edge")
            for port in (8001, 8002):
                assert httpx.get(f"http://127.0.0.1:{port}/readyz").status_code == 200
            counts = {"a": 0, "b": 0}
            for _ in range(20):
                response = httpx.get("http://127.0.0.1:8080/v1/infer", timeout=3)
                if response.status_code != 200:
                    raise AssertionError(response.text)
                counts[response.json()["region"]] += 1
            if counts != {"a": 10, "b": 10}:
                raise AssertionError(counts)
            winner, conflicts = merge([
                {"event_id": "a-7", "clock": 7, "region": "a", "body": "A update"},
                {"event_id": "b-7", "clock": 7, "region": "b", "body": "B update"},
            ])
            result = {"requests": 20, "served_by": counts, "errors": 0,
                      "conflict_winner": winner["event_id"],
                      "retained_conflicts": [event["event_id"] for event in conflicts]}
            output = ROOT / "reports/stretch/active-active.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, indent=2))
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
