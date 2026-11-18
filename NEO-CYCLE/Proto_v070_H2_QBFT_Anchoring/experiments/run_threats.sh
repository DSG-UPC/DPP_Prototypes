#!/usr/bin/env bash
# Threat-model checks (threat_tests) and the chain-outage test on the running
# prototype. Needs the stack up and set up. Results in experiments/results/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p experiments/results
APP=neocyclehub-bootstrap

docker compose exec -T $APP python manage.py threat_tests --out bench_out/threats.json ${FOREIGN:+--foreign-contract $FOREIGN} | tail -40
cp $APP/bench_out/threats.json experiments/results/
docker compose exec -T $APP rm -f bench_out/threats.json

# Outage: stop two of the four validators (quorum lost), issue one DPP for a
# supplier actor (submits through besu-supplier, which stays up), restart,
# then check whether the anchor transaction was included after all.
docker stop besu-manufacturer besu-upc >/dev/null
T0=$(date +%s.%N)
docker compose exec -T $APP python manage.py shell -c "
import json, time
from django.conf import settings
from supply_chain.models import ProductBatch
from supply_chain.credential_builders import build_dpp
from supply_chain.issuer import issue_credential
from supply_chain.registry_client import register_dpp
from supply_chain.chain_client import anchor_dpp
from web3 import Web3
p = ProductBatch.objects.filter(actor__role='supplier').order_by('id').first()
s, e, sub = build_dpp(p, settings.NEOCYCLEHUB_PUBLIC_URL)
t = time.perf_counter()
c = issue_credential(actor=p.actor, cred_type='DPP', endpoint=e, schema_name=s, credential_subject=sub,
                     label='[outage-test] ' + p.name, product=p, metrics_mode='benchmark')
t_sign = time.perf_counter() - t
register_dpp(p.actor, p, c, settings.NEOCYCLEHUB_PUBLIC_URL)
t = time.perf_counter()
anchor_dpp(p.actor, p, c, settings.NEOCYCLEHUB_PUBLIC_URL)
t_anchor = time.perf_counter() - t
c.refresh_from_db()
json.dump({'uuid': str(c.uuid), 'hash': Web3.keccak(text=json.dumps(c.credential, sort_keys=True)).hex(),
           'sign_s': t_sign, 'anchor_call_s': t_anchor, 'recorded_tx': c.chain_tx_hash or None},
          open('bench_out/outage.json', 'w'))
print('outage issuance done', t_sign, t_anchor, c.chain_tx_hash)
" | tail -1
docker start besu-manufacturer besu-upc >/dev/null
docker compose exec -T $APP python manage.py shell -c "
import json
from supply_chain.models import IssuedCredential
from supply_chain.chain_client import verify_anchor, _load_deployment
from supply_chain.issuer import delete_credential_everywhere
from web3 import Web3
o = json.load(open('bench_out/outage.json'))
w3 = Web3(Web3.HTTPProvider('http://besu-supplier:8545'))
dep = _load_deployment()
ct = w3.eth.contract(address=dep['address'], abi=dep['abi'])
h = o['hash'] if o['hash'].startswith('0x') else '0x' + o['hash']
# Poll for up to 420 s: after a quorum loss QBFT resumes only once the doubled
# round timer expires (testbed: about 170 s after the restart).
import time
t0 = time.time(); logs = []; first_block_wait = None; b0 = w3.eth.block_number
while time.time() - t0 < 420:
    try:
        if first_block_wait is None and w3.eth.block_number > b0:
            first_block_wait = time.time() - t0
        logs = ct.events.Anchored().get_logs(fromBlock=0, argument_filters={'dppHash': bytes.fromhex(h[2:])})
        if logs:
            break
    except Exception:
        pass
    time.sleep(5)
o['first_block_after_restart_s'] = first_block_wait
o['anchor_appeared_after_restart_s'] = (time.time() - t0) if logs else None
c = IssuedCredential.objects.get(uuid=o['uuid'])
o['anchor_status_in_app'] = verify_anchor(c)['status']
o['included_on_chain_after_recovery'] = len(logs)
delete_credential_everywhere(c)
json.dump(o, open('bench_out/outage.json', 'w'), indent=1)
print(json.dumps(o, indent=1))
"
cp $APP/bench_out/outage.json experiments/results/
docker compose exec -T $APP rm -f bench_out/outage.json

# Remove the registry rows of all test credentials.
python3 - <<'PY'
import json, subprocess
u = json.load(open("experiments/results/threats.json"))["created_uuids"] + [json.load(open("experiments/results/outage.json"))["uuid"]]
code = ("from registry.models import RegistryEntry as R\n" f"u={u!r}\n" "n=0\n"
        "for x in u: n+=R.objects.filter(dpp_url__contains=x).delete()[0]\n"
        "print('registry rows removed:', n, 'remaining:', R.objects.count())")
subprocess.run(["docker", "compose", "exec", "-T", "eu-registry", "python", "manage.py", "shell", "-c", code], check=True)
PY
