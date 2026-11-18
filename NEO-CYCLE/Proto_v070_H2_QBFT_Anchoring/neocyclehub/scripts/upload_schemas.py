#!/usr/bin/env python3
"""
Download UNTP v0.7.0 schemas from the official spec and upload them to idhub.
Run after idhub is up: python scripts/upload_schemas.py
"""
import json
import sys
import requests

IDHUB_URL = "http://localhost:8000"
ADMIN_EMAIL = "admin@neocycle.local"
ADMIN_PASSWORD = "admin1234"

UNTP_CONTEXT = "https://vocabulary.uncefact.org/untp/0.7.0/context/"

# Official UNTP v0.7.0 schema URLs -> local filename used in idhub
SCHEMA_SOURCES = {
    "untp-dpp-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dpp/DigitalProductPassport.json",
    "untp-dte-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dte/DigitalTraceabilityEvent.json",
    "untp-dcc-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dcc/ConformityCredential.json",
    "untp-dfr-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dfr/DigitalFacilityRecord.json",
    "untp-dia-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dia/DigitalIdentityAnchor.json",
}


def get_csrf(session, url):
    session.get(url)
    return session.cookies.get('csrftoken')


def login(session):
    csrf = get_csrf(session, f"{IDHUB_URL}/login/")
    resp = session.post(f"{IDHUB_URL}/login/", data={
        'username': ADMIN_EMAIL,
        'password': ADMIN_PASSWORD,
        'csrfmiddlewaretoken': csrf,
    }, headers={'Referer': f"{IDHUB_URL}/login/"}, allow_redirects=True)
    if '/login/' in resp.url:
        print("Login failed — check ADMIN_EMAIL/ADMIN_PASSWORD", file=sys.stderr)
        sys.exit(1)
    print(f"Logged in as {ADMIN_EMAIL}")


def upload_schema(session, filename, url):
    # idhub has no Django built-in admin (/admin/ 404s) — it has its own
    # custom admin views. Schema registration lives at /admin/schemas/upload;
    # its "Enable from URL" tab (ImportSchemaUrlForm, submit name "submitUrl")
    # fetches the schema itself server-side and derives file_schema from the
    # URL's last path segment — e.g. ".../dpp/DigitalProductPassport.json"
    # becomes file_schema="DigitalProductPassport.json", matching what
    # credential_builders.py's SCHEMAS dict looks up by. It also stores
    # context_url on Schemas.context, which the old /admin/idhub/schemas/add/
    # target (Django admin's URL convention — never actually wired up in this
    # app) never supported in the first place.
    csrf = get_csrf(session, f"{IDHUB_URL}/admin/schemas/upload")
    resp = session.post(
        f"{IDHUB_URL}/admin/schemas/upload",
        data={
            'csrfmiddlewaretoken': csrf,
            'schema_url': url,
            'context_url': UNTP_CONTEXT,
            'submitUrl': 'Save',
        },
        headers={'Referer': f"{IDHUB_URL}/admin/schemas/upload"},
        allow_redirects=True,
    )
    if resp.status_code in (200, 302) and 'upload' not in resp.url:
        print(f"  Uploaded: {filename}")
        return True
    if 'exist' in resp.text.lower():
        print(f"  Already exists: {filename}")
        return True
    print(f"  Warning — unexpected response for {filename}: {resp.status_code} {resp.url}", file=sys.stderr)
    return False


def main():
    session = requests.Session()
    session.verify = False

    print("\nLogging in to idhub ...")
    login(session)

    print("\nUploading schemas (idhub fetches each URL itself) ...")
    for filename, url in SCHEMA_SOURCES.items():
        upload_schema(session, filename, url)

    print("\nDone. Schemas available at http://localhost:8000/admin/schemas/")


if __name__ == '__main__':
    main()
