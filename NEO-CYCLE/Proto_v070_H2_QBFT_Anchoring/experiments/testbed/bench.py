#!/usr/bin/env python3
"""
Anchoring benchmark driver for a testbed network made by gen_network.py.

Sends DPPAnchor.anchor(hash, url) transactions -- the exact call the
prototype's chain_client.anchor_dpp() makes -- as an open-loop Poisson process
from several actor accounts, each submitting through "its own" validator
(account k -> node k mod N), and measures for every transaction the time
from submission until a watcher first sees the block that contains it.

Optional fault and network-delay injection:
  --delay-ms D        one-way delay D on every validator's p2p traffic
                      (tc netem on port 30303 only, RPC is not delayed)
  --jitter-ms J       netem jitter
  --crash K@T1-T2     stop K validators (the highest-numbered ones) at T1 s
                      and start them again at T2 s after load start

Writes one JSON file with per-transaction and per-block records.
"""
import argparse
import json
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN_ID = 4004


def rpc(i):
    return f"http://127.0.0.1:{8600 + i}"


def w3_for(i):
    w3 = Web3(Web3.HTTPProvider(rpc(i), request_kwargs={"timeout": 10}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # QBFT extraData
    return w3


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def netem(n, delay_ms, jitter_ms):
    """Delay only p2p (port 30303) egress on every node; RPC stays undelayed."""
    for i in range(n):
        docker("run", "--rm", "--net", f"container:tb-node{i}", "--cap-add", "NET_ADMIN", "tb-netem",
               "tc qdisc del dev eth0 root 2>/dev/null; "
               "tc qdisc add dev eth0 root handle 1: prio && "
               f"tc qdisc add dev eth0 parent 1:3 handle 30: netem delay {delay_ms}ms {jitter_ms}ms && "
               "tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip dport 30303 0xffff flowid 1:3 && "
               "tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip sport 30303 0xffff flowid 1:3")


def clear_netem(n):
    for i in range(n):
        docker("run", "--rm", "--net", f"container:tb-node{i}", "--cap-add", "NET_ADMIN", "tb-netem",
               "tc qdisc del dev eth0 root 2>/dev/null; true", check=False)


def wait_ready(n, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ok = all(len(w3_for(i).provider.make_request("admin_peers", [])["result"]) == n - 1 for i in range(n))
            b = w3_for(0).eth.block_number
            if ok and b >= 2:
                return b
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit("network not ready")


def deploy(w3):
    art = json.load(open(os.path.join(HERE, "DPPAnchor.json")))
    acct = Account.create()
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
    tx = c.constructor().build_transaction({"from": acct.address, "nonce": 0, "gasPrice": 0, "chainId": CHAIN_ID})
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    return w3.eth.contract(address=r.contractAddress, abi=art["abi"])


class BlockWatcher(threading.Thread):
    """Records the wall-clock time each block number is first seen on one node."""

    def __init__(self, node, period=0.02):
        super().__init__(daemon=True)
        self.node, self.period = node, period
        self.seen, self.stop_flag = {}, False
        self.w3 = w3_for(node)

    def run(self):
        last = None
        while not self.stop_flag:
            try:
                b = self.w3.eth.block_number
                now = time.time()
                if last is None:
                    last = b
                for x in range(last + 1, b + 1):
                    self.seen.setdefault(x, now)
                last = max(last, b)
            except Exception:
                pass
            time.sleep(self.period)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True, help="network dir from gen_network.py")
    ap.add_argument("--rate", type=float, required=True, help="offered load, tx/s")
    ap.add_argument("--duration", type=float, required=True, help="seconds of load")
    ap.add_argument("--actors", type=int, default=10)
    ap.add_argument("--delay-ms", type=float, default=0)
    ap.add_argument("--jitter-ms", type=float, default=0)
    ap.add_argument("--crash", default="", help="K@T1-T2")
    ap.add_argument("--restart-all", action="store_true",
                    help="at T2 restart every validator, not only the crashed ones "
                         "(resets the doubled QBFT round timeout, as the Besu docs recommend)")
    ap.add_argument("--submit-node", type=int, default=-1, help="-1: account k -> node k mod N")
    ap.add_argument("--drain", type=float, default=60, help="max seconds to wait for inclusion after load")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    random.seed(a.seed)

    meta = json.load(open(os.path.join(a.net, "network.json")))
    n = meta["validators"]
    wait_ready(n)
    w3 = w3_for(0)
    contract = deploy(w3)

    clear_netem(n)
    if a.delay_ms or a.jitter_ms:
        netem(n, a.delay_ms, a.jitter_ms)
        time.sleep(5)

    crash_k, crash_t1, crash_t2 = 0, None, None
    if a.crash:
        k, span = a.crash.split("@")
        crash_k = int(k)
        crash_t1, crash_t2 = (float(x) for x in span.split("-"))
    crashed = list(range(n - crash_k, n))
    live_nodes = [i for i in range(n) if i not in crashed]

    # Pre-sign every transaction so client-side signing never limits the rate.
    total = int(a.rate * a.duration)
    actors = [Account.create() for _ in range(a.actors)]
    nonces = [0] * a.actors
    gas = 100_000
    sample = contract.functions.anchor(os.urandom(32), "x" * 66).estimate_gas({"from": actors[0].address, "gasPrice": 0})
    txs = []
    for k in range(total):
        ai = k % a.actors
        url = f"http://localhost:9001/credentials/{os.urandom(16).hex()}/"[:66]
        tx = contract.functions.anchor(os.urandom(32), url).build_transaction({
            "from": actors[ai].address, "nonce": nonces[ai], "gas": gas, "gasPrice": 0, "chainId": CHAIN_ID})
        nonces[ai] += 1
        if a.submit_node >= 0:
            node = a.submit_node
        else:
            node = ai % n
            if crash_k and node in crashed:
                node = live_nodes[ai % len(live_nodes)]
        txs.append({"k": k, "actor": ai, "node": node,
                    "raw": actors[ai].sign_transaction(tx).raw_transaction})

    watcher_node = live_nodes[0]
    watcher = BlockWatcher(watcher_node)
    watcher.start()
    time.sleep(1)
    providers = {i: w3_for(i) for i in range(n)}
    results = [None] * total
    events = []

    def send(t):
        try:
            ts = time.time()
            h = providers[t["node"]].eth.send_raw_transaction(t["raw"])
            results[t["k"]] = {"k": t["k"], "node": t["node"], "t_submit": ts, "hash": h.hex()}
        except Exception as e:
            results[t["k"]] = {"k": t["k"], "node": t["node"], "t_submit": time.time(), "error": str(e)[:200]}

    def fault_thread(t0):
        if not crash_k:
            return
        time.sleep(max(0, t0 + crash_t1 - time.time()))
        for i in crashed:
            docker("stop", "-t", "0", f"tb-node{i}")
        events.append({"event": "crash", "nodes": crashed, "t": time.time()})
        time.sleep(max(0, t0 + crash_t2 - time.time()))
        if a.restart_all:
            for i in live_nodes:
                docker("stop", "-t", "0", f"tb-node{i}")
            for i in range(n):
                docker("start", f"tb-node{i}")
            events.append({"event": "restart", "nodes": list(range(n)), "all": True, "t": time.time()})
            return
        for i in crashed:
            docker("start", f"tb-node{i}")
        events.append({"event": "restart", "nodes": crashed, "t": time.time()})

    # Poisson arrivals: exponential gaps with mean 1/rate. Fixed spacing at a
    # rate commensurate with the block period phase-locks every arrival to the
    # same point of the block slot and measures that phase, not the latency
    # distribution (found in the second testbed run).
    offsets, acc = [], 0.0
    for _ in txs:
        offsets.append(acc)
        acc += random.expovariate(a.rate)
    t0 = time.time() + 0.5
    ft = threading.Thread(target=fault_thread, args=(t0,), daemon=True)
    ft.start()
    with ThreadPoolExecutor(max_workers=64) as pool:
        for k, t in enumerate(txs):
            target = t0 + offsets[k]
            d = target - time.time()
            if d > 0:
                time.sleep(d)
            pool.submit(send, t)
    t_load_end = time.time()
    ft.join(timeout=max(0, (crash_t2 or 0) + 5 - (time.time() - t0)) if crash_k else 0)

    # Drain: wait until every accepted transaction has a receipt, or timeout.
    pending = {r["hash"]: r for r in results if r and "hash" in r}
    deadline = time.time() + a.drain
    while pending and time.time() < deadline:
        def check(h):
            try:
                return h, providers[watcher_node].eth.get_transaction_receipt(h)
            except Exception:
                return h, None
        with ThreadPoolExecutor(max_workers=32) as pool:
            for h, rc in pool.map(check, list(pending)):
                if rc is not None:
                    pending[h]["block"] = rc.blockNumber
                    pending[h]["gas_used"] = rc.gasUsed
                    pending[h]["status"] = rc.status
                    del pending[h]
        time.sleep(0.5)
    time.sleep(1)
    watcher.stop_flag = True

    # Resync of restarted nodes: time until each reaches the watcher's head.
    resync = {}
    if crash_k:
        head = providers[watcher_node].eth.block_number
        t_r0 = time.time()
        for i in crashed:
            while time.time() - t_r0 < 300:
                try:
                    if w3_for(i).eth.block_number >= head:
                        resync[i] = time.time() - t_r0
                        break
                except Exception:
                    pass
                time.sleep(0.5)

    blocks = {}
    first = min(watcher.seen) if watcher.seen else None
    if first is not None:
        for b in range(first, max(watcher.seen) + 1):
            blk = providers[watcher_node].eth.get_block(b)
            blocks[b] = {"seen": watcher.seen.get(b), "timestamp": blk.timestamp,
                         "tx_count": len(blk.transactions), "gas_used": blk.gasUsed, "size": blk.size}

    for r in results:
        if r and "block" in r and r["block"] in watcher.seen:
            r["latency"] = watcher.seen[r["block"]] - r["t_submit"]

    clear_netem(n)
    out = {"meta": {**meta, **vars(a), "estimated_gas": sample, "t0": t0, "t_load_end": t_load_end,
                    "watcher_node": watcher_node},
           "tx": results, "blocks": blocks, "events": events, "resync_s": resync}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f)
    lat = sorted(r["latency"] for r in results if r and "latency" in r)
    ok = len(lat)
    print(f"{os.path.basename(a.out)}: sent={total} included={ok} "
          f"errors={sum(1 for r in results if r and 'error' in r)} "
          + (f"median={lat[ok // 2]:.3f}s p95={lat[int(ok * 0.95) - 1]:.3f}s" if ok else ""))


if __name__ == "__main__":
    main()
