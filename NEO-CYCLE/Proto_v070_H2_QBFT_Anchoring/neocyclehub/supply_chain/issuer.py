"""
Call the idhub signing API to issue a UNTP credential and store it.
"""
import logging
import requests

from django.conf import settings
from .metrics import record_timing
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
    metrics_mode="passive",
) -> IssuedCredential:
    """
    Issue a UNTP credential via idhub and store the result.

    credential_subject: dict (DPP/DCC/DFR/DIA) or list of dicts (DTE)

    metrics_mode: "passive" (default, a real UI/issue_all action) or
    "benchmark" (a controlled, repeated timing run — see
    management/commands/benchmark_issuance.py). Only affects how the timing
    sample is tagged in the metrics dashboard, not the issuance
    itself.
    """
    payload = {
        'schema_name': schema_name,
        'issuer_did': actor.did,
        'credentialSubject': credential_subject,
    }

    with record_timing(f"issue_{cred_type.lower()}", mode=metrics_mode, cred_type=cred_type, actor_name=actor.name, label=label):
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


def delete_credential_everywhere(issued: IssuedCredential) -> tuple[bool, str | None]:
    """
    Delete a credential from both idhub and neocyclehub — shared by the
    "Delete" button (views.py::delete_credential) and
    management/commands/benchmark_issuance.py, which issues-then-deletes on
    every repeat so a benchmark run doesn't leave duplicate credentials
    behind. Returns (idhub_deleted, idhub_error) — the local row is always
    removed regardless of whether the idhub side succeeded, same as the
    view's prior behavior.
    """
    actor = issued.actor
    idhub_deleted = False
    idhub_error = None
    try:
        url = actor.idhub_url.rstrip('/') + '/credential/delete/'
        resp = requests.post(
            url,
            json={'credential': issued.credential},
            headers={'Authorization': f'Bearer {actor.idhub_token}'},
            timeout=15,
            verify=False,
        )
        if resp.status_code == 200:
            idhub_deleted = True
        else:
            idhub_error = resp.json().get('error', f'HTTP {resp.status_code}')
    except Exception as e:
        idhub_error = str(e)[:120]

    issued.delete()
    return idhub_deleted, idhub_error
