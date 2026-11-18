# Experiments

Harness and raw results of the anchoring evaluation reported in the paper
"Evaluating Permissioned QBFT Anchoring for UNTP Digital Product Passports in a
Rare-Earth Upcycling Consortium".

## On the prototype (stack up and set up with `scripts/setup.py`)

| Script | What it does | Result |
|---|---|---|
| `run_pipeline.sh [repeat] [delays...]` | E1: stage timing of DPP issuance (`bench_pipeline` command) at 0/50/100 ms one-way delay between the four Besu validators (netem on port 30303) | `results/pipeline_d*.json` |
| `run_threats.sh` (`FOREIGN=<addr>` optional) | Threat tests T1-T4 and URL squatting (`threat_tests` command), pointer check vs strict check, plus the 2-of-4 outage test | `results/threats.json`, `results/outage.json` |
| `time_strict_check.py` | Times `verify_anchor_strict()` over the 15 scenario passports | `results/strict_check_timing.json` |

`run_pipeline.sh` and `run_threats.sh` clean up after themselves: test credentials
are deleted and their registry rows removed. Their on-chain anchors stay (the chain
is append-only). Run `time_strict_check.py` with
`docker compose exec -T neocyclehub-bootstrap python manage.py shell < experiments/time_strict_check.py`.

## Testbed (`testbed/`)

`gen_network.py` generates an N-validator QBFT network with the prototype's Besu
version, consensus settings and 4.7 M block gas limit. `bench.py` sends the
prototype's `anchor(hash, url)` call as an open-loop Poisson process and records
per-transaction latency, blocks, and optional netem delay / crash injection.
`run_all.py <experiment...>` runs the matrix. It needs the `tb-netem` image:
`docker build -t tb-netem testbed/netem`.

| Experiment | Content |
|---|---|
| `latency` | E2: block period 1/2/5 s, one-way delay 0-200 ms, 2 anchors/s |
| `scale` | E3: 4/7/10 validators at low load |
| `throughput` | E3: offered load 20-120 anchors/s for 4 and 7 validators |
| `faults` | E4: k adjacent validators stopped 30 s into a run, restarted 60 s later |
| `timeout` | E4: round-change timeout varied |
| `restartall` | E4: all validators restarted |
| `quorum` | E4: quorum loss repeated (N=4 with outages of 30/60/120 s, N=7/10 at 60 s, restart-all), with the Besu log of every node per run (`results/testbed/quorum/*_node*.log`) |

E5 (CPU and memory per validator) is sampled during all runs. Run the matrix
detached; the first six experiments take about 3.5 h, `quorum` about 75 min:

```
cd testbed && setsid nohup python3 -u run_all.py latency scale throughput faults timeout restartall quorum > ../results/testbed/run.log 2>&1 &
```

`analyze.py --fig-dir <dir>` writes `results/summary.json` and the figures
(`results/figures/`). Achieved throughput is measured from the first to the last
block in the measurement window.

## Results

- `results/summary.json`: all numbers used in the paper.
- `results/testbed/`: testbed runs (Poisson arrivals, measured after JVM warm-up).
  `throughput/` is the run with an extra unmeasured load phase before measuring;
  `throughput_v3_no_load_warmup/` is the same run without it, kept for comparison
  (the first loaded run is slower because of JIT compilation).
- `results/testbed_diag_n10_cpu/`: shows that 10 validators are CPU-bound on the
  8-core host at 20 anchors/s, which is why there is no 10-validator throughput run.
- `results/pipeline_d*.json`, `threats.json`, `outage.json`,
  `strict_check_timing.json`: measurements on the prototype itself.

## Notes on the prototype

- `blockchain/scripts/deploy.js` reuses an existing deployment, so earlier anchors
  stay visible to address-filtered queries after `docker compose up/start`.
- `chain_client.verify_anchor_strict()` (both hub apps) uses the pinned contract
  and the earliest anchor for the URL sent by the issuer, and queries logs in
  5000-block windows (Besu's default `--rpc-max-logs-range`). Taking the earliest
  anchor by any sender and checking the sender afterwards would let another member
  squat a URL; the `squat` threat test shows both rules. The simpler
  `verify_anchor()` follows the issuer's stored transaction hash and is fooled
  by an issuer that re-anchors and repoints it; the threat tests show both.
