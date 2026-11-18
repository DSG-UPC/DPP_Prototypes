"""
Client for anchoring a hash of an issued DPP on the 4-node Besu QBFT network
(see blockchain/, blockchain/besu-config/, and CHAIN_ANCHOR.md). Mirrors
registry_client.py's shape — a single best-effort call right after a DPP is
signed — but writes to the chain as the *issuing actor's own* identity, not
through a shared service: each Actor gets its own generated Ethereum
keypair, held here in neocyclehub (never in idhub), the same way each Actor
already holds its own did:web identity. Each actor also submits through its
role's own validator node (NODE_BY_ROLE), not a single shared entrypoint —
one node per supplier/upcycler/manufacturer role, with several actors
sharing each role's node — except UPC, which has its own dedicated node
(_node_rpc_url below).
"""
import json
import logging

from django.conf import settings
from eth_account import Account
from web3 import Web3

from .models import Actor

logger = logging.getLogger(__name__)

_deployment_cache = None

# One Besu validator per issuing role (see docker-compose.yml /
# blockchain/besu-config/). Nine actors share the three role nodes —
# S1/S2 on 'supplier', U1/U2/U3/U4/U5 on 'upcycler',
# M1/M2 on 'manufacturer'. Three validators alone would tolerate zero
# Byzantine faults (N=3f+1 needs N=4 for f=1); the fourth node below fixes that.
NODE_BY_ROLE = {
    Actor.Role.SUPPLIER: "http://besu-supplier:8545",
    Actor.Role.UPCYCLER: "http://besu-upcycler:8545",
    Actor.Role.MANUFACTURER: "http://besu-manufacturer:8545",
}

# UPC's own dedicated validator node -- a per-company split off a shared
# role node, which brings the network to N=4 and so to the N>=3f+1
# threshold for f=1. UPC operates this demonstrator, so it has the lowest
# onboarding cost, and the split relieves the most crowded node (six
# companies would otherwise share 'upcycler'). Matched by name
# since Actor has no dedicated per-company routing field; every other
# upcycler-role actor still shares besu-upcycler.
_UPC_ACTOR_NAME = "Universitat Politecnica de Catalunya (UPC)"
_UPC_NODE_RPC_URL = "http://besu-upc:8545"


def _node_rpc_url(actor):
    if actor.name == _UPC_ACTOR_NAME:
        return _UPC_NODE_RPC_URL
    return NODE_BY_ROLE.get(actor.role, settings.CHAIN_RPC_URL)


def _load_deployment():
    global _deployment_cache
    if _deployment_cache is None:
        with open(settings.CHAIN_DEPLOYMENT_FILE) as f:
            _deployment_cache = json.load(f)
    return _deployment_cache


def _ensure_actor_account(actor):
    """
    Lazily generates and persists a fresh Ethereum keypair for this actor on
    first use. Unlike the Hardhat setup this replaces, no funding step is
    needed: the Besu network runs with --min-gas-price=0 (see
    docker-compose.yml), so a brand-new, zero-balance account can submit a
    transaction immediately.
    """
    if actor.chain_address and actor.chain_private_key:
        return actor.chain_address, actor.chain_private_key

    account = Account.create()
    actor.chain_address = account.address
    actor.chain_private_key = account.key.hex()
    actor.save(update_fields=["chain_address", "chain_private_key"])
    logger.info("Generated chain account %s for actor %s", account.address, actor.name)
    return actor.chain_address, actor.chain_private_key


def anchor_dpp(actor, product, issued_credential, public_url):
    """
    Anchors keccak256(credential JSON) + the DPP's URL on the DPPAnchor
    contract, signed by the issuing actor's own chain account and submitted
    through that actor's own validator node — the on-chain counterpart to
    registry_client.register_dpp()'s off-chain index entry, but written by
    the actor itself.

    Best-effort: a chain outage shouldn't block the DPP itself from being
    issued, so failures are logged, not raised.
    """
    try:
        rpc_url = _node_rpc_url(actor)
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        address, private_key = _ensure_actor_account(actor)

        deployment = _load_deployment()
        contract = w3.eth.contract(address=deployment["address"], abi=deployment["abi"])

        dpp_hash = Web3.keccak(text=json.dumps(issued_credential.credential, sort_keys=True))
        dpp_url = f"{public_url}/credentials/{issued_credential.uuid}/"

        tx = contract.functions.anchor(dpp_hash, dpp_url).build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address),
            "gasPrice": 0,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

        issued_credential.chain_tx_hash = tx_hash.hex()
        issued_credential.chain_block_number = receipt.blockNumber
        issued_credential.save(update_fields=["chain_tx_hash", "chain_block_number"])
        logger.info("Anchored DPP %s on chain: tx %s (via %s)", issued_credential.uuid, tx_hash.hex(), rpc_url)
    except Exception as e:
        logger.warning("Chain anchoring failed for %s: %s", issued_credential.uuid, e)


def verify_anchor(issued_credential):
    """
    Live on-chain re-verification of a DPP's anchor — recomputes
    keccak256(credential JSON) and compares it against the Anchored event
    actually recorded on the Besu network, rather than trusting the
    chain_tx_hash/chain_block_number fields stored locally at issuance time.

    Before this existed, _build_chain() (views.py) only ever *displayed*
    those two stored fields — it never re-queried Besu, so a chain-walk's
    "⛓ anchored" badge meant "a tx hash was recorded once," not "this
    credential's anchor still checks out right now." This is the actual
    tamper-evidence check CHAIN_ANCHOR.md describes (recompute the hash,
    compare against the on-chain event), now callable on demand instead of
    only as a manual curl/shell recipe.

    Only meaningful for DPPs — DTE/DCC/DFR/DIA are never anchored
    (anchor_dpp() is only ever called from _issue_dpp).

    Returns {status, detail}:
    - "not_anchored" — no chain_tx_hash recorded (anchoring was never
      attempted, or failed at issuance time — anchor_dpp() is best-effort).
    - "valid" — a receipt exists, the transaction succeeded, and the
      on-chain hash matches this credential's current stored content.
    - "invalid" — a receipt exists but reverted, or the on-chain hash
      doesn't match (tampering, or the credential row changed since
      anchoring — should never happen here, since credentials are
      immutable once issued, but this is what would catch it if it did).
    - "unavailable" — the chain couldn't be reached to check (Besu down,
      RPC error) — explicitly distinct from "invalid": absence of evidence
      is not evidence of absence.
    """
    if not issued_credential.chain_tx_hash:
        return {"status": "not_anchored", "detail": "This DPP has no recorded anchor transaction"}

    try:
        actor = issued_credential.actor
        rpc_url = _node_rpc_url(actor)
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        receipt = w3.eth.get_transaction_receipt(issued_credential.chain_tx_hash)
        if receipt is None:
            return {"status": "unavailable", "detail": f"Anchor tx not found on {rpc_url}"}
        if receipt.status != 1:
            return {"status": "invalid", "detail": "Anchor transaction reverted on-chain"}

        deployment = _load_deployment()
        contract = w3.eth.contract(address=deployment["address"], abi=deployment["abi"])
        events = contract.events.Anchored().process_receipt(receipt)
        if not events:
            return {"status": "invalid", "detail": "No Anchored event found in this transaction's receipt"}

        onchain_hash = events[0]["args"]["dppHash"]
        recomputed_hash = Web3.keccak(text=json.dumps(issued_credential.credential, sort_keys=True))

        if onchain_hash == recomputed_hash:
            return {"status": "valid", "detail": f"On-chain hash matches (block {receipt.blockNumber}, via {rpc_url})"}
        return {"status": "invalid", "detail": "On-chain hash does not match this credential's current content"}
    except Exception as e:
        return {"status": "unavailable", "detail": str(e)[:160]}


LOGS_RANGE = 5000  # Besu default for --rpc-max-logs-range


def verify_anchor_strict(issued_credential):
    """
    Anchor check that does not trust any pointer the issuer stored itself
    (used in the threat-model evaluation, see experiments/README.md).

    verify_anchor() above follows the issuer-stored chain_tx_hash and accepts
    an Anchored event from whatever contract emitted it. An issuer that
    silently replaces a credential can therefore anchor the new hash (in the
    real contract under the old URL, or in a contract of its own) and update
    chain_tx_hash, and verify_anchor() reports "valid". This check instead
      1. reads Anchored events only from the pinned contract address,
      2. only events SENT BY the issuing actor's registered chain address
         (`actor` is an indexed topic, so the node filters them),
      3. takes the EARLIEST of those recorded for this credential's URL,
      4. compares its hash with the hash of the credential being served.
    Step 2 matters: taking the earliest event for the URL from ANY sender and
    then checking the sender lets another consortium member squat a URL by
    anchoring junk under it first, which makes the honest passport fail
    (threat_tests 'squat' shows both rules).
    Cost: dppUrl is not an indexed event field, so step 3 scans the issuer's
    events in 5000-block windows; the first anchor per (issuer, URL) kept in
    contract state would make it a single call.
    """
    try:
        actor = issued_credential.actor
        w3 = Web3(Web3.HTTPProvider(_node_rpc_url(actor)))
        deployment = _load_deployment()
        contract = w3.eth.contract(address=deployment["address"], abi=deployment["abi"])
        url = f"{settings.NEOCYCLEHUB_PUBLIC_URL}/credentials/{issued_credential.uuid}/"
        if not actor.chain_address:
            return {"status": "invalid", "detail": "Issuer has no registered chain address"}
        issuer = Web3.to_checksum_address(actor.chain_address)
        # Besu refuses eth_getLogs ranges over 5000 blocks by default (--rpc-max-logs-range),
        # so a single fromBlock=0 query fails once the chain is longer.
        head, logs, start = w3.eth.block_number, [], 0
        while start <= head:
            end = min(start + LOGS_RANGE - 1, head)
            logs += [l for l in contract.events.Anchored().get_logs(fromBlock=start, toBlock=end,
                                                                    argument_filters={"actor": issuer})
                     if l["args"]["dppUrl"] == url and l["address"] == contract.address]
            start = end + 1
        if not logs:
            return {"status": "not_anchored", "detail": "No anchor by the issuer for this URL in the pinned contract"}
        first = min(logs, key=lambda l: (l["blockNumber"], l["logIndex"]))
        recomputed = Web3.keccak(text=json.dumps(issued_credential.credential, sort_keys=True))
        if first["args"]["dppHash"] == recomputed:
            return {"status": "valid", "detail": f"Earliest issuer anchor for this URL matches (block {first['blockNumber']}, "
                                                 f"{len(logs)} anchor(s) for the URL)"}
        return {"status": "invalid", "detail": f"Served credential differs from the earliest issuer anchor for this URL "
                                               f"({len(logs)} anchor(s) for the URL)"}
    except Exception as e:
        return {"status": "unavailable", "detail": str(e)[:160]}
