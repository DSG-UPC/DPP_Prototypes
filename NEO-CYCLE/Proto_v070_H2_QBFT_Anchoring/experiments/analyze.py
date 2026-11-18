#!/usr/bin/env python3
"""
Turns experiments/results into the numbers, tables and figures of the
accompanying paper. Prints a summary and writes results/summary.json plus PDF
figures to the directory given with --fig-dir.

Usage: python3 experiments/analyze.py --fig-dir experiments/results/figures
"""
import argparse
import csv
import glob
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
TB = os.path.join(RES, "testbed")
GAS_LIMIT = 0x47B760


def pct(v, p):
    v = sorted(v)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def load(path):
    with open(path) as f:
        return json.load(f)


def runs(exp):
    """Result files of one testbed experiment, without warm-up records."""
    return [f for f in sorted(glob.glob(os.path.join(TB, exp, "*.json")))
            if not os.path.basename(f).startswith("_") and not f.endswith("_warmup.json")]


def lat(d, t_from=None, t_to=None):
    return [r["latency"] for r in d["tx"] if r and "latency" in r
            and (t_from is None or r["t_submit"] >= t_from) and (t_to is None or r["t_submit"] < t_to)]


def summary_lat(v):
    return {"n": len(v), "median": pct(v, 50), "p5": pct(v, 5), "p95": pct(v, 95), "max": max(v) if v else None}


def pipeline():
    out = {}
    for f in sorted(glob.glob(os.path.join(RES, "pipeline_d*.json"))):
        d = load(f)
        rows = [r for r in d["rows"] if "error" not in r]
        tot = [r["sign_ms"] + r["register_ms"] + r["anchor_ms"] for r in rows]
        out[d["label"]] = {
            "n": len(rows),
            "all_ok": all(r["anchored"] and r["verify_sig"] == "valid" and r["verify_anchor"] == "valid" for r in rows),
            **{k: {"median": pct([r[k] for r in rows], 50), "p5": pct([r[k] for r in rows], 5),
                   "p95": pct([r[k] for r in rows], 95)}
               for k in ("sign_ms", "register_ms", "anchor_ms", "verify_sig_ms", "verify_anchor_ms")},
            "total_ms_median": pct(tot, 50),
            "anchor_share_median": pct([r["anchor_ms"] / t for r, t in zip(rows, tot)], 50),
            "credential_bytes_median": pct([r["credential_bytes"] for r in rows], 50),
        }
    return out


def block_gaps(d, t_from, t_to):
    seen = sorted(b["seen"] for b in d["blocks"].values() if b["seen"] and t_from <= b["seen"] <= t_to)
    return [b - a for a, b in zip(seen, seen[1:])]


def latency_exp(name):
    out = {}
    for f in runs(name):
        d = load(f)
        out[os.path.basename(f)[:-5]] = summary_lat(lat(d))
    return out


def throughput():
    out = {}
    for f in runs("throughput"):
        d = load(f)
        m = d["meta"]
        t0, t1 = m["t0"] + 10, m["t_load_end"]
        blocks = sorted((b for b in d["blocks"].values() if b["seen"] and t0 <= b["seen"] <= t1),
                        key=lambda b: b["seen"])
        # Rate from first to last block in the window: dividing by the
        # fixed window counted +-1 block at its edges and gave 87.9/s above the 85/s
        # ceiling of 170 anchors per 2 s block.
        tx_in = sum(b["tx_count"] for b in blocks[1:])
        t0, t1 = (blocks[0]["seen"], blocks[-1]["seen"]) if len(blocks) > 1 else (t0, t0)
        gas = [r["gas_used"] for r in d["tx"] if r and "gas_used" in r]
        included = sum(1 for r in d["tx"] if r and "block" in r)
        out[os.path.basename(f)[:-5]] = {
            "validators": m["validators"], "offered": m["rate"],
            "achieved": tx_in / (t1 - t0) if t1 > t0 else None,
            "included": included, "sent": len(d["tx"]),
            "errors": sum(1 for r in d["tx"] if r and "error" in r),
            "latency": summary_lat(lat(d)),
            "gas_per_tx_median": pct(gas, 50),
            "max_tx_per_block": max((b["tx_count"] for b in d["blocks"].values()), default=0),
            "bytes_per_tx": (sum(b["size"] for b in blocks[1:]) / tx_in) if tx_in else None,
        }
    return out


def faults(name):
    out = {}
    for f in runs(name):
        d = load(f)
        ev = {e["event"]: e["t"] for e in d["events"]}
        tc, tr = ev.get("crash"), ev.get("restart")
        m = d["meta"]
        during = lat(d, tc, tr)
        before = lat(d, m["t0"], tc)
        gaps = block_gaps(d, tc, tr + 60)
        seen_after = sorted(b["seen"] for b in d["blocks"].values() if b["seen"] and b["seen"] > tr)
        blocks_during = sum(1 for b in d["blocks"].values() if b["seen"] and tc < b["seen"] < tr)
        out[os.path.basename(f)[:-5]] = {
            "validators": m["validators"], "crashed": len(d["events"][0]["nodes"]) if d["events"] else 0,
            "request_timeout": m.get("request_timeout"),
            "blocks_during_crash": blocks_during,
            "crash_window_s": tr - tc,
            "max_block_gap_s": max(gaps) if gaps else None,
            "first_block_after_restart_s": (seen_after[0] - tr) if seen_after else None,
            "lat_before": summary_lat(before), "lat_during": summary_lat(during),
            "included": sum(1 for r in d["tx"] if r and "block" in r), "sent": len(d["tx"]),
            "resync_s": d.get("resync_s"),
        }
    return out


def resources():
    """CPU and memory per validator, idle and under load, from the docker stats samples."""
    out = {}
    for f in sorted(glob.glob(os.path.join(TB, "*", "*_stats.csv"))):
        rows = list(csv.DictReader(open(f)))
        if not rows:
            continue
        cpu = [float(r["cpu_pct"]) for r in rows]
        mem = [float(r["mem_mib"]) for r in rows]
        key = os.path.relpath(f, TB)[:-10]
        out[key] = {"cpu_pct_median": pct(cpu, 50), "mem_mib_median": pct(mem, 50),
                    "nodes": len({r["container"] for r in rows})}
    return out


def cpu_per_anchor():
    """Validator CPU per anchor: (median host CPU under load - idle) / rate,
    per validator, plus the median host CPU to flag host saturation."""
    out = {}
    for f in runs("throughput"):
        d = load(f)
        n, rate = d["meta"]["validators"], d["meta"]["rate"]
        name = os.path.basename(f)[:-5]
        def host_cpu(path):
            rows = list(csv.DictReader(open(path)))
            by_t = {}
            for r in rows:
                by_t[r["t"]] = by_t.get(r["t"], 0) + float(r["cpu_pct"])
            return pct(list(by_t.values()), 50) if by_t else None
        load_cpu = host_cpu(os.path.join(TB, "throughput", f"{name}_stats.csv"))
        idle_cpu = host_cpu(os.path.join(TB, "throughput", f"validators{n}_block_period2_idle_stats.csv"))
        out[name] = {"validators": n, "rate": rate, "host_cpu_pct_median": load_cpu, "idle_cpu_pct": idle_cpu,
                     "cpu_ms_per_anchor_per_validator": ((load_cpu - idle_cpu) * 10 / rate / n)
                     if load_cpu is not None and idle_cpu is not None else None,
                     "host_saturated": load_cpu is not None and load_cpu > 0.75 * 800}
    return out


def storage():
    out = {}
    for f in runs("throughput"):
        d = load(f)
        b0, b1 = d.get("data_kib_before"), d.get("data_kib_after")
        inc = sum(1 for r in d["tx"] if r and "block" in r)
        if b0 and b1 and inc:
            grow = [b1[k] - b0[k] for k in b0 if b0[k] is not None and b1.get(k) is not None]
            out[os.path.basename(f)[:-5]] = {"included": inc, "kib_growth_median_node": pct(grow, 50),
                                              "bytes_per_anchor": 1024 * pct(grow, 50) / inc}
    return out


def figures(s, fig_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 8, "font.family": "serif", "figure.dpi": 150})
    os.makedirs(fig_dir, exist_ok=True)

    # Fig: latency vs one-way delay for three block periods
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for bp, mk in ((1, "o"), (2, "s"), (5, "^")):
        pts = []
        for d in (0, 25, 50, 100, 200):
            r = s["latency"].get(f"n4_b{bp}_d{d}")
            if r and r["median"] is not None:
                pts.append((d, r["median"], r["p5"], r["p95"]))
        if pts:
            x = [p[0] for p in pts]
            ax.plot(x, [p[1] for p in pts], marker=mk, ms=3, lw=1, label=f"block {bp} s")
            ax.fill_between(x, [p[2] for p in pts], [p[3] for p in pts], alpha=0.15)
    ax.set_xlabel("one-way delay (ms)")
    ax.set_ylabel("latency (s)")
    ax.legend(frameon=False, fontsize=6.5)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(fig_dir, "latency_delay.pdf"))
    plt.close(fig)

    # Fig: throughput achieved vs offered, with the gas-limit ceiling
    fig, ax = plt.subplots(figsize=FIGSIZE)
    gas = None
    for n, mk in ((4, "o"), (7, "s")):
        pts = sorted((r["offered"], r["achieved"]) for k, r in s["throughput"].items()
                     if r["validators"] == n and r["achieved"] is not None)
        gas = gas or next((r["gas_per_tx_median"] for r in s["throughput"].values() if r["gas_per_tx_median"]), None)
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker=mk, ms=3, lw=1, label=f"{n} validators")
    if gas:
        ceil = GAS_LIMIT / gas / 2
        ax.axhline(ceil, ls="--", lw=0.8, color="gray")
        ax.text(22, ceil - 2, f"gas limit\n({int(GAS_LIMIT // gas)}/block)", fontsize=6, color="gray", va="top")
    ax.set_xlabel("offered (anchors/s)")
    ax.set_ylabel("achieved (1/s)")
    ax.legend(frameon=False, fontsize=6.5)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(fig_dir, "throughput.pdf"))
    plt.close(fig)


# One third of the LNCS text width (three panels in one row).
FIGSIZE = (2.0, 1.75)


def fault_figure(fig_dir):
    """Cumulative blocks over time for the crash runs of the 4-validator network."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    runs_ = [("faults", "n4_crash1", "1 of 4 down"),
             ("faults", "n4_crash2", "2 of 4, restart stopped"),
             ("restartall", "n4_crash2_restartall", "2 of 4, restart all")]
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for exp, name, label in runs_:
        f = os.path.join(TB, exp, f"{name}.json")
        if not os.path.exists(f):
            continue
        d = load(f)
        t0 = d["meta"]["t0"]
        ts = sorted(b["timestamp"] for b in d["blocks"].values())
        base = ts[0]
        xs = [t - t0 for t in ts]
        ax.step(xs, range(len(xs)), where="post", lw=1, label=label)
    ax.axvspan(30, 90, color="gray", alpha=0.15, lw=0)
    # (grey band = validators stopped; explained in the paper caption)
    ax.set_xlim(0, 280)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("blocks")
    ax.legend(frameon=False, fontsize=6, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(fig_dir, "fault_timeline.pdf"))
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fig-dir", default=os.path.join(RES, "figures"))
    a = ap.parse_args()
    s = {"pipeline": pipeline(), "latency": latency_exp("latency"), "scale": latency_exp("scale"),
         "throughput": throughput(), "faults": faults("faults"), "timeout": faults("timeout"),
         "restartall": faults("restartall"), "cpu_per_anchor": cpu_per_anchor(),
         "resources": resources(), "storage": storage()}
    with open(os.path.join(RES, "summary.json"), "w") as f:
        json.dump(s, f, indent=1)
    figures(s, a.fig_dir)
    fault_figure(a.fig_dir)
    print(json.dumps(s, indent=1, default=lambda x: round(x, 3) if isinstance(x, float) else x)[:6000])


if __name__ == "__main__":
    main()
