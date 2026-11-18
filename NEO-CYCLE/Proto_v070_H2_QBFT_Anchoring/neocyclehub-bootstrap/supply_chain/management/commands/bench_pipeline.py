"""
Stage-by-stage timing of the DPP issuance pipeline, for the anchoring
evaluation (see experiments/README.md).

For each repetition it issues one DPP for a product from the fixtures,
cycling through all products so that every actor and therefore every
validator node is used, and times the four stages separately:

  sign      issue_credential(): idhub validates and JOSE-signs the payload
  register  register_dpp(): index entry in the mock EU registry
  anchor    anchor_dpp(): keccak256 of the credential on the Besu network,
            submitted through the actor's own node, until the receipt
  verify    idhub signature/status check (_verify_node) and the live
            on-chain anchor check (verify_anchor), timed separately

Each test credential is deleted afterwards (delete_credential_everywhere),
like benchmark_issuance does. Its registry row and its on-chain anchor
remain: the registry has no delete API and the chain is append-only. The
written result file lists the UUIDs so the registry rows can be removed.

A random pause of 0..--pause seconds before each repetition keeps the
loop from phase-locking to the block period (one iteration takes about one
block period, so without it every anchor lands at the same point in the slot).

Usage: python manage.py bench_pipeline --repeat 30 --label d0 --out bench_out/pipeline_d0.json
"""
import json
import os
import random
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from supply_chain.chain_client import anchor_dpp, verify_anchor
from supply_chain.credential_builders import build_dpp
from supply_chain.issuer import IssuerError, delete_credential_everywhere, issue_credential
from supply_chain.models import ProductBatch
from supply_chain.registry_client import register_dpp
from supply_chain.views import _verify_node


class Command(BaseCommand):
    help = "Time sign / register / anchor / verify for repeated DPP issuance."

    def add_arguments(self, parser):
        parser.add_argument('--repeat', type=int, default=30)
        parser.add_argument('--label', default='')
        parser.add_argument('--out', required=True)
        parser.add_argument('--pause', type=float, default=2.0, help='max random pause before each repetition, s')
        parser.add_argument('--seed', type=int, default=1)

    def handle(self, *args, **options):
        public_url = settings.NEOCYCLEHUB_PUBLIC_URL
        products = list(ProductBatch.objects.filter(actor__idhub_token__gt='')
                        .select_related('actor').order_by('id'))
        rows = []
        random.seed(options['seed'])
        for i in range(options['repeat']):
            product = products[i % len(products)]
            schema_name, endpoint, subject = build_dpp(product, public_url)
            row = {'i': i, 'product': product.name, 'actor': product.actor.name}
            time.sleep(random.uniform(0, options['pause']))
            try:
                t = time.perf_counter()
                issued = issue_credential(actor=product.actor, cred_type='DPP', endpoint=endpoint,
                                          schema_name=schema_name, credential_subject=subject,
                                          label=product.name, product=product, metrics_mode='benchmark')
                row['sign_ms'] = (time.perf_counter() - t) * 1000

                t = time.perf_counter()
                register_dpp(product.actor, product, issued, public_url)
                row['register_ms'] = (time.perf_counter() - t) * 1000

                t = time.perf_counter()
                anchor_dpp(product.actor, product, issued, public_url)
                row['anchor_ms'] = (time.perf_counter() - t) * 1000
                issued.refresh_from_db()
                row['anchored'] = bool(issued.chain_tx_hash)
                row['block'] = issued.chain_block_number

                t = time.perf_counter()
                v = _verify_node(issued)
                row['verify_sig_ms'] = (time.perf_counter() - t) * 1000
                row['verify_sig'] = v['status']

                t = time.perf_counter()
                va = verify_anchor(issued)
                row['verify_anchor_ms'] = (time.perf_counter() - t) * 1000
                row['verify_anchor'] = va['status']

                row['credential_bytes'] = len(json.dumps(issued.credential))
                row['uuid'] = str(issued.uuid)
                delete_credential_everywhere(issued)
            except IssuerError as e:
                row['error'] = str(e)[:200]
            rows.append(row)
            self.stdout.write(json.dumps({k: (round(v, 1) if isinstance(v, float) else v)
                                          for k, v in row.items() if k != 'product'}))

        os.makedirs(os.path.dirname(os.path.abspath(options['out'])), exist_ok=True)
        with open(options['out'], 'w') as f:
            json.dump({'label': options['label'], 'rows': rows}, f, indent=1)
        self.stdout.write(self.style.SUCCESS(f"wrote {options['out']}"))
