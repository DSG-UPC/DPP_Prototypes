"""
Build UNTP v0.7.0 credentialSubject dicts for each credential type.
Each function returns (schema_name, credential_type_key, subject_payload)
to be passed directly to the idhub signing API.
"""
from datetime import date


UNTP_CONTEXT = "https://vocabulary.uncefact.org/untp/0.7.0/context/"

SCHEMAS = {
    'DPP': 'DigitalProductPassport.json',
    'DTE': 'DigitalTraceabilityEvent.json',
    'DCC': 'ConformityCredential.json',
    'DFR': 'DigitalFacilityRecord.json',
    'DIA': 'DigitalIdentityAnchor.json',
}

IDHUB_ENDPOINTS = {
    'DPP': 'issue-dpp/',
    'DTE': 'issue-traceability/',
    'DCC': 'issue-conformity/',
    'DFR': 'issue-facility/',
    'DIA': 'issue-identity-anchor/',
}


def build_dpp(product, public_url, related_documents=None):
    """Build DPP credentialSubject for a ProductBatch."""
    actor = product.actor
    facility_id = actor.website or f"{public_url}/actors/{actor.pk}/facility/"
    facility_name = product.facility_name_override or f"{actor.name} — Main Facility"
    id_scheme_id = product.id_scheme_id or "https://id.gs1.org/01/"
    id_scheme_name = product.id_scheme_name or "Global Trade Identification Number (GTIN)"
    subject = {
        "@context": [UNTP_CONTEXT],
        "validUntil": "2034-01-01T00:00:00Z",
        "name": f"Digital Product Passport — {product.name}",
        "type": ["Product"],
        "id": product.gtin,
        "description": product.description,
        "idScheme": {
            "type": ["IdentifierScheme"],
            "id": id_scheme_id,
            "name": id_scheme_name,
        },
        "modelNumber": product.model_number,
        "batchNumber": product.batch_number,
        "idGranularity": product.id_granularity,
        "productCategory": product.product_category(),
        "producedAtFacility": {
            "type": ["Facility"],
            "id": facility_id,
            "name": facility_name,
        },
        "countryOfProduction": {
            "countryCode": actor.country_code,
        },
    }

    # UNTP standard field: materialProvenance (recycled content and origin)
    if product.recycled_mass_fraction is not None:
        subject["materialProvenance"] = [{
            "name": product.name,
            "originCountry": {"countryCode": actor.country_code},
            "materialType": {
                "code": product.cpc_code or "42940",
                "name": product.cpc_name or "Recycled rare earth material",
                "schemeId": "https://unstats.un.org/unsd/classifications/Econ/cpc/",
                "schemeName": "UN Central Product Classification (CPC)",
            },
            "massFraction": 1.0,
            "recycledMassFraction": product.recycled_mass_fraction,
        }]

    # UNTP standard field: characteristics (industry-specific extension — upstreamProcess)
    if product.upstream_process_type:
        subject["characteristics"] = {
            "upstreamProcess": {
                "type": product.upstream_process_type,
                "sourceMaterial": product.upstream_source_material,
                "sourceBatch": product.upstream_source_batch,
                "processDate": str(product.upstream_process_date) if product.upstream_process_date else None,
            }
        }

    if related_documents:
        subject["relatedDocument"] = related_documents

    return SCHEMAS['DPP'], 'issue-dpp/', subject


def build_dte(event, public_url):
    """Build DTE credentialSubject list for a ProcessingEvent."""
    format_dt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    actor = event.actor
    facility_id = actor.website or f"{public_url}/actors/{actor.pk}/facility/"
    facility_name = event.origin_facility_name_override or f"{actor.name} — Main Facility"

    def product_item(p):
        return {
            "product": {
                "type": ["Product"],
                "id": p.gtin,
                "name": p.name,
                "idGranularity": p.id_granularity or "batch",
            }
        }

    # Collect DPP credential URLs for relatedDocument links
    all_products = list(event.input_products.all()) + list(event.output_products.all())
    related_docs = []
    for p in all_products:
        cred = p.credentials.filter(cred_type='DPP').order_by('-issued_at').first()
        if cred:
            related_docs.append({
                "linkURL": f"{public_url}/credentials/{cred.uuid}/",
                "linkName": f"DPP — {p.name}",
                "linkType": "digitalProductPassport",
            })

    activity_code = event.activity_code or "commissioning"
    activity_name = event.activity_name or "Commissioning"
    activity_scheme_id = event.activity_scheme_id or "https://ref.gs1.org/cbv/BizStep"
    activity_scheme_name = event.activity_scheme_name or "GS1 CBV Business Step"

    subject_event = {
        "type": [event.event_type, "LifecycleEvent"],
        "id": f"{public_url}/events/{event.pk}/",
        "name": f"Digital Traceability Event — {event.name}",
        "description": event.description,
        "eventDate": format_dt(event.event_date),
        "activityType": {
            "code": activity_code,
            "name": activity_name,
            "schemeId": activity_scheme_id,
            "schemeName": activity_scheme_name,
        },
        "relatedParty": [{
            "role": "manufacturer",
            "party": {"id": actor.did, "name": actor.name},
        }],
    }

    if event.event_type == 'MakeEvent':
        subject_event["inputProduct"] = [product_item(p) for p in event.input_products.all()]
        subject_event["outputProduct"] = [product_item(p) for p in event.output_products.all()]
        subject_event["madeAtFacility"] = {"id": facility_id, "name": facility_name}

    elif event.event_type == 'ModifyEvent':
        # input_products = before, output_products = after; combined as modifiedProduct
        modified = list(event.input_products.all()) + list(event.output_products.all())
        subject_event["modifiedProduct"] = [product_item(p) for p in modified]
        subject_event["modifiedAtFacility"] = {"id": facility_id, "name": facility_name}

    elif event.event_type == 'MoveEvent':
        subject_event["movedProduct"] = [product_item(p) for p in event.input_products.all()]
        subject_event["fromFacility"] = {"id": facility_id, "name": facility_name}
        dest_id = event.destination_facility_id or f"{public_url}/actors/{actor.pk}/destination/"
        dest_name = event.destination_facility_name or "Destination Facility"
        subject_event["toFacility"] = {"id": dest_id, "name": dest_name}

    if related_docs:
        subject_event["relatedDocument"] = related_docs

    return SCHEMAS['DTE'], 'issue-traceability/', [subject_event]


def build_dcc(claim, public_url):
    """Build DCC credentialSubject for a ConformityClaim."""
    performance_score_code = claim.performance_score_code or "pass"
    conformity_topic_id = claim.conformity_topic_id or f"{claim.standard_id}#topic"
    conformity_topic_name = claim.conformity_topic_name or "Environmental Sustainability"
    # UNTP v0.7.0 context defines these terms only in type-scoped contexts,
    # so each node needs its type for JSON-LD expansion (UNTP Playground).
    subject = {
        "type": ["ConformityAttestation"],
        "id": f"{public_url}/conformity/{claim.pk}/",
        "name": claim.name,
        "description": claim.description,
        "assessorLevel": claim.assessor_level,
        "assessmentLevel": claim.assessment_level,
        "attestationType": claim.attestation_type,
        "issuedToParty": {
            "type": ["Party"],
            "id": claim.actor.website or claim.actor.did,
            "name": claim.actor.name,
            "registeredId": claim.actor.registered_id,
        },
        "referenceScheme": {
            "id": claim.standard_id,
            "name": claim.standard_name,
        },
        "conformityAssessment": [{
            "type": ["ConformityAssessment"],
            "id": f"{public_url}/conformity/{claim.pk}/assessment/",
            "name": f"{claim.standard_name} — Assessment",
            "assessmentCriteria": [{
                "id": claim.standard_id,
                "name": claim.standard_name,
            }],
            "assessmentDate": claim.valid_from.strftime("%Y-%m-%d"),
            "assessedPerformance": [{
                "score": {"code": performance_score_code},
            }],
            "conformityTopic": [{
                "id": conformity_topic_id,
                "name": conformity_topic_name,
            }],
        }],
    }
    return SCHEMAS['DCC'], 'issue-conformity/', subject


def build_dfr(actor, form):
    """Build DFR credentialSubject for a facility operated by an Actor."""
    facility_id = form.get('facility_id') or f"{actor.did}/facility"
    subject = {
        "type": ["Facility"],
        "id": facility_id,
        "name": form.get('facility_name') or f"{actor.name} — Main Facility",
        "description": form.get('facility_description', ''),
        "registeredId": actor.registered_id,
        "operationalStatus": "operating",
        "countryOfOperation": {
            "countryCode": actor.country_code,
        },
        "processCategory": [{
            "code": form.get('process_code') or "E38.3",
            "name": form.get('process_name') or "Material recovery",
            "schemeId": form.get('process_scheme_id') or "https://ec.europa.eu/eurostat/ramon/nomenclatures/NACE_REV2",
            "schemeName": form.get('process_scheme_name') or "NACE Rev.2",
        }],
        "locationInformation": {
            "geoLocation": {
                "latitude": float(form.get('latitude') or 48.8566),
                "longitude": float(form.get('longitude') or 2.3522),
            }
        },
        "address": {
            "streetAddress": form.get('street_address') or "1 Rue de la Récupération",
            "postalCode": form.get('postal_code') or "75001",
            "addressLocality": form.get('city') or "Paris",
            "addressRegion": form.get('region') or "Île-de-France",
            "addressCountry": {
                "countryCode": actor.country_code,
            },
        },
        "operatedByParty": {
            "id": actor.did,
            "name": actor.name,
        },
    }
    return SCHEMAS['DFR'], 'issue-facility/', subject


def build_dia(actor, registrar_did, registrar_name, registrar_url, register_type='business'):
    """Build DIA credentialSubject anchoring an Actor's corporate identity."""
    scheme_id = actor.id_scheme_id or "https://vocabulary.uncefact.org/identifierSchemes/GLEIF"
    scheme_name = actor.id_scheme_name or "GLEIF LEI"

    subject = {
        "type": ["RegisteredIdentity"],
        "id": actor.did,
        "name": f"Digital Identity Anchor — {actor.name}",
        "registeredName": actor.name,
        "registeredId": actor.registered_id or actor.lei_id or actor.did,
        "registeredDate": (actor.registered_date or date.today()).strftime("%Y-%m-%d"),
        "registerType": register_type if register_type in (
            'product', 'facility', 'business', 'trademark', 'land', 'accreditation'
        ) else 'business',
        "idScheme": {
            "id": scheme_id,
            "name": scheme_name,
        },
    }

    if registrar_url:
        subject["registrar"] = {
            "id": registrar_url,
            "name": registrar_name or registrar_url,
        }

    if actor.website:
        subject["publicInformation"] = actor.website

    return SCHEMAS['DIA'], 'issue-identity-anchor/', subject
