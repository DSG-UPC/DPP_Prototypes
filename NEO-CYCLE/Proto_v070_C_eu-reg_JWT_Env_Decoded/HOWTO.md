# How to run the NeoCycle UNTP v0.7.0 demonstrator

This guide walks a third party through running this project from scratch:
starting the stack, provisioning it, and exploring/verifying the result. It
assumes no prior context beyond having this folder on your machine.

## What you're running

A demonstrator for issuing signed [UNTP](https://untp.unece.org/) (UN
Transparency Protocol) v0.7.0 verifiable credentials across a four-company
rare-earth-magnet recycling supply chain — a Recycler, an Upcycler, a
Fertilizer Manufacturer, and an Auditor. Each company issues its own signed
credentials (Digital Product Passports, Traceability Events, Conformity
Credentials, Facility Records, Identity Anchors) from its own `did:web`
identity, and the credentials cross-link into one provenance graph via
`relatedDocument`, even across company boundaries.

Two Django apps make this happen:

- **idhub** (port 8000) — a DID/Verifiable-Credential signing service. Every
  credential is actually signed here.
- **neocyclehub** (ports 9000 and 9001) — the demo app. It holds the fixture
  supply-chain data (companies, products, events) and calls idhub's API to
  get each credential signed. It does no cryptography itself. Port 9001
  (`neocyclehub-bootstrap`) is a Bootstrap-styled, login-gated variant of the
  same app; port 9000 is a plain/no-login variant. They're independent
  instances with separate databases but the same fixture data.

## Prerequisites

- Docker Engine and Docker Compose (`docker compose version` should print a
  version; if that command isn't found, try the older standalone
  `docker-compose` instead — this guide uses `docker compose`, substitute
  accordingly).
- Python 3 with the `requests` package (`pip install requests`) — only
  needed on your host machine, to run the one-time setup script.
- Ports 8000, 9000, and 9001 free on your machine.
- Outbound internet access from wherever `docker compose` runs — the setup
  script has idhub download the real UNTP v0.7.0 schemas from
  `untp.unece.org` at provisioning time.

## Step 1 — Start the stack

From this folder (the one containing `docker-compose.yml`):

```bash
docker compose up --build -d
```

This builds and starts three containers:

| Container | URL | Notes |
|---|---|---|
| `idhub` | http://localhost:8000 | signs all credentials |
| `neocyclehub` | http://localhost:9000 | plain UI, no login |
| `neocyclehub-bootstrap` | http://localhost:9001 | Bootstrap-styled UI, login required |

The first build takes several minutes (idhub's image installs a large
dependency list). Wait for idhub to report healthy before continuing:

```bash
docker compose ps
```

`idhub` should show `(healthy)`. `neocyclehub`/`neocyclehub-bootstrap` wait
for that automatically before starting.

## Step 2 — One-time setup

A script provisions everything: uploads the 5 UNTP v0.7.0 schemas to idhub
(fetched live from untp.unece.org), creates a `did:web` identity and a
Bearer token in idhub for each of the three credential-issuing companies,
wires those into NeoCycleHub's records, and issues every credential for
every company in the correct dependency order (so all `relatedDocument`
links — including the cross-company ones — resolve immediately).

```bash
cd neocyclehub
pip install requests
python scripts/setup.py
```

For the Bootstrap variant on port 9001, also run its own copy of the script
(it targets a different container and creates its own tokens; the idhub
identities are shared, the tokens are not):

```bash
cd ../neocyclehub-bootstrap
python scripts/setup.py
```

Expect output ending in a summary of every DIA/DFR/DCC/DTE/DPP credential
issued. The script is safe to re-run — it skips anything already provisioned
or already issued.

## Step 3 — Explore the result

Open either:

- http://localhost:9000 (no login), or
- http://localhost:9001 (login `admin` / `admin1234`)

Click into each company ("Manage credentials") to see its issued
credentials. On the Auditor's page, click "View chain" on the Green
Fertilizer DPP to walk its `relatedDocument` links back through every hop —
Manufacturer → Upcycler → Recycler — with each credential verified live
against idhub.

Every credential is also servable directly as JSON-LD:

```
http://localhost:9000/credentials/<uuid>/
```

idhub's own admin (schemas, DIDs, tokens) is at http://localhost:8000/,
login `admin@neocycle.local` / `admin1234`. Useful entry points:

- http://localhost:8000/admin/schemas/ — the 5 registered UNTP schemas
- http://localhost:8000/admin/wallet/identities/ — issued `did:web` identities
- http://localhost:8000/webhook/tokens/ — API bearer tokens

(There is no plain `http://localhost:8000/admin/` index page — idhub has
its own custom admin UI, not Django's built-in admin, so that bare URL
404s. Use the specific paths above, or navigate from the login landing
page.)

## Optional — independently verify a credential's signature

Every credential is a JOSE-enveloped Verifiable Credential (a signed JWT).
You don't have to trust the app's UI — you can verify one yourself:

```bash
# 1. Get a credential's envelope (grabs the first DPP found)
docker exec neocyclehub python manage.py shell -c "
from supply_chain.models import IssuedCredential
c = IssuedCredential.objects.filter(cred_type='DPP').first()
print(c.credential['id'])
"
# -> data:application/vc+jwt,eyJhbGciOiJFZERTQSIs...

# 2. Fetch the issuer's did:web document (kid header names it, e.g.
#    did:web:localhost:xwAgT9 resolves to /xwAgT9/did.json)
curl -s http://localhost:8000/<did-path>/did.json | python3 -m json.tool
```

```python
# 3. Verify the JWT's EdDSA signature against the resolved public key
import jwt, base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

jwt_str = "<paste the string after data:application/vc+jwt, here>"
x = "<paste publicKeyJwk.x from the did.json here>"

def b64d(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)

pub_key = Ed25519PublicKey.from_public_bytes(b64d(x))
decoded = jwt.decode(jwt_str, pub_key, algorithms=["EdDSA"], options={"verify_aud": False})
print("Signature valid. Issuer:", decoded.get("vc", decoded)["issuer"])  # payload = the credential (VC 2.0 vc+jwt)
```

If the signature has been tampered with, `jwt.decode` raises
`InvalidSignatureError` instead of returning the payload.

## Optional — check a credential's revocation status

Every credential's `vc.credentialStatus` points at a real, signed [W3C
Bitstring Status List](https://www.w3.org/TR/vc-bitstring-status-list/)
credential — one bit per credential, GZIP-compressed, at
`http://localhost:8000/credentials/status/<issuer-did-pk>/`. Fetch it
(`curl`, `application/vc+jwt` response) and decode the same way as above;
its `vc.credentialSubject.encodedList` is the bitstring, and
`credentialStatus.statusListIndex` on the original credential tells you
which bit to check. Revoking a credential (idhub admin → the credential →
Revoke) flips that bit immediately on the next fetch — no separate
republish step.

## Troubleshooting

- **Setup script fails with `idhub API error [401]: Unauthorized`** — the
  tokens NeoCycleHub has for its actors don't match any token in idhub
  (common after recreating containers under a different Docker Compose
  project name, which gives neocyclehub a fresh database while idhub's
  bind-mounted SQLite file is reused, or vice versa). Re-run
  `python scripts/setup.py` — it creates fresh tokens and rewires the actors.
- **Schema upload reports "Already exists" for all 5** — expected on a
  re-run against a database that already has them; not an error.
- **"Issue" fails with a connection error in the manual/dashboard flow** —
  idhub isn't up yet, or the actor's `idhub_token`/`idhub_url` is wrong;
  check http://localhost:9000/admin/supply_chain/actor/.
- **Reset NeoCycleHub only** (keep idhub's schemas/DIDs/tokens) —
  `docker compose down -v` removes the `neocyclehub`/`neocyclehub-bootstrap`
  named volumes, then `docker compose up -d` and re-run
  `scripts/setup.py` (it reuses idhub's existing DIDs by label and creates
  fresh tokens).
- **Reset idhub too** (fresh schemas/DIDs from scratch) — idhub's database
  is a bind mount at `idhub/db.sqlite3` on the host, not a Docker volume, so
  `-v` doesn't touch it. Stop the stack, delete that file, then restart from
  Step 1.

## Tear down

```bash
docker compose down        # stop containers, keep data
docker compose down -v     # stop containers, also remove neocyclehub/-bootstrap volumes
```

(Recall idhub's data lives in `idhub/db.sqlite3` on the host either way —
delete it manually if you want a fully clean idhub too.)
