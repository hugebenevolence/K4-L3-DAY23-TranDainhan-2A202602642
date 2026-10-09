"""Five reproducible, isolated chaos drills with randomized kill timing/mode."""

import json
import os
import pathlib
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

import httpx


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from state.seed_vectors import seed  # noqa: E402
from state.snapshot import put  # noqa: E402
from tools.measure_rto import measure  # noqa: E402

OUT = ROOT / "reports/stretch/randomized-chaos"
DURATION = 36


def wait_health(port):
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"port {port}")


def run_one(index, mode, delay):
    old_cwd = pathlib.Path.cwd()
    with tempfile.TemporaryDirectory(prefix=f"day23-chaos-{index}-") as temp:
        work = pathlib.Path(temp)
        for name in ("serving", "edge", "state", "dr", "chaos", "loadgen", "tools"):
            shutil.copytree(ROOT / name, work / name,
                            ignore=shutil.ignore_patterns("__pycache__", "region-a", "region-b",
                                                          "_replica", "active_region", "*.jsonl"))
        (work / "reports").mkdir()
        (work / "run").mkdir()
        os.chdir(work)
        processes = []
        logs = []
        try:
            seed("a", 200, 2)
            seed("b", 0, 0)
            pathlib.Path("edge/active_region").write_text("a")
            put("a", "fs")
            for region, port in (("a", 8001), ("b", 8002)):
                env = dict(os.environ, REGION=region, STATE_DIR=f"state/region-{region}",
                           WARMUP_SECONDS="2")
                log = open(work / f"region-{region}.log", "w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "serving.app:app",
                                            "--host", "127.0.0.1", "--port", str(port),
                                            "--log-level", "warning"], env=env, stdout=log, stderr=log)
                processes.append(process)
                pathlib.Path(f"run/region-{region}.pid").write_text(str(process.pid))
            edge_log = open(work / "edge.log", "w", encoding="utf-8")
            logs.append(edge_log)
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "edge.proxy:app",
                                               "--host", "127.0.0.1", "--port", "8080",
                                               "--log-level", "warning"],
                                              env=dict(os.environ, EDGE_TTL_SECONDS="3"),
                                              stdout=edge_log, stderr=edge_log))
            for port in (8001, 8002):
                wait_health(port)
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:8080/edge/state", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("edge")

            traffic = subprocess.Popen([sys.executable, "loadgen/traffic.py", "--duration", str(DURATION),
                                        "--rps", "2", "--out", "reports/traffic.jsonl"],
                                       stdout=subprocess.DEVNULL)
            checker = subprocess.Popen([sys.executable, "dr/health_checker.py", "--interval", "2",
                                        "--threshold", "3", "--duration", str(DURATION),
                                        "--out", "reports/health-events.jsonl"],
                                       stdout=subprocess.DEVNULL)
            processes += [traffic, checker]
            time.sleep(delay)
            subprocess.run([sys.executable, "chaos/kill_region.py", "--region", "a",
                            "--mode", mode, "--mock"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run([sys.executable, "dr/runbook.py", "--primary", "a", "--target", "b",
                            "--backend", "fs", "--auto"], check=True, stdout=subprocess.DEVNULL)
            traffic.wait(timeout=DURATION + 15)
            checker.wait(timeout=DURATION + 15)
            result = measure("reports/traffic.jsonl", "chaos/chaos-events.jsonl",
                             "reports/health-events.jsonl", "reports/failover-events.jsonl", 300)
            if not result["valid"] or result["warnings"] or result["rto_verdict"] != "PASS":
                raise AssertionError(result)
            dest = OUT / f"run-{index}"
            dest.mkdir(parents=True, exist_ok=True)
            for relative in ("reports/traffic.jsonl", "reports/health-events.jsonl",
                             "reports/failover-events.jsonl", "reports/runbook-run.jsonl",
                             "chaos/chaos-events.jsonl"):
                shutil.copy2(relative, dest / pathlib.Path(relative).name)
            row = {"run": index, "mode": mode, "kill_delay_s": delay,
                   "rto_s": result["rto_measured_s"], "rpo_s": result["rpo_at_restore_s"],
                   "docs_lost": result["docs_lost"], "failed_requests": result["requests_failed"],
                   "valid": result["valid"], "warnings": result["warnings"]}
            print(json.dumps(row), flush=True)
            return row
        finally:
            for process in processes:
                if process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGCONT)
                    except OSError:
                        pass
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            for log in logs:
                log.close()
            os.chdir(old_cwd)


def main():
    rng = random.Random(23)
    modes = ["stop", "stop", "stop", "netblock", "netblock"]
    rng.shuffle(modes)
    runs = [run_one(i, mode, round(rng.uniform(5, 8), 2))
            for i, mode in enumerate(modes, 1)]
    values = [row["rto_s"] for row in runs]
    summary = {"seed": 23, "runs": runs, "mean_rto_s": round(statistics.mean(values), 2),
               "sample_stddev_rto_s": round(statistics.stdev(values), 2),
               "config": {"health_interval_s": 2, "threshold": 3, "warmup_s": 2,
                          "edge_ttl_s": 3, "traffic_duration_s": DURATION}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
