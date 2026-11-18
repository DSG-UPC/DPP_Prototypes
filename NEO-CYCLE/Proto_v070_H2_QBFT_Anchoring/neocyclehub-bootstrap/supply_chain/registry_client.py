"""
Client for the mock EU DPP Registry (see eu-registry/). Mirrors issuer.py's
shape (neocyclehub calling out to a separate service) — this one indexes a
DPP after it's signed, it never signs or verifies anything itself.
"""
import base64
import json
import logging

import requests

from django.conf import settings

logger = logging.getLogger(__name__)


def _decode_envelope_vc(credential):
    """
    Locally decode a JOSE-enveloped credential's JWT payload — no signature
    check, this only needs validFrom/validUntil for the registry index, not
    to verify authenticity (idhub's /verify/ already does that elsewhere).
    """
    envelope = credential.get('verifiableCredential', credential)
    if envelope.get('type') != 'EnvelopedVerifiableCredential':
        return credential

    data_uri = envelope.get('id', '')
    if 'data:application/vc+jwt,' not in data_uri:
        return {}

    token = data_uri.split('data:application/vc+jwt,', 1)[1]
    try:
        payload_b64 = token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}

    return payload.get('vc', payload)


def register_dpp(actor, product, issued_credential, public_url):
    """
    Mock of ESPR's mandatory registration step: "Before a product can be
    placed on the EU market, the relevant economic operator must register
    its DPP in the Registry." Sends only index/metadata — gtin, issuer,
    validity, and a pointer back to where the actual DPP is hosted — never
    the DPP content itself, mirroring the real registry's decentralized-
    content / centralized-index split.

    Best-effort: a registry outage shouldn't block the DPP itself from
    being issued, so failures are logged, not raised.
    """
    vc = _decode_envelope_vc(issued_credential.credential)

    payload = {
        "gtin": product.gtin,
        "product_name": product.name,
        "issuer_did": actor.did,
        "issuer_name": actor.name,
        "valid_from": vc.get("validFrom", ""),
        "valid_until": vc.get("validUntil", ""),
        "dpp_url": f"{public_url}/credentials/{issued_credential.uuid}/",
    }

    try:
        resp = requests.post(
            f"{settings.EU_REGISTRY_URL}/api/register/",
            json=payload, timeout=10,
        )
        resp.raise_for_status()
        logger.info("Registered DPP %s with EU Registry", issued_credential.uuid)
    except requests.RequestException as e:
        logger.warning("EU Registry registration failed for %s: %s", issued_credential.uuid, e)
