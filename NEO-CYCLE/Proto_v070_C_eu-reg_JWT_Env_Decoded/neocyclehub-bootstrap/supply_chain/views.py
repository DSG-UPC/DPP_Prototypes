import base64
import json
import logging

import requests as http_client

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST

from .models import Actor, ProductBatch, ProcessingEvent, ConformityClaim, IssuedCredential
from .credential_builders import build_dpp, build_dte, build_dcc, build_dfr, build_dia
from .issuer import issue_credential, IssuerError
from .registry_client import register_dpp

logger = logging.getLogger(__name__)

PUBLIC_URL = settings.NEOCYCLEHUB_PUBLIC_URL


# ─── Credential serving (public JSON-LD endpoint) ────────────────────────────

def serve_credential(request, uuid):
    cred = get_object_or_404(IssuedCredential, uuid=uuid)
    response = JsonResponse(cred.credential)
    response['Content-Type'] = 'application/ld+json'
    response['Content-Disposition'] = f'inline; filename="{cred.cred_type}-{uuid}.json"'
    response['Access-Control-Allow-Origin'] = '*'
    return response


# ─── Dashboard ────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    actors = Actor.objects.prefetch_related('credentials').all()
    return render(request, 'supply_chain/dashboard.html', {'actors': actors})


@login_required
def actor_detail(request, pk):
    actor = get_object_or_404(Actor, pk=pk)
    credentials = IssuedCredential.objects.filter(actor=actor).order_by('-issued_at')
    products = ProductBatch.objects.filter(actor=actor)
    events = ProcessingEvent.objects.filter(actor=actor)
    claims = ConformityClaim.objects.filter(actor=actor)
    all_dpps = None
    if actor.role == 'auditor':
        all_dpps = (IssuedCredential.objects
                    .filter(cred_type='DPP')
                    .select_related('actor')
                    .order_by('actor__name', '-issued_at'))
    return render(request, 'supply_chain/actor_detail.html', {
        'actor': actor,
        'credentials': credentials,
        'products': products,
        'events': events,
        'claims': claims,
        'public_url': PUBLIC_URL,
        'all_dpps': all_dpps,
    })


def _verify_node(cred):
    """POST the stored credential to idhub's verify endpoint. Returns {status, detail}."""
    try:
        url = cred.actor.idhub_url.rstrip('/') + '/verify/'
        resp = http_client.post(
            url,
            json={'credential': cred.credential},
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                'status': 'valid' if data.get('valid') else 'invalid',
                'detail': data.get('detail', ''),
            }
        return {'status': 'error', 'detail': f'idhub returned HTTP {resp.status_code}'}
    except http_client.exceptions.Timeout:
        return {'status': 'unavailable', 'detail': 'idhub did not respond in time'}
    except Exception as e:
        return {'status': 'unavailable', 'detail': str(e)[:120]}


def _credential_subject(credential):
    """
    credentialSubject for a stored credential, whatever securing shape it's
    in. JOSE-enveloped credentials (EnvelopedVerifiableCredential) don't have
    it at the top level — it's inside the signed JWT's "vc" claim — so decode
    that locally. No signature check here: this is for reading
    relatedDocument links to walk the chain, not for verifying authenticity
    (_verify_node does that, via idhub).
    """
    envelope = credential.get('verifiableCredential', credential)
    if envelope.get('type') == 'EnvelopedVerifiableCredential':
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
        return payload.get('vc', payload).get('credentialSubject', {})

    return credential.get('credentialSubject', {})


def _decode_jwt_envelope(credential):
    """
    Decode a JOSE-enveloped credential's JWT header and payload for display —
    same base64url-decode approach as _credential_subject() above, but
    returns the full header/payload rather than just credentialSubject, for
    a human to actually read what was signed. No signature check here
    either (_verify_node does that, via idhub); this is display-only.
    Returns None if the credential isn't a JOSE envelope in the expected
    shape (e.g. a Data Integrity proof credential instead).
    """
    envelope = credential.get('verifiableCredential', credential)
    if envelope.get('type') != 'EnvelopedVerifiableCredential':
        return None
    data_uri = envelope.get('id', '')
    if 'data:application/vc+jwt,' not in data_uri:
        return None
    token = data_uri.split('data:application/vc+jwt,', 1)[1]
    try:
        header_b64, payload_b64, _signature = token.split('.')
        header_b64 += '=' * (-len(header_b64) % 4)
        payload_b64 += '=' * (-len(payload_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_b64))
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    return {'token': token, 'header': header, 'payload': payload}


def _build_chain(cred, visited=None):
    """Recursively follow relatedDocument links and build a chain tree dict."""
    if visited is None:
        visited = set()
    uid = str(cred.uuid)
    if uid in visited:
        return None
    visited.add(uid)

    node = {
        'uuid': uid,
        'cred_type': cred.cred_type,
        'label': cred.label,
        'actor': cred.actor.name,
        'url': f"{PUBLIC_URL}/credentials/{cred.uuid}/",
        'chain_url': f"/credentials/{cred.uuid}/chain/",
        'verification': _verify_node(cred),
        'children': [],
    }

    subject = _credential_subject(cred.credential)
    if isinstance(subject, list):
        related_docs = []
        for ev in subject:
            if isinstance(ev, dict):
                related_docs.extend(ev.get('relatedDocument', []))
    elif isinstance(subject, dict):
        related_docs = subject.get('relatedDocument', [])
    else:
        related_docs = []

    for doc in related_docs:
        link_url = doc.get('linkURL', '')
        parts = link_url.rstrip('/').split('/')
        child_uid = parts[-1] if parts else ''
        if not child_uid or child_uid in visited:
            continue
        try:
            child_cred = IssuedCredential.objects.select_related('actor').get(uuid=child_uid)
            child_node = _build_chain(child_cred, visited)
            if child_node:
                node['children'].append(child_node)
        except IssuedCredential.DoesNotExist:
            node['children'].append({
                'uuid': child_uid,
                'cred_type': 'EXT',
                'label': doc.get('linkName', link_url),
                'actor': 'External',
                'url': link_url,
                'chain_url': None,
                'verification': {'status': 'unavailable', 'detail': 'External credential — not verifiable here'},
                'children': [],
            })

    return node


@login_required
def credential_chain(request, uuid):
    cred = get_object_or_404(IssuedCredential.objects.select_related('actor'), uuid=uuid)
    chain = _build_chain(cred)
    return render(request, 'supply_chain/credential_chain.html', {
        'root': cred,
        'chain_json': json.dumps(chain),
        'public_url': PUBLIC_URL,
    })


@login_required
def decode_credential(request, uuid):
    """
    Human-readable view of what was actually signed — the decoded JOSE
    header and JWT payload, not the opaque data:application/vc+jwt,... blob
    the raw JSON endpoint serves. Decode-only, no signature verification
    (see credential_chain's live idhub verify for that).
    """
    cred = get_object_or_404(IssuedCredential.objects.select_related('actor'), uuid=uuid)
    decoded = _decode_jwt_envelope(cred.credential)
    return render(request, 'supply_chain/credential_decoded.html', {
        'cred': cred,
        'decoded_header': json.dumps(decoded['header'], indent=2, ensure_ascii=False) if decoded else None,
        'decoded_payload': json.dumps(decoded['payload'], indent=2, ensure_ascii=False) if decoded else None,
        'raw_envelope': json.dumps(cred.credential, indent=2, ensure_ascii=False),
        'public_url': PUBLIC_URL,
    })


@login_required
@require_POST
def delete_credential(request, uuid):
    cred = get_object_or_404(IssuedCredential.objects.select_related('actor'), uuid=uuid)
    actor = cred.actor

    idhub_deleted = False
    idhub_error = None
    try:
        url = actor.idhub_url.rstrip('/') + '/credential/delete/'
        resp = http_client.post(
            url,
            json={'credential': cred.credential},
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

    cred.delete()

    if idhub_deleted:
        messages.success(request, f"Credential deleted from neocyclehub and idhub.")
    else:
        messages.warning(request, f"Credential deleted locally only — idhub: {idhub_error}")

    return redirect('actor_detail', pk=actor.pk)


@login_required
def edit_actor(request, pk):
    actor = get_object_or_404(Actor, pk=pk)
    if request.method == 'POST':
        actor.did = request.POST.get('did', actor.did).strip()
        actor.idhub_url = request.POST.get('idhub_url', actor.idhub_url).strip()
        actor.idhub_token = request.POST.get('idhub_token', actor.idhub_token).strip()
        actor.registered_id = request.POST.get('registered_id', actor.registered_id).strip()
        actor.lei_id = request.POST.get('lei_id', actor.lei_id).strip()
        actor.website = request.POST.get('website', actor.website).strip()
        actor.country_code = request.POST.get('country_code', actor.country_code).strip()
        actor.country_name = request.POST.get('country_name', actor.country_name).strip()
        actor.save()
        messages.success(request, f'Actor "{actor.name}" updated.')
        return redirect('actor_detail', pk=pk)
    return render(request, 'supply_chain/edit_actor.html', {'actor': actor})


# ─── Issue actions ────────────────────────────────────────────────────────────

@login_required
@require_POST
def issue_dpp(request, product_pk):
    product = get_object_or_404(ProductBatch, pk=product_pk)
    actor = product.actor

    related_docs = []
    for ev_cred in IssuedCredential.objects.filter(event__output_products=product, cred_type='DTE'):
        related_docs.append({
            "linkURL": f"{PUBLIC_URL}/credentials/{ev_cred.uuid}/",
            "linkName": ev_cred.label,
            "mediaType": "application/ld+json",
            "linkType": "https://test.uncefact.org/vocabulary/linkTypes/dte",
        })

    schema_name, endpoint, subject = build_dpp(product, PUBLIC_URL, related_docs or None)

    try:
        issued = issue_credential(
            actor=actor,
            cred_type='DPP',
            endpoint=endpoint,
            schema_name=schema_name,
            credential_subject=subject,
            label=product.name,
            product=product,
        )
        register_dpp(actor, product, issued, PUBLIC_URL)
        messages.success(request, f"DPP issued: {issued.uuid}")
    except IssuerError as e:
        messages.error(request, f"Failed to issue DPP: {e}")

    return redirect('actor_detail', pk=actor.pk)


@login_required
@require_POST
def issue_dte(request, event_pk):
    event = get_object_or_404(ProcessingEvent, pk=event_pk)
    actor = event.actor

    schema_name, endpoint, subject = build_dte(event, PUBLIC_URL)

    try:
        issued = issue_credential(
            actor=actor,
            cred_type='DTE',
            endpoint=endpoint,
            schema_name=schema_name,
            credential_subject=subject,
            label=event.name,
            event=event,
        )
        messages.success(request, f"DTE issued: {issued.uuid}")
    except IssuerError as e:
        messages.error(request, f"Failed to issue DTE: {e}")

    return redirect('actor_detail', pk=actor.pk)


@login_required
@require_POST
def issue_dcc(request, claim_pk):
    claim = get_object_or_404(ConformityClaim, pk=claim_pk)
    actor = claim.actor

    schema_name, endpoint, subject = build_dcc(claim, PUBLIC_URL)

    try:
        issued = issue_credential(
            actor=actor,
            cred_type='DCC',
            endpoint=endpoint,
            schema_name=schema_name,
            credential_subject=subject,
            label=claim.standard_name,
            conformity_claim=claim,
        )
        messages.success(request, f"DCC issued: {issued.uuid}")
    except IssuerError as e:
        messages.error(request, f"Failed to issue DCC: {e}")

    return redirect('actor_detail', pk=actor.pk)


@login_required
@require_POST
def issue_dfr(request, actor_pk):
    actor = get_object_or_404(Actor, pk=actor_pk)
    schema_name, endpoint, subject = build_dfr(actor, request.POST)

    try:
        issued = issue_credential(
            actor=actor,
            cred_type='DFR',
            endpoint=endpoint,
            schema_name=schema_name,
            credential_subject=subject,
            label=subject.get('name', actor.name),
        )
        messages.success(request, f"DFR issued: {issued.uuid}")
    except IssuerError as e:
        messages.error(request, f"Failed to issue DFR: {e}")

    return redirect('actor_detail', pk=actor.pk)


@login_required
@require_POST
def issue_dia(request, actor_pk):
    actor = get_object_or_404(Actor, pk=actor_pk)
    registrar_did = request.POST.get('registrar_did', '')
    registrar_name = request.POST.get('registrar_name', '')
    registrar_url = request.POST.get('registrar_url', '')

    schema_name, endpoint, subject = build_dia(
        actor, registrar_did, registrar_name, registrar_url
    )

    try:
        issued = issue_credential(
            actor=actor,
            cred_type='DIA',
            endpoint=endpoint,
            schema_name=schema_name,
            credential_subject=subject,
            label=f"Identity Anchor — {actor.name}",
        )
        messages.success(request, f"DIA issued: {issued.uuid}")
    except IssuerError as e:
        messages.error(request, f"Failed to issue DIA: {e}")

    return redirect('actor_detail', pk=actor.pk)
