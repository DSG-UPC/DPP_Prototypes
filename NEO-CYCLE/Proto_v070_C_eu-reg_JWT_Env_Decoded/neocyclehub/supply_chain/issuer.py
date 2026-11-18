"""
Call the idhub signing API to issue a UNTP credential and store it.
"""
import logging
import requests

from django.conf import settings
from .models import Actor, IssuedCredential

logger = logging.getLogger(__name__)


class IssuerError(Exception):
    pass


def _post_to_idhub(actor: Actor, endpoint: str, payload: dict) -> dict:
    url = f"{actor.idhub_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        'Authorization': f'Bearer {actor.idhub_token}',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30, verify=False)
        resp.raise_for_status()
        data = resp.json()
        cred = data.get('credential')
        if not cred:
            raise IssuerError(f"idhub returned success but no 'credential' key: {data}")
        return cred
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        raise IssuerError(f"idhub API error [{e.response.status_code}]: {detail}") from e
    except requests.ConnectionError as e:
        raise IssuerError(f"Cannot reach idhub at {url}: {e}") from e


def issue_credential(
    actor: Actor,
    cred_type: str,
    endpoint: str,
    schema_name: str,
    credential_subject,
    label: str,
    product=None,
    event=None,
    conformity_claim=None,
) -> IssuedCredential:
    """
    Issue a UNTP credential via idhub and store the result.

    credential_subject: dict (DPP/DCC/DFR/DIA) or list of dicts (DTE)
    """
    payload = {
        'schema_name': schema_name,
        'issuer_did': actor.did,
        'credentialSubject': credential_subject,
    }

    signed = _post_to_idhub(actor, endpoint, payload)

    issued = IssuedCredential.objects.create(
        actor=actor,
        cred_type=cred_type,
        label=label,
        credential=signed,
        product=product,
        event=event,
        conformity_claim=conformity_claim,
    )
    logger.info("Issued %s credential %s for actor %s", cred_type, issued.uuid, actor.name)
    return issued
