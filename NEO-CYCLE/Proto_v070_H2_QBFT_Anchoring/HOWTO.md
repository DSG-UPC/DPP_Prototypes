# How to run the NeoCycle UNTP v0.7.0 demonstrator

This guide walks a third party through running this project from scratch:
starting the stack, provisioning it, and exploring/verifying the result. It
assumes no prior context beyond having this folder on your machine.

## What you're running

A demonstrator for issuing signed [UNTP](https://untp.unece.org/) (UN
Transparency Protocol) v0.7.0 verifiable credentials across a ten-actor
rare-earth-magnet recycling supply chain modeled on the NEO-CYCLE
consortium. Partners other than UPC are pseudonymized by role: suppliers
**S1** and **S2** source spent magnets; upcyclers **U1** and **U2** process
them (SSC chlorination / SENE electrochemical leaching); **U3** separates
the Nd/Fe/B streams; **UPC**, **U4** and **U5** upcycle them; manufacturers
**M1** and **M2** are the terminal actors; plus an **Auditor**. Each company issues its own signed credentials
(Digital Product Passports, Traceability Events, Conformity Credentials,
Facility Records, Identity Anchors) from its own `did:web` identity, and the
credentials cross-link into one provenance graph via `relatedDocument`, even
across company boundaries.

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
- Ports 8000, 8546, 9000, 9001, 9100 and 9200 free on your machine.
- Outbound internet access from wherever `docker compose` runs — the setup
  script has idhub download the real UNTP v0.7.0 schemas from
  `untp.unece.org` at provisioning time.

## Step 1 — Start the stack

From this folder (the one containing `docker-compose.yml`):

```bash
docker compose up --build -d
```

This builds and starts three user-facing containers, plus `eu-registry` (the
mock EU DPP registry, see [`EU_REGISTRY.md`](EU_REGISTRY.md)) and a
supporting 4-node blockchain network in the background:

| Container | URL | Notes |
|---|---|---|
| `idhub` | http://localhost:8000 | signs all credentials |
| `neocyclehub` | http://localhost:9000 | plain UI, no login |
| `neocyclehub-bootstrap` | http://localhost:9001 | Bootstrap-styled UI, login required |
| `besu-supplier` | `besu-supplier:8545` internally, also http://localhost:8546 from the host | one of 4 Besu QBFT validators (one per issuing role, plus UPC's own node); this one's RPC is published for convenient direct access (curl/Postman/MetaMask/Blockscout), see [`CHAIN_ANCHOR.md`](CHAIN_ANCHOR.md) |
| `besu-upcycler` / `besu-manufacturer` / `besu-upc` | internal only, `besu-<name>:8545` | the other 3 validators — same shared ledger as `besu-supplier`, just not published to the host |
| `chain-deploy` | — | one-shot: deploys the anchoring contract once all 4 validators are healthy (or reuses an existing deployment), then exits |

The first build takes several minutes (idhub's image installs a large
dependency list). Wait for idhub to report healthy before continuing:

```bash
docker compose ps
```

`idhub` should show `(healthy)`. `neocyclehub`/`neocyclehub-bootstrap` wait
for that automatically before starting, and also wait for `chain-deploy` to
exit successfully (`Exited (0)`) — if it's still `Exited (1)` or restarting,
see [Troubleshooting](#troubleshooting).

## Step 2 — One-time setup

A script provisions everything: uploads the 5 UNTP v0.7.0 schemas to idhub
(fetched live from untp.unece.org), creates a `did:web` identity and a
Bearer token in idhub for each of the ten credential-issuing companies
(the Auditor issues nothing, so it's excluded), wires those into
NeoCycleHub's records, and issues every credential for every company in the
correct dependency order (so all `relatedDocument` links — including the
cross-company ones — resolve immediately).

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
credentials. On the Auditor's page, click "View chain" on either terminal
actor's DPP (M2's validated API batch, or M1's Fe-Nd ammonia synthesis
catalyst) to walk its `relatedDocument` links back through every hop —
e.g. M1 → U5/U4 → U3 → U1/U2 → S1/S2 — with each
credential verified live against idhub.

Every credential is also servable directly as JSON-LD:

```
http://localhost:9000/credentials/<uuid>/
```

That raw JSON is a JOSE envelope — the actual signed content is a base64url
JWT inside it, unreadable at a glance. Click "Decoded" next to any
credential (or go to `/credentials/<uuid>/decoded/`) to see the JOSE
header and JWT payload pretty-printed instead — a local decode, not a
signature check (that's what "Chain" does, live against idhub).

Before issuing a DPP/DTE/DCC, its source record is editable — click "Edit"
next to the "Issue" button on the actor page to change any field the
credential will carry (product/event/claim details, GS1 `idScheme`,
facility names, conformity scores, etc.) before signing it. Editing after
issuance doesn't retroactively change what's already signed — it only
affects the next credential issued from that record.

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

## Optional — independently verify a DPP's on-chain anchor

Every DPP is anchored on the 4-node Besu network right after issuance,
signed by the issuing actor's own chain account and submitted via that
actor's own validator node (see [`CHAIN_ANCHOR.md`](CHAIN_ANCHOR.md)).
Confirm one landed, and that the on-chain hash matches the credential:

```bash
docker exec neocyclehub python manage.py shell -c "
import json
from web3 import Web3
from supply_chain.models import IssuedCredential
c = IssuedCredential.objects.filter(cred_type='DPP').exclude(chain_tx_hash='').first()
print('tx:', c.chain_tx_hash, 'block:', c.chain_block_number)
print('recomputed hash:', Web3.keccak(text=json.dumps(c.credential, sort_keys=True)).hex())
"
```

The meaningful check is reading the event back from a **different** node
than the one the actor submitted through — this is what actually
distinguishes "distributed ledger" from "a database with extra steps":

```bash
# neocyclehub's image has curl installed and can reach every besu-* service
# by name; the besu image itself has neither curl nor wget.
docker exec neocyclehub curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["<tx hash from above>"],"id":1}' \
  http://besu-supplier:8545
docker exec neocyclehub curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["<tx hash from above>"],"id":1}' \
  http://besu-upcycler:8545
```

confirm both calls return the **same** block hash/number for that
transaction, queried from two different nodes — proving the block was
agreed by consensus, not just recorded locally by whichever node received
the call.

`besu-supplier`'s RPC is also published at `http://localhost:8546`, so the
same call works directly from the host without `docker exec` at all:

```bash
curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["<tx hash from above>"],"id":1}' \
  http://localhost:8546
```

For repeated poking-around, raw JSON-RPC gets tedious fast — a proper block
explorer is much nicer. [Blockscout](https://www.blockscout.com/) is
open-source, self-hostable, and Besu documents it directly as a supported
explorer for private/permissioned networks; pointed at
`http://localhost:8546` (or `http://besu-supplier:8545` if run as its own
compose service) it gives a browsable UI over every anchored DPP's
transaction and the `Anchored` event log, instead of hand-decoding JSON.
Not wired into this repo — running it is a manual step if you want it
(typically a Postgres + Blockscout container pair, configured with this
network's RPC URL and chain ID `4004`).

## Optional — see the fault-tolerance property directly

```bash
docker stop besu-manufacturer
docker exec neocyclehub curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' http://besu-supplier:8545
# wait a few seconds, run the same command again — block height keeps advancing
# with only 3 of 4 validators online (QBFT needs 3 of 4 for a quorum)
docker start besu-manufacturer
# after ~15s it resyncs and catches back up to the same height
```

## Optional — try the GTIN resolver and tiered credential views

Grab a real `gtin` value from the DB first (any `ProductBatch`),
then:

```bash
# default: redirects to the DPP
curl -sI "http://localhost:9100/01/<the numeric part of a gtin>" | head -1

# machine-readable: an IETF Linkset listing the full DPP and the public-summary view
curl -s -H "Accept: application/linkset+json" "http://localhost:9100/01/<gtin>" | python3 -m json.tool
```

Then compare the three views of the same credential directly:

```bash
docker exec neocyclehub python manage.py shell -c "
from supply_chain.models import IssuedCredential
c = IssuedCredential.objects.filter(cred_type='DPP').first()
print(c.uuid)
"
curl -s http://localhost:9000/credentials/<uuid>/public/ | python3 -m json.tool   # reduced fields, no auth
curl -s http://localhost:9000/credentials/<uuid>/         | python3 -m json.tool   # full content (neocyclehub, unchanged)
curl -s http://localhost:9001/credentials/<uuid>/partner/                          # bootstrap: redirects to login (no session)
```

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
- **`besu-supplier`/`besu-upcycler`/`besu-manufacturer` restart repeatedly or
  never go healthy** — check `docker compose logs besu-supplier` first for
  `Illegal static enode supplied` (a `static-nodes.json` entry doesn't match
  the fixed IPs in `docker-compose.yml`'s `networks.default.ipam` block —
  Besu's static-peer parser rejects DNS hostnames, only literal IPs work)
  or `Failed to create data directory` (the named `besu-*-data` volume
  didn't get created/mounted correctly).
- **`chain-deploy` container shows `Exited (1)`** — it couldn't reach
  `besu-supplier`'s RPC (check all 4 Besu nodes are healthy first — QBFT
  needs a live network to include the deployment transaction in a block)
  or the Solidity compile failed. Check its logs
  (`docker compose logs chain-deploy`) and retry with
  `docker compose up --build chain-deploy`; `neocyclehub`/`neocyclehub-bootstrap`
  won't start until it exits `0`.
- **DPPs issue fine but never get a "⛓ anchored" badge / `chain_tx_hash`
  stays empty** — chain anchoring is best-effort and only logs on failure
  (same convention as `eu-registry`, see
  [`CHAIN_ANCHOR.md`](CHAIN_ANCHOR.md#failure-mode-best-effort-non-blocking)).
  Check `docker compose logs neocyclehub | grep -i chain` for the warning —
  common causes are that actor's own Besu node (`chain_client.NODE_BY_ROLE`)
  not yet healthy, or all 4 Besu data volumes having been wiped (fresh
  chain) without also clearing that actor's `chain_address`/
  `chain_private_key` in `neocyclehub`'s DB.
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
