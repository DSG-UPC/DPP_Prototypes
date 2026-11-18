#!/usr/bin/env python3
"""
Runs the testbed experiment matrix of the accompanying paper (see
../README.md). For every network configuration it generates a fresh
network (gen_network.py), starts it, runs the bench.py runs listed for it
while sampling `docker stats` of the validators, records each node's
chain-data size, and tears the network down again.

Usage: python3 run_all.py [experiment ...]   (default: all)
Experiments: latency, scale, throughput, faults, timeout, restartall, quorum
Results go to ../results/testbed/<experiment>/.
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NETS = os.path.join(HERE, "nets")
RESULTS = os.path.join(HERE, "..", "results", "testbed")

LOW_RATE, LOW_DUR = 2, 120          # low load: latency is not queueing-dominated
LOAD_WARMUP = {"throughput"}         # experiments that get an unmeasured load warm-up run

# experiment -> list of (network params, [bench runs])
MATRIX = {
    # E2: anchoring latency vs block period and emulated one-way delay
    "latency": [
        ({"validators": 4, "block_period": bp},
         [{"name": f"n4_b{bp}_d{d}", "rate": LOW_RATE, "duration": LOW_DUR, "delay_ms": d}
          for d in (0, 25, 50, 100, 200)])
        for bp in (1, 2, 5)
    ],
    # E3a: latency vs number of validators (governance: role nodes -> per-company nodes)
    "scale": [
        ({"validators": n, "block_period": 2},
         [{"name": f"n{n}_b2_d{d}", "rate": LOW_RATE, "duration": LOW_DUR, "delay_ms": d} for d in (0, 50)])
        for n in (4, 7, 10)
    ],
    # E3b: throughput ceiling under the prototype's block gas limit. 200 sender
    # accounts: aggregate load from many issuers, each with few pending
    # transactions, as in the prototype where anchor_dpp() waits for its receipt.
    "throughput": [
        ({"validators": n, "block_period": 2},
         [{"name": f"n{n}_b2_r{r}", "rate": r, "duration": 60, "delay_ms": 0, "drain": 120, "actors": 200}
          for r in (20, 40, 60, 80, 100, 120)])
        for n in (4, 7)
    ],
    # (10 validators are not in the throughput set: on the 8-core host they are
    # CPU-bound already at 20 anchors/s, see results/testbed_diag_n10_cpu/.)
    # E4: crash faults; K highest-numbered validators stopped at 30 s, restarted at 90 s
    "faults": [
        ({"validators": n, "block_period": 2},
         [{"name": f"n{n}_crash{k}", "rate": LOW_RATE, "duration": 150, "delay_ms": 0,
           "crash": f"{k}@30-90", "drain": 180} for k in ks])
        for n, ks in ((3, (1,)), (4, (1, 2)), (7, (2, 3)), (10, (3, 4)))
    ],
    # E4c: recovery from quorum loss when every company restarts its validator
    "restartall": [
        ({"validators": 4, "block_period": 2},
         [{"name": "n4_crash2_restartall", "rate": LOW_RATE, "duration": 150, "delay_ms": 0,
           "crash": "2@30-90", "drain": 180, "restart_all": True}]),
    ],
    # E4d: repetitions of quorum
    # loss with different outage lengths, plus Besu logs of every node, to test
    # whether recovery time depends on outage length and N and to check the
    # round-timer explanation. Seeds differ per repetition.
    "quorum": [
        ({"validators": 4, "block_period": 2},
         [{"name": f"n4_crash2_out{o}_rep{rep}", "rate": LOW_RATE, "duration": 30 + o + 60, "delay_ms": 0,
           "crash": f"2@30-{30 + o}", "drain": 600, "seed": rep, "logs": True}
          for o in (30, 60, 120) for rep in (1, 2, 3)]
         + [{"name": f"n4_crash2_restartall_rep{rep}", "rate": LOW_RATE, "duration": 150, "delay_ms": 0,
             "crash": "2@30-90", "drain": 300, "restart_all": True, "seed": rep, "logs": True}
            for rep in (2, 3)]),
    ] + [
        ({"validators": n, "block_period": 2},
         [{"name": f"n{n}_crash{k}_out60_rep{rep}", "rate": LOW_RATE, "duration": 150, "delay_ms": 0,
           "crash": f"{k}@30-90", "drain": 600, "seed": rep, "logs": True} for rep in (2, 3)])
        for n, k in ((7, 3), (10, 4))
    ],
    # E4b: effect of QBFT's round-change timeout on latency while one validator is down
    "timeout": [
        ({"validators": 4, "block_period": 2, "request_timeout": rt},
         [{"name": f"n4_crash1_rt{rt}", "rate": LOW_RATE, "duration": 150, "delay_ms": 0,
           "crash": "1@30-90", "drain": 180}])
        for rt in (2, 5, 10)
    ],
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, **kw)


class StatsSampler(threading.Thread):
    def __init__(self, path, period=5):
        super().__init__(daemon=True)
        self.path, self.period, self.stop_flag = path, period, False

    def run(self):
        with open(self.path, "w") as f:
            f.write("t,container,cpu_pct,mem_mib\n")
            while not self.stop_flag:
                out = subprocess.run(
                    "docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' "
                    "$(docker ps -q --filter name=tb-node)", shell=True, capture_output=True, text=True).stdout
                now = time.time()
                for line in out.strip().splitlines():
                    name, cpu, mem = line.split(",")
                    used = mem.split("/")[0].strip()
                    mib = float(used[:-3]) * (1024 if used.endswith("GiB") else 1) if used[-3:] in ("MiB", "GiB") else 0
                    f.write(f"{now:.1f},{name},{cpu.rstrip('%')},{mib:.1f}\n")
                f.flush()
                time.sleep(self.period)


def total_cpu():
    out = subprocess.run("docker stats --no-stream --format '{{.CPUPerc}}' $(docker ps -q --filter name=tb-node)",
                         shell=True, capture_output=True, text=True).stdout
    return sum(float(x.rstrip('%')) for x in out.split() if x)


def warm_up(n, limit=300):
    """Wait until the validators' JVMs have warmed up: total CPU below 20 % per
    node in two consecutive samples (or the limit). Measuring before this
    point measured JIT compilation, not consensus (found in the first run)."""
    t0 = time.time()
    time.sleep(30)
    calm = 0
    while time.time() - t0 < limit:
        calm = calm + 1 if total_cpu() < 20 * n else 0
        if calm >= 2:
            break
        time.sleep(10)
    return time.time() - t0


def data_sizes(n):
    sizes = {}
    for i in range(n):
        try:
            sizes[i] = int(sh(f"docker exec tb-node{i} du -sk /tmp/data").stdout.split()[0])
        except subprocess.CalledProcessError:
            sizes[i] = None
    return sizes


def run_experiment(exp):
    outdir = os.path.join(RESULTS, exp)
    os.makedirs(outdir, exist_ok=True)
    for params, runs in MATRIX[exp]:
        n = params["validators"]
        tag = "_".join(f"{k}{v}" for k, v in params.items())
        net = os.path.join(NETS, f"{exp}_{tag}")
        args = " ".join(f"--{k.replace('_', '-')} {v}" for k, v in params.items())
        print(f"[{time.strftime('%H:%M:%S')}] {exp}: network {tag}", flush=True)
        sh(f"python3 {HERE}/gen_network.py {args} --out {net}")
        sh("docker compose -p tb up -d", cwd=net)
        try:
            warm = warm_up(n)
            print(f"  warm-up {warm:.0f} s", flush=True)
            with open(os.path.join(outdir, f"{tag}_warmup.json"), "w") as f:
                json.dump({"warmup_s": warm}, f)
            # idle resource baseline for this network size, after JVM warm-up
            idle = StatsSampler(os.path.join(outdir, f"{tag}_idle_stats.csv"))
            idle.start(); time.sleep(30); idle.stop_flag = True; idle.join()
            if exp in LOAD_WARMUP:
                # Idle warm-up does not JIT-compile the transaction paths: the first
                # loaded run on a fresh network was slower (found in v3). One
                # unmeasured run at moderate load first.
                subprocess.run(f"python3 {HERE}/bench.py --net {net} --rate 20 --duration 30 --actors 200 "
                               f"--out {os.path.join(outdir, '_load_warmup.json')}", shell=True, capture_output=True)
                print("  load warm-up done", flush=True)
            for r in runs:
                out = os.path.join(outdir, f"{r['name']}.json")
                if os.path.exists(out):
                    print(f"  skip {r['name']} (exists)", flush=True)
                    continue
                size0 = data_sizes(n)
                t_start = int(time.time())
                st = StatsSampler(os.path.join(outdir, f"{r['name']}_stats.csv"))
                st.start()
                cmd = (f"python3 {HERE}/bench.py --net {net} --rate {r['rate']} --duration {r['duration']} "
                       f"--delay-ms {r['delay_ms']} --drain {r.get('drain', 60)} --actors {r.get('actors', 10)} --out {out}"
                       + (f" --crash {r['crash']}" if r.get("crash") else "")
                       + (" --restart-all" if r.get("restart_all") else "")
                       + (f" --seed {r['seed']}" if r.get("seed") else ""))
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                st.stop_flag = True; st.join()
                print("  " + (res.stdout.strip().splitlines() or ["(no output)"])[-1], flush=True)
                if res.returncode != 0:
                    print(res.stderr[-1500:], flush=True)
                    continue
                if r.get("logs"):
                    for i in range(n):
                        subprocess.run(f"docker logs --since {t_start} tb-node{i} > "
                                       f"{os.path.join(outdir, r['name'])}_node{i}.log 2>&1", shell=True)
                d = json.load(open(out))
                d["data_kib_before"], d["data_kib_after"] = size0, data_sizes(n)
                json.dump(d, open(out, "w"))
        finally:
            subprocess.run("docker compose -p tb down -t 5", shell=True, cwd=net, capture_output=True)


if __name__ == "__main__":
    for exp in sys.argv[1:] or list(MATRIX):
        run_experiment(exp)
    print(f"[{time.strftime('%H:%M:%S')}] all done", flush=True)
