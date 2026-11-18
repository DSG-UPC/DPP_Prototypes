#!/usr/bin/env bash
# E1: stage timing of the prototype's own DPP issuance pipeline under emulated
# inter-validator delay. Needs the prototype stack up and set up (scripts/setup.py).
# Usage: experiments/run_pipeline.sh [repeat] [delays...]
set -euo pipefail
cd "$(dirname "$0")/.."
REPEAT=${1:-45}; shift || true
DELAYS=${*:-0 50 100}
NODES="besu-supplier besu-upcycler besu-manufacturer besu-upc"
mkdir -p experiments/results

netem() {  # one-way delay on Besu p2p traffic only (port 30303)
  for c in $NODES; do
    docker run --rm --net container:$c --cap-add NET_ADMIN tb-netem \
      "tc qdisc del dev eth0 root 2>/dev/null; [ $1 = 0 ] && exit 0;
       tc qdisc add dev eth0 root handle 1: prio &&
       tc qdisc add dev eth0 parent 1:3 handle 30: netem delay ${1}ms &&
       tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip dport 30303 0xffff flowid 1:3 &&
       tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip sport 30303 0xffff flowid 1:3"
  done
}

for d in $DELAYS; do
  netem "$d"; sleep 5
  docker compose exec -T neocyclehub-bootstrap python manage.py bench_pipeline \
    --repeat "$REPEAT" --label "d$d" --out "bench_out/pipeline_d$d.json" | tail -1
  cp neocyclehub-bootstrap/bench_out/pipeline_d$d.json experiments/results/
  docker compose exec -T neocyclehub-bootstrap rm -f bench_out/pipeline_d$d.json
done
netem 0

# Remove the registry rows the benchmark created (credentials are already deleted).
python3 - <<'EOF'
import glob, json, subprocess
uuids = [r["uuid"] for f in glob.glob("experiments/results/pipeline_d*.json")
         for r in json.load(open(f))["rows"] if "uuid" in r]
uuids += [r["uuid"] for f in glob.glob("neocyclehub-bootstrap/bench_out/*.json")
          for r in json.load(open(f))["rows"] if "uuid" in r]
code = ("from registry.models import RegistryEntry as R\n"
        f"u={uuids!r}\n"
        "n=0\n"
        "for x in u: n+=R.objects.filter(dpp_url__contains=x).delete()[0]\n"
        "print('registry rows removed:', n, 'remaining:', R.objects.count())")
subprocess.run(["docker", "compose", "exec", "-T", "eu-registry", "python", "manage.py", "shell", "-c", code], check=True)
EOF
