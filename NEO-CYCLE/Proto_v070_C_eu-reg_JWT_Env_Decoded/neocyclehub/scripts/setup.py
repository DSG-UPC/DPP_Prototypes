#!/usr/bin/env python3
"""
Full NeoCycle demo setup.
Run once after both containers are up:
    pip install requests
    python scripts/setup.py

Container names are auto-discovered from Docker's own Compose labels (works
regardless of which compose file started the stack, or whether it pins
`container_name:` — docker-compose-remote.yml deliberately doesn't, to avoid
collisions on a shared host, so containers get Compose's auto-generated
names instead). Set NEOCYCLEHUB_CONTAINER / IDHUB_CONTAINER explicitly to
skip discovery — required if more than one deployment of this stack is
running on the same Docker host at once (discovery can't disambiguate that).
"""
import json
import os
import re
import subprocess
import sys
import requests


def _discover_container(service, env_var, fallback):
    """
    Resolve the actual container name for a Compose service by its
    `com.docker.compose.service` label, so this doesn't depend on
    `container_name:` being pinned (docker-compose-remote.yml doesn't) or on
    being run from the directory holding the compose file that started it
    (local vs. remote use different files, and this script's CWD is
    `neocyclehub/`, which contains neither).
    """
    override = os.environ.get(env_var)
    if override:
        return override

    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.service={service}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, check=True,
        )
        names = [n for n in result.stdout.strip().splitlines() if n]
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  WARNING: could not query docker for service '{service}' ({e}); "
              f"falling back to '{fallback}'. Override with {env_var}=<container name> if wrong.",
              file=sys.stderr)
        return fallback

    if len(names) == 1:
        return names[0]

    if not names:
        print(f"  WARNING: no running container found for compose service '{service}'; "
              f"falling back to '{fallback}'. Is the stack up? Override with "
              f"{env_var}=<container name> if this guess is wrong.", file=sys.stderr)
        return fallback

    print(f"  ERROR: multiple containers found for compose service '{service}': "
          f"{', '.join(names)}", file=sys.stderr)
    print(f"  More than one deployment of this stack is running on this host — "
          f"set {env_var}=<the right one> and re-run.", file=sys.stderr)
    sys.exit(1)


IDHUB_URL = os.environ.get("IDHUB_URL", "http://localhost:8000")
IDHUB_EMAIL = os.environ.get("IDHUB_EMAIL", "admin@neocycle.local")
IDHUB_PASSWORD = os.environ.get("IDHUB_PASSWORD", "admin1234")
NEOCYCLEHUB_CONTAINER = _discover_container("neocyclehub", "NEOCYCLEHUB_CONTAINER", "neocyclehub")
IDHUB_CONTAINER = _discover_container("idhub", "IDHUB_CONTAINER", "idhub")

UNTP_CONTEXT = "https://vocabulary.uncefact.org/untp/0.7.0/context/"

SCHEMA_SOURCES = {
    "untp-dpp-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dpp/DigitalProductPassport.json",
    "untp-dte-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dte/DigitalTraceabilityEvent.json",
    "untp-dcc-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dcc/ConformityCredential.json",
    "untp-dfr-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dfr/DigitalFacilityRecord.json",
    "untp-dia-v070.json": "https://untp.unece.org/artefacts/schema/v0.7.0/dia/DigitalIdentityAnchor.json",
}

# Actor names must match the fixture exactly
ACTORS = [
    {"fixture_name": "Rare Earth Recycling Ltd.", "did_label": "supplier-rer", "token_label": "neocyclehub-supplier"},
    {"fixture_name": "Rare Earth Upcycling Ltd.", "did_label": "upcycler-reu", "token_label": "neocyclehub-upcycler"},
    {"fixture_name": "GreenFert Innovations SAS", "did_label": "manufacturer-greenfert", "token_label": "neocyclehub-greenfert"},
]


# ── helpers ──────────────────────────────────────────────────────────────────

def step(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print('='*60)


def get_csrf(session, url):
    r = session.get(url)
    m = re.search(r'csrfmiddlewaretoken.*?value=["\']([^"\']+)["\']', r.text)
    if m:
        return m.group(1)
    tok = session.cookies.get('csrftoken')
    if tok:
        return tok
    raise RuntimeError(f"No CSRF token found at {url}")


# ── 1. Login ─────────────────────────────────────────────────────────────────

def login(session):
    csrf = get_csrf(session, f"{IDHUB_URL}/login/")
    r = session.post(f"{IDHUB_URL}/login/", data={
        'username': IDHUB_EMAIL,
        'password': IDHUB_PASSWORD,
        'csrfmiddlewaretoken': csrf,
    }, headers={'Referer': f"{IDHUB_URL}/login/"}, allow_redirects=True)
    if '/login/' in r.url:
        print("  ERROR: idhub login failed — check IDHUB_EMAIL / IDHUB_PASSWORD", file=sys.stderr)
        sys.exit(1)
    print(f"  Logged in as {IDHUB_EMAIL}")


# ── 2. Upload UNTP schemas ────────────────────────────────────────────────────
#
# idhub has no Django built-in admin (/admin/ 404s) — it has its own custom
# admin views. Schema registration lives at /admin/schemas/upload, whose
# "Enable from URL" tab (ImportSchemaUrlForm, submit name "submitUrl") fetches
# the schema itself server-side and derives file_schema from the URL's last
# path segment — e.g. ".../dpp/DigitalProductPassport.json" becomes
# file_schema="DigitalProductPassport.json", which is exactly what
# credential_builders.py's SCHEMAS dict looks up by. It also stores context_url
# on the Schemas.context field, which the old /admin/idhub/schemas/add/ target
# (Django admin's URL convention — never actually wired up in this app) never
# supported in the first place.

def upload_schemas(session):
    for filename, url in SCHEMA_SOURCES.items():
        csrf = get_csrf(session, f"{IDHUB_URL}/admin/schemas/upload")
        r = session.post(
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
        if r.status_code in (200, 302) and 'upload' not in r.url:
            print(f"    Uploaded: {filename}")
        elif 'exist' in r.text.lower():
            print(f"    Already exists: {filename}")
        else:
            print(f"    Warning: unexpected {r.status_code} for {filename}")


# ── 3. Create org DID ─────────────────────────────────────────────────────────

def create_org_did(session, label):
    """GET the form (which pre-generates a DID in the session), then POST to save it."""
    form_url = f"{IDHUB_URL}/admin/dids/new/"

    # GET pre-populates session with a fresh DID
    r = session.get(form_url)
    csrf = re.search(r'csrfmiddlewaretoken.*?value=["\']([^"\']+)["\']', r.text)
    if not csrf:
        csrf = session.cookies.get('csrftoken')
    else:
        csrf = csrf.group(1)

    # Extract the pre-generated DID value from the form HTML
    did_match = re.search(r'name=["\']did["\'][^>]*value=["\']([^"\']+)["\']', r.text)
    if not did_match:
        did_match = re.search(r'id=["\']id_did["\'][^>]*value=["\']([^"\']+)["\']', r.text)
    if not did_match:
        # Try multiline
        did_match = re.search(r'id_did[^>]*value=["\']([^"\']+)["\']', r.text, re.DOTALL)
    if not did_match:
        print(f"    ERROR: could not extract pre-generated DID from form for label '{label}'")
        print(f"    Form HTML snippet: {r.text[r.text.find('id_did')-50:r.text.find('id_did')+200]}")
        sys.exit(1)

    did_value = did_match.group(1)

    r2 = session.post(form_url, data={
        'csrfmiddlewaretoken': csrf,
        'label': label,
        'type': '1',  # 1 = Web
        'did': did_value,
    }, headers={'Referer': form_url}, allow_redirects=True)

    if r2.url != form_url and '/new/' not in r2.url:
        print(f"    Created DID: {did_value}  (label: {label})")
        return did_value

    # Check for error message in response
    if 'already' in r2.text.lower() or 'exist' in r2.text.lower():
        # DID already exists; try to retrieve it from the list
        existing = get_did_by_label(session, label)
        if existing:
            print(f"    DID already exists: {existing}  (label: {label})")
            return existing

    print(f"    ERROR creating DID '{label}': status={r2.status_code} url={r2.url}")
    print(f"    Response snippet: {r2.text[:500]}")
    sys.exit(1)


def get_did_by_label(session, label):
    """
    Parse the identities table, which renders one <tr> per DID with the
    label and the did:web string in adjacent <td> cells:
        <td>supplier-rer</td>
        <td><a href="...">did:web:localhost:2WY1e8</a></td>
    Auto-generated DIDs don't contain the label text, so matching has to be
    done row-by-row rather than by substring search over all DIDs on the page.
    """
    r = session.get(f"{IDHUB_URL}/admin/wallet/identities/")
    rows = re.findall(r'<tr\b.*?</tr>', r.text, re.DOTALL)
    for row in rows:
        if f'>{label}<' not in row:
            continue
        did_match = re.search(r'(did:web:[^\s<"\']+)', row)
        if did_match:
            return did_match.group(1)
    return None


# ── 4. Create webhook token ───────────────────────────────────────────────────

def create_token(session, label):
    form_url = f"{IDHUB_URL}/webhook/tokens/new"
    csrf = get_csrf(session, form_url)
    r = session.post(form_url, data={
        'csrfmiddlewaretoken': csrf,
        'label': label,
    }, headers={'Referer': form_url}, allow_redirects=True)

    if r.status_code in (200, 302):
        # Retrieve the token UUID from the idhub DB via its container
        token = get_token_by_label_from_db(label)
        if token:
            print(f"    Created token for '{label}': {token}")
            return token

    print(f"    ERROR creating token '{label}': {r.status_code}")
    sys.exit(1)


def get_token_by_label_from_db(label):
    """Read token UUID from idhub's SQLite via docker exec."""
    cmd = [
        'docker', 'exec', IDHUB_CONTAINER,
        'python', '/opt/idhub/manage.py', 'shell', '-c',
        f"from webhook.models import Token; t=Token.objects.filter(label='{label}').order_by('-id').first(); print(t.token if t else '')"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # manage.py shell -c prints a startup banner (and any settings.py debug
    # prints) before running our code, so the token is the last non-empty line.
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    token = lines[-1].strip() if lines else ''
    return token if token else None


# ── 5. Update neocyclehub actors ──────────────────────────────────────────────

def update_actor(fixture_name, did, token):
    idhub_api_url = "http://idhub:8000/api/v1/"
    code = (
        f"from supply_chain.models import Actor; "
        f"a=Actor.objects.get(name='{fixture_name}'); "
        f"a.did='{did}'; "
        f"a.idhub_url='{idhub_api_url}'; "
        f"a.idhub_token='{token}'; "
        f"a.save(); "
        f"print(f'Updated: {{a.name}}')"
    )
    cmd = ['docker', 'exec', NEOCYCLEHUB_CONTAINER, 'python', 'manage.py', 'shell', '-c', code]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr}")
        sys.exit(1)
    print(f"    {result.stdout.strip()}")


# ── 6. Issue every credential in dependency order ─────────────────────────────

def issue_all():
    cmd = ['docker', 'exec', NEOCYCLEHUB_CONTAINER, 'python', 'manage.py', 'issue_all']
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    session = requests.Session()

    step("1. Login to idhub")
    login(session)

    step("2. Upload UNTP v0.7.0 schemas")
    upload_schemas(session)

    step("3. Create org DIDs in idhub")
    dids = {}
    for actor in ACTORS:
        dids[actor['fixture_name']] = create_org_did(session, actor['did_label'])

    step("4. Create webhook tokens in idhub")
    tokens = {}
    for actor in ACTORS:
        tokens[actor['fixture_name']] = create_token(session, actor['token_label'])

    step("5. Wire DIDs + tokens into NeoCycleHub actors")
    for actor in ACTORS:
        update_actor(
            fixture_name=actor['fixture_name'],
            did=dids[actor['fixture_name']],
            token=tokens[actor['fixture_name']],
        )

    step("6. Issue all credentials (DIA/DFR/DCC/DPP/DTE) for every actor")
    issue_all()

    step("Done!")
    print()
    print("  idhub admin:       http://localhost:8000  (admin@neocycle.local / admin1234)")
    print("  NeoCycleHub:       http://localhost:9000")
    print("  NeoCycleHub admin: http://localhost:9000/admin  (admin / admin1234)")
    print()
    print("  Every product, event, and conformity claim in the fixtures now has a")
    print("  signed credential, and the relatedDocument chain (Supplier -> Upcycler")
    print("  -> GreenFert) is fully linked. Browse http://localhost:9000 and use the")
    print("  'Chain' button on any credential to walk it.")


if __name__ == '__main__':
    main()
