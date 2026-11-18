#!/usr/bin/env python3

import json
import logging
import jsonschema
import nacl.signing
import nacl.exceptions
import multicodec
import multiformats
from ninja import NinjaAPI
from typing import List, Any, Optional

from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.db import transaction
from django.conf import settings
from idhub.models import DID, Schemas, VerificableCredential, Event, verify_jose_vc
from .schemas import (
    UpdateServiceEndpointPayload, UpdateServiceEndpointResponse,
    CreateObjectDIDPayload, CreateObjectDIDResponse,
    IssueDPPayload, IssueTraceabilityPayload, IssueFacilityPayload,
    IssueConformityPayload, IssueIdentityAnchorPayload,
    SignedCredentialResponse, ErrorResponse,
    VerifyPayload, VerifyResponse,
    DeleteCredentialPayload, DeleteCredentialResponse,
)
from idhub.admin.forms import DIDForm
from ninja.security import HttpBearer
from webhook.models import Token
from pyvckit.verify import verify_schema, get_message, get_signing_input, jws_split, get_pubkey_bytes_from_diddocument

api_v1 = NinjaAPI(version='1.0.0', title="IdHub v1 API")

logger = logging.getLogger(__name__)

@api_v1.exception_handler(ValueError)
def handle_value_error(request, exc):
    return api_v1.create_response(request, {"error": str(exc)}, status=400)

@api_v1.exception_handler(PermissionDenied)
def handle_permission_denied(request, exc):
    return api_v1.create_response(request, {"error": str(exc)}, status=403)


class DatabaseTokenAuth(HttpBearer):
    def authenticate(self, request, token_string):
        try:
            token_obj = Token.objects.select_related('owner').get(
                token=token_string, active=True
            )
            request.user = token_obj.owner
            return request.user
        except Exception as e:
            logger.warning(f"Authentication failed: {e}")
            return None


def _find_issuer_did(requested_did: str, user):
    if not requested_did:
        raise ValueError(f"Requested Issuer DID '{requested_did}' not found on this server.")

    issuer = DID.objects.filter(did=requested_did, user__isnull=True, is_product=False).first()
    if not issuer:
        raise PermissionDenied(f"You do not own the Issuer DID '{requested_did}' or it doesnt exist.")
    return issuer


@api_v1.post("object-did/create/",
             response={200: CreateObjectDIDResponse, 201: CreateObjectDIDResponse, 400: ErrorResponse, 500: ErrorResponse},
             summary="Create a new Object DID",
             auth=DatabaseTokenAuth())
def create_object_did(request, payload: CreateObjectDIDPayload):
    try:
        expected_did = f"did:web:{settings.DOMAIN}:{payload.suffix_did_id}"
        existing_did = DID.objects.filter(did=expected_did, is_product=True).first()

        if existing_did:
            if payload.service_endpoint and existing_did.service_endpoint != payload.service_endpoint:
                existing_did.service_endpoint = payload.service_endpoint
                existing_did.save(update_fields=['service_endpoint', ])

            doc_json = json.loads(existing_did.didweb_document) if existing_did.didweb_document else {}
            return 200, {
                "did": existing_did.did,
                "did_document": doc_json
            }

        form_data = {
            'label': payload.label or f"device-{payload.suffix_did_id}",
            'type': DID.Types.WEB.value,
            'did': expected_did
        }

        unsaved_instance = DID(type=DID.Types.WEB)
        form = DIDForm(data=form_data, instance=unsaved_instance)

        if not form.is_valid():
            return 400, {
                "error": "Validation failed",
                "details": json.dumps(form.errors.get_json_data())
            }

        form.instance.user = request.user
        form.instance.is_product = True
        form.instance.service_endpoint = payload.service_endpoint

        obj_did = form.save(commit=True)
        obj_did.set_did()
        obj_did.save()
        doc_json = json.loads(obj_did.didweb_document) if obj_did.didweb_document else {}

        return 201, {
            "did": obj_did.did,
            "did_document": doc_json
        }
    except Exception as e:
        logger.error(f"Failed to create Object DID: {e}", exc_info=True)
        return 500, {"error": "Internal server error during DID creation."}


@api_v1.post("object-did/update/",
             response={200: UpdateServiceEndpointResponse, 403: ErrorResponse, 404: ErrorResponse},
             summary="Update the Service Endpoint of an existing DID",
             auth=DatabaseTokenAuth())
def update_did_service_endpoint(request, payload: UpdateServiceEndpointPayload):

    did_obj = DID.objects.filter(did=payload.did, user=request.user, is_product=True ).first()

    if not did_obj:
        return 404, {"error": "DID not found or you do not have permission to modify it."}

    try:
        did_obj.service_endpoint = payload.service_endpoint
        did_obj.save(update_fields=['service_endpoint', ])

        return 200, {
            "success": True,
            "did": did_obj.did,
            "service_endpoint": did_obj.service_endpoint
        }

    except Exception as e:
        logger.error(f"Failed to update Service Endpoint: {e}", exc_info=True)
        return 500, {"error": "Internal server error updating the DID."}

    

@api_v1.post("issue-dpp/",
             response={201: SignedCredentialResponse, 400: ErrorResponse, 422: ErrorResponse, 500: ErrorResponse},
             summary="Issue Digital Product Passport", auth=DatabaseTokenAuth())
def issue_dpp_credential(request, payload: IssueDPPayload):
    issuer_did = _find_issuer_did(payload.issuer_did, request.user)

    #only one dpp service_endpoint at a time
    cleaned_subject = payload.credentialSubject.copy()
    subject_did_str = cleaned_subject.get("id")
    did_obj = DID.objects.filter(did=subject_did_str, is_product=True).first()


    return process_credential_issuance(
        request, payload.schema_name, cleaned_subject,
        ["VerifiableCredential", "DigitalProductPassport"],
        did_obj, issuer_did
    )


@api_v1.post("issue-facility/",
    response={201: SignedCredentialResponse, 400: ErrorResponse, 422: ErrorResponse, 500: ErrorResponse},
    summary="Issue Digital Facility Record", auth=DatabaseTokenAuth())
def issue_facility_credential(request, payload: IssueFacilityPayload):
    issuer_did = _find_issuer_did(payload.issuer_did, request.user)

    return process_credential_issuance(
        request, payload.schema_name, payload.credentialSubject,
        ["VerifiableCredential", "DigitalFacilityRecord"],
        #This is self_signing, should another reputable did sign this?
        issuer_did, issuer_did
    )


@api_v1.post("issue-traceability/",
    response={201: SignedCredentialResponse, 400: ErrorResponse, 422: ErrorResponse, 500: ErrorResponse},
    summary="Issue Traceability Event Batch", auth=DatabaseTokenAuth())
def issue_traceability_credential(request, payload: IssueTraceabilityPayload):
    issuer_did = _find_issuer_did(payload.issuer_did, request.user)
    events_list = payload.credentialSubject

    if not events_list:
        raise ValueError('credentialSubject must be a non-empty list of events.')

    first_event_id = events_list[0].get("id")
    if not first_event_id:
        raise ValueError('The first event in the list must have a valid "id".')

    did_obj = DID.objects.filter(did=first_event_id, is_product=True).first()

    return process_credential_issuance(
        request=request,
        schema_name=payload.schema_name,
        subject_data=events_list,
        credential_type=["VerifiableCredential", "DigitalTraceabilityEvent"],
        subject_did=did_obj,
        issuer_did_obj=issuer_did
    )


def process_credential_issuance(
    request,
    schema_name: str,
    subject_data: Any,
    credential_type: List[str],
    subject_did,
    issuer_did_obj
):
    try:
        schema = Schemas.objects.get(file_schema=schema_name)
    except Schemas.DoesNotExist:
        return 422, {'error': f"Schema '{schema_name}' does not exist."}

    with transaction.atomic():

        domain = f"https://{settings.DOMAIN}/"
        cred = VerificableCredential(
            verified=True, user=request.user, json_data=subject_data,
            issuer_did=issuer_did_obj, schema=schema,
            type= credential_type
        )

        try:
            rendered_json_str = cred.render(domain)
            valid, error_details = verify_schema(rendered_json_str, verify=not settings.DEBUG)

            if not valid:
                return 400, {'error': 'Schema validation failed.', 'details': error_details}

            #For now force usage of subject did, not any other type of id
            cred.issue(did=subject_did if subject_did else None, domain=domain)
            cred.save()

        except jsonschema.exceptions.ValidationError as e:
            return 400, {'error': 'Schema validation failed.', 'details': e.message, 'path': list(e.path)}
        except Exception as e:
            logger.error(f"Issuance failed: {e}", exc_info=True)
            return 500, {'error': 'Internal server error during credential signing.'}

    return 201, {"credential": json.loads(cred.get_data())}


@api_v1.post("issue-conformity/",
    response={201: SignedCredentialResponse, 400: ErrorResponse, 422: ErrorResponse, 500: ErrorResponse},
    summary="Issue Digital Conformity Credential", auth=DatabaseTokenAuth())
def issue_conformity_credential(request, payload: IssueConformityPayload):
    issuer_did = _find_issuer_did(payload.issuer_did, request.user)

    return process_credential_issuance(
        request, payload.schema_name, payload.credentialSubject,
        ["VerifiableCredential", "DigitalConformityCredential"],
        issuer_did, issuer_did
    )


@api_v1.post("issue-identity-anchor/",
    response={201: SignedCredentialResponse, 400: ErrorResponse, 422: ErrorResponse, 500: ErrorResponse},
    summary="Issue Digital Identity Anchor", auth=DatabaseTokenAuth())
def issue_identity_anchor(request, payload: IssueIdentityAnchorPayload):
    issuer_did = _find_issuer_did(payload.issuer_did, request.user)

    return process_credential_issuance(
        request, payload.schema_name, payload.credentialSubject,
        ["VerifiableCredential", "DigitalIdentityAnchor"],
        issuer_did, issuer_did
    )


@api_v1.post("verify/",
    response={200: VerifyResponse, 400: ErrorResponse},
    summary="Verify the cryptographic signature of a signed credential",
    auth=None)
def verify_credential_api(request, payload: VerifyPayload):
    """
    Verifies the Ed25519 JWS proof of a credential using idhub's local DID
    registry — no outbound HTTP calls needed, so demo DIDs work correctly.
    """
    try:
        vc = payload.credential

        # Accept both the current top-level EnvelopedVerifiableCredential
        # shape and the older {"verifiableCredential": {...}} nesting.
        envelope = vc.get("verifiableCredential", vc)
        if envelope.get("type") == "EnvelopedVerifiableCredential":
            valid, detail = verify_jose_vc(envelope)
            return 200, {"valid": valid, "detail": detail}

        proof = vc.get("proof", {})
        if not proof.get("jws"):
            return 200, {"valid": False, "detail": "No JWS proof found in credential"}

        vm = proof.get("verificationMethod", "")
        did_str = vm.split("#")[0]

        if did_str.startswith("did:web:"):
            # Resolve from idhub's own DB — avoids HTTP to localhost:80 which fails
            did_obj = DID.objects.filter(did=did_str).first()
            if not did_obj or not did_obj.didweb_document:
                return 200, {"valid": False, "detail": f"DID not registered in this idhub: {did_str}"}
            did_doc = json.loads(did_obj.didweb_document)
            pub_bytes = get_pubkey_bytes_from_diddocument(did_str, did_document=did_doc)
            if not pub_bytes:
                return 200, {"valid": False, "detail": "Could not extract public key from DID document"}

        elif did_str.startswith("did:key:"):
            # Public key is encoded in the DID string itself
            raw = did_str.split(":")[-1]
            mc = multiformats.multibase.decode(raw)
            pub_bytes = multicodec.remove_prefix(mc)

        else:
            return 200, {"valid": False, "detail": f"Unsupported DID method: {did_str}"}

        verify_key = nacl.signing.VerifyKey(pub_bytes)

        # Canonicalise + hash the credential body (same steps as signing)
        jws_padded, message = get_message(vc)
        if message is False or not jws_padded:
            return 200, {"valid": False, "detail": "Could not extract JWS from proof"}

        _, signing_input = get_signing_input(message)
        _, signature_bytes = jws_split(jws_padded)

        # nacl expects: signature_bytes (64 B) || signed_data
        try:
            verify_key.verify(signature_bytes + signing_input)
            return 200, {"valid": True, "detail": f"Signature valid (DID: {did_str})"}
        except nacl.exceptions.BadSignatureError:
            return 200, {"valid": False, "detail": "Signature cryptographically invalid"}

    except Exception as e:
        logger.error(f"Credential verification error: {e}", exc_info=True)
        return 400, {"error": f"Verification failed: {e}"}


@api_v1.post("credential/delete/",
    response={200: DeleteCredentialResponse, 404: ErrorResponse, 400: ErrorResponse},
    summary="Delete a VerificableCredential from idhub by matching its proof.jws",
    auth=DatabaseTokenAuth())
def delete_credential_api(request, payload: DeleteCredentialPayload):
    """
    Finds the stored VerificableCredential whose signed data matches the
    provided credential's proof.jws (unique per Ed25519 signature), deletes
    it, and records an audit event.  The caller must supply a valid bearer
    token issued to the same user who owns the credential.
    """
    try:
        target_jws = payload.credential.get("proof", {}).get("jws", "")
        if not target_jws:
            return 400, {"error": "Credential has no proof.jws — cannot identify it"}

        # Pre-filter by issuer DID to keep the search fast
        issuer_raw = payload.credential.get("issuer", {})
        issuer_did_str = issuer_raw.get("id", issuer_raw) if isinstance(issuer_raw, dict) else issuer_raw
        issuer_did_obj = DID.objects.filter(did=issuer_did_str).first()

        vc_qs = (
            VerificableCredential.objects.filter(issuer_did=issuer_did_obj, user=request.user)
            if issuer_did_obj
            else VerificableCredential.objects.filter(user=request.user)
        )

        for vc in vc_qs:
            try:
                data_str = vc.get_data()
                if not data_str:
                    continue
                stored = json.loads(data_str)
                if stored.get("proof", {}).get("jws") == target_jws:
                    Event.set_EV_CREDENTIAL_DELETED(vc)
                    vc_pk = vc.pk
                    vc.delete()
                    return 200, {"deleted": True, "detail": f"Credential (idhub pk={vc_pk}) deleted"}
            except Exception:
                continue

        return 404, {"error": "No matching credential found in idhub for this token"}

    except Exception as e:
        logger.error(f"Credential deletion error: {e}", exc_info=True)
        return 400, {"error": f"Deletion failed: {e}"}
