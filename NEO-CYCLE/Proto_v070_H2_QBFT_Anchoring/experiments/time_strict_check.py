"""
Times chain_client.verify_anchor_strict() over all scenario passports
(results/strict_check_timing.json).

Run inside the operator application:
  docker compose exec -T neocyclehub-bootstrap python manage.py shell < experiments/time_strict_check.py
Prints one JSON line: per-call times (ms), median/p95 and the number of Anchored events
in the pinned contract (the strict check scans all of them, since dppUrl is not indexed).
"""
import json
import statistics
import time

from supply_chain import chain_client
from supply_chain.models import IssuedCredential

REPEAT = 3
creds = list(IssuedCredential.objects.filter(cred_type='DPP').exclude(label__startswith='[')
             .select_related('actor'))
times, statuses = [], {}
for _ in range(REPEAT):
    for c in creds:
        t = time.perf_counter()
        r = chain_client.verify_anchor_strict(c)
        times.append((time.perf_counter() - t) * 1000)
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1

dep = chain_client._load_deployment()
w3 = chain_client.Web3(chain_client.Web3.HTTPProvider(chain_client._node_rpc_url(creds[0].actor)))
contract = w3.eth.contract(address=dep["address"], abi=dep["abi"])
head, n_events = w3.eth.block_number, 0
for start in range(0, head + 1, chain_client.LOGS_RANGE):
    n_events += len(contract.events.Anchored().get_logs(
        fromBlock=start, toBlock=min(start + chain_client.LOGS_RANGE - 1, head)))
times.sort()
print(json.dumps({
    "passports": len(creds), "repeat": REPEAT, "calls": len(times), "statuses": statuses,
    "median_ms": round(statistics.median(times), 1),
    "p95_ms": round(times[int(0.95 * (len(times) - 1))], 1),
    "events_in_contract": n_events, "block": w3.eth.block_number,
}))
