"""
Checks the anchoring threat model against the running prototype:
for each manipulation, does the signature check (idhub /verify/, i.e.
signatures + did:web + status list) detect it, and does the anchor check
(verify_anchor, or an eth_getLogs lookup by hash) detect it?

  storage_tamper   a stored credential is edited without re-signing
                   (outsider or storage corruption)
  silent_replace   the issuer re-signs changed content and serves it under
                   the URL of the original credential
  deletion         the issuer deletes a credential after issuance
  timing           gap between the issuer-asserted validFrom and the
                   consensus timestamp of the anchoring block, for every
                   anchored DPP in the scenario (bounds back-dating)

Every test credential is deleted at the end. Its registry row is removed
by experiments/run_threats.sh, its on-chain anchor stays (append-only).

Usage: python manage.py threat_tests --out bench_out/threats.json
"""
import base64
import copy
import json
import os
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand
from web3 import Web3

from supply_chain.chain_client import (LOGS_RANGE, _ensure_actor_account, _load_deployment, _node_rpc_url, anchor_dpp,
                                       verify_anchor, verify_anchor_strict)
from supply_chain.credential_builders import build_dpp
from supply_chain.issuer import delete_credential_everywhere, issue_credential
from supply_chain.models import IssuedCredential, ProductBatch
from supply_chain.registry_client import _decode_envelope_vc, register_dpp
from supply_chain.views import _verify_node


def _hash(credential):
    return Web3.keccak(text=json.dumps(credential, sort_keys=True))


def _logs_for_hash(actor, h):
    w3 = Web3(Web3.HTTPProvider(_node_rpc_url(actor)))
    dep = _load_deployment()
    c = w3.eth.contract(address=dep["address"], abi=dep["abi"])
    # Query in LOGS_RANGE windows: Besu refuses eth_getLogs ranges over 5000 blocks
    # by default (--rpc-max-logs-range), so fromBlock=0 fails on a longer chain.
    head, logs, start = w3.eth.block_number, [], 0
    while start <= head:
        end = min(start + LOGS_RANGE - 1, head)
        logs += c.events.Anchored().get_logs(fromBlock=start, toBlock=end, argument_filters={"dppHash": h})
        start = end + 1
    return [{"block": l["blockNumber"], "dppUrl": l["args"]["dppUrl"], "actor": l["args"]["actor"],
             "timestamp": l["args"]["timestamp"]} for l in logs]


class Command(BaseCommand):
    help = "Run the anchoring threat-model checks against the live prototype."

    def add_arguments(self, parser):
        parser.add_argument('--out', required=True)
        parser.add_argument('--foreign-contract', dest='foreign_contract', default='')

    def _issue(self, product, subject_patch=None):
        schema_name, endpoint, subject = build_dpp(product, self.public_url)
        if subject_patch:
            subject_patch(subject)
        issued = issue_credential(actor=product.actor, cred_type='DPP', endpoint=endpoint,
                                  schema_name=schema_name, credential_subject=subject,
                                  label=f"[threat-test] {product.name}", product=product,
                                  metrics_mode='benchmark')
        register_dpp(product.actor, product, issued, self.public_url)
        anchor_dpp(product.actor, product, issued, self.public_url)
        issued.refresh_from_db()
        self.created.append(str(issued.uuid))
        return issued

    def _checks(self, cred):
        return {"signature_check": _verify_node(cred)["status"], "anchor_check": verify_anchor(cred)["status"],
                "anchor_check_strict": verify_anchor_strict(cred)["status"]}

    def _anchor_as_issuer(self, actor, credential, url, contract_address=None):
        """The issuer sends its own anchor transaction (hash of `credential`, any URL),
        optionally to another contract with the same ABI. Returns the tx hash."""
        w3 = Web3(Web3.HTTPProvider(_node_rpc_url(actor)))
        dep = _load_deployment()
        c = w3.eth.contract(address=contract_address or dep["address"], abi=dep["abi"])
        address, key = _ensure_actor_account(actor)
        tx = c.functions.anchor(_hash(credential), url).build_transaction(
            {"from": address, "nonce": w3.eth.get_transaction_count(address), "gasPrice": 0})
        h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key=key).rawTransaction)
        w3.eth.wait_for_transaction_receipt(h, timeout=60)
        return h.hex()

    def handle(self, *args, **options):
        self.public_url = settings.NEOCYCLEHUB_PUBLIC_URL
        self.created = []
        # Leftovers of an aborted earlier run: delete them, and list their UUIDs
        # so that run_threats.sh also removes their registry rows.
        for c in IssuedCredential.objects.filter(label__startswith='[threat-test]'):
            self.created.append(str(c.uuid))
            delete_credential_everywhere(c)
        product = ProductBatch.objects.filter(actor__idhub_token__gt='', actor__role='supplier') \
            .select_related('actor').order_by('id').first()
        res = {"product_actor_role": product.actor.role}

        # Baseline: an untouched credential passes both checks.
        x = self._issue(product)
        res["baseline"] = self._checks(x)

        # storage_tamper: change one field of the stored credential, no re-signing.
        y = self._issue(product)
        # The stored credential is an EnvelopedVerifiableCredential whose "id" is a
        # data: URI holding the JWS. Change one field of the signed JWT payload and
        # keep the original signature (editing the wrapper would not touch signed data).
        tampered = copy.deepcopy(y.credential)
        prefix = 'data:application/vc+jwt,'
        head, payload, sig = tampered['id'][len(prefix):].split('.')
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        vc = claims.get('vc', claims)  # VC 2.0 vc+jwt: the payload is the credential
        vc['credentialSubject']['name'] = str(vc['credentialSubject'].get('name', '')) + ' (tampered)'
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
        tampered['id'] = prefix + '.'.join([head, payload, sig])
        IssuedCredential.objects.filter(pk=y.pk).update(credential=tampered)
        y.refresh_from_db()
        res["storage_tamper"] = self._checks(y)

        # silent_replace: issuer re-signs changed content, stores it under the original URL.
        original = self._issue(product)
        orig_hash = _hash(original.credential)

        def patch(subject):
            subject["name"] = subject.get("name", "") + " (revised)"
        replacement = self._issue(product, subject_patch=patch)
        IssuedCredential.objects.filter(pk=original.pk).update(credential=replacement.credential)
        original.refresh_from_db()
        res["silent_replace"] = self._checks(original)
        res["silent_replace"]["anchors_for_original_hash"] = len(_logs_for_hash(product.actor, orig_hash))
        res["silent_replace"]["note"] = ("the replacement was itself anchored under its own URL; a verifier that "
                                         "looks up the URL of the original finds the original hash, not the served one")

        # smart_replace: as silent_replace, but the issuer also anchors the new hash in
        # the real contract under the ORIGINAL URL and repoints its stored tx hash.
        s2 = self._issue(product)
        s2_url = f"{self.public_url}/credentials/{s2.uuid}/"
        r2 = self._issue(product, subject_patch=patch)
        tx = self._anchor_as_issuer(product.actor, r2.credential, s2_url)
        IssuedCredential.objects.filter(pk=s2.pk).update(credential=r2.credential, chain_tx_hash=tx)
        s2.refresh_from_db()
        res["smart_replace"] = self._checks(s2)

        # foreign_contract: the new anchor goes to another contract with the same ABI
        # (a contract the issuer deployed itself).
        foreign = options.get('foreign_contract')
        if foreign:
            s3 = self._issue(product)
            s3_url = f"{self.public_url}/credentials/{s3.uuid}/"
            r3 = self._issue(product, subject_patch=patch)
            tx = self._anchor_as_issuer(product.actor, r3.credential, s3_url, contract_address=foreign)
            IssuedCredential.objects.filter(pk=s3.pk).update(credential=r3.credential, chain_tx_hash=tx)
            s3.refresh_from_db()
            res["foreign_contract"] = self._checks(s3)

        # deletion: issuer deletes the credential; the anchor still proves it existed.
        z = self._issue(product)
        z_hash, z_url = _hash(z.credential), f"{self.public_url}/credentials/{z.uuid}/"
        delete_credential_everywhere(z)
        logs = _logs_for_hash(product.actor, z_hash)
        res["deletion"] = {
            "credential_still_served": IssuedCredential.objects.filter(uuid=z.uuid).exists(),
            "anchor_events_found": len(logs),
            "anchor_url_matches": bool(logs) and logs[0]["dppUrl"] == z_url,
        }

        # timing: validFrom (issuer-asserted) vs anchoring block timestamp (consensus).
        gaps = []
        for c in IssuedCredential.objects.filter(cred_type='DPP').exclude(chain_tx_hash__isnull=True) \
                .exclude(chain_tx_hash='').select_related('actor'):
            vc = _decode_envelope_vc(c.credential)
            vf = vc.get('validFrom')
            logs = _logs_for_hash(c.actor, _hash(c.credential))
            if vf and logs:
                t_vf = datetime.fromisoformat(vf.replace('Z', '+00:00')).timestamp()
                gaps.append(logs[0]["timestamp"] - t_vf)
        res["timing"] = {"n": len(gaps), "gap_s_min": min(gaps) if gaps else None,
                         "gap_s_max": max(gaps) if gaps else None,
                         "gap_s_median": sorted(gaps)[len(gaps) // 2] if gaps else None}

        # Strict check over all scenario passports (not the test ones).
        sc = IssuedCredential.objects.filter(cred_type='DPP').exclude(label__startswith='[').select_related('actor')
        res["scenario_strict"] = dict(__import__('collections').Counter(verify_anchor_strict(c)["status"] for c in sc))

        # Cleanup of the remaining test credentials.
        for c in IssuedCredential.objects.filter(uuid__in=self.created):
            delete_credential_everywhere(c)
        res["created_uuids"] = self.created

        os.makedirs(os.path.dirname(os.path.abspath(options['out'])), exist_ok=True)
        with open(options['out'], 'w') as f:
            json.dump(res, f, indent=1)
        self.stdout.write(json.dumps({k: v for k, v in res.items() if k != 'created_uuids'}, indent=1))
