# NeoCycle UNTP v0.7.0 Prototype

A demonstrator for issuing signed [UNTP](https://untp.unece.org/) (UN Transparency Protocol) v0.7.0 verifiable credentials across a four-actor rare-earth-magnet recycling supply chain: a **Recycler/Supplier** (collection/extraction), an **Upcycler** (chemical processing), a **Manufacturer** (fertilizer blending), and an **Auditor** (issues nothing, verifies the chain), with credentials cross-linked between actors — including across company boundaries — via `relatedDocument`.

## Structure

| Folder | What it is |
|---|---|
| `idhub/` | [idhub](https://gitlab.com/dsg-upc/idhub) — third-party DID/VC signing service, extended here with UNTP v0.7.0 support (`DigitalConformityCredential` / `DigitalIdentityAnchor` endpoints, `issuerAlsoKnownAs`, fixed context URL). Signs the UNTP credential types as JOSE-enveloped JWTs (EdDSA, per UNTP's VC-JOSE-COSE requirement) rather than `pyvckit`'s Data Integrity proof, and serves credential status (revocation, W3C Bitstring Status List) and `did:web` identities. |
| `neocyclehub/` | The demo app: a Django project (`supply_chain`) modeling `Actor`, `ProductBatch`, `ProcessingEvent`, `ConformityClaim`, and `IssuedCredential`, with builders that turn those into UNTP v0.7.0 credential payloads, an issuer service that calls idhub's API to sign them, and a dashboard UI to trigger issuance and browse results. |
| `neocyclehub-bootstrap/` | Same app as `neocyclehub`, restyled with Bootstrap + login-gated views. Runs as a second, independent instance on a different port. |
| `eu-registry/` | Mock of the EU's centralized DPP Registry — indexes each issued DPP's metadata (gtin, issuer, validity, a pointer back to the credential) right after it's signed, demonstrating ESPR's mandatory registration step. See [`EU_REGISTRY.md`](EU_REGISTRY.md) for details. |
| `devicehub-django/` | [devicehub-django](https://github.com/eReuse/devicehub-django) — eReuse's IT asset management platform, kept here as a reference. It was evaluated as a base for this prototype but not used: it models individual devices, not material batches, so `neocyclehub` was built fresh instead (see `what_was_done_and_instructions.txt`). |
| `suppliers/`, `upcyclers/`, `manufacturers/` | Sample/reference UNTP v0.7.0 credential JSON files (one per credential type: DPP, DTE, DCC, DFR, DIA — for the Recycler/Supplier, Upcycler, and Manufacturer/GreenFert actors respectively) used as the structural basis for the credential builders. No equivalent folder for the Auditor — it issues no credentials. |


## Credential types

All five UNTP v0.7.0 credential types are issued:

- **DPP** — Digital Product Passport (per product batch)
- **DTE** — Digital Traceability Event (collection, extraction, transport, transformation)
- **DCC** — Digital Conformity Credential (third-party certifications, e.g. R2)
- **DFR** — Digital Facility Record (per actor's facility)
- **DIA** — Digital Identity Anchor (actor's registered legal identity)

## Running it

```
docker compose up --build
```

(Older Docker installs may need the standalone `docker-compose` binary instead of the `docker compose` plugin — same arguments either way.)

This starts `idhub` (port 8000), `neocyclehub` (port 9000), and `neocyclehub-bootstrap` (port 9001). Wait for `idhub` to report healthy (`docker compose ps`) before continuing — schema loading depends on it.

Note: use only `idhub` (port 8000) and `neocyclehub-bootstrap` (port 9001) to run the demonstration.

Then run the one-time setup script, which uploads the UNTP schemas to idhub, creates `did:web` identities and API tokens for each credential-issuing actor, and wires them into the NeoCycleHub fixtures:

```
cd neocyclehub-bootstrap
pip install requests
python scripts/setup.py
```

(`neocyclehub` on port 9000 is an independent instance with its own database — if you also want to use it, `cd neocyclehub` and run its own copy of `scripts/setup.py` too; idhub's `did:web` identities are shared between both, but tokens are not.)

Open `http://localhost:9001` (login `admin` / `admin1234`) — or `:9000` for the non-Bootstrap variant — and use the dashboard to issue credentials for the pre-loaded actors. Signed credentials are served as JSON-LD at `/credentials/<uuid>/`, making `relatedDocument` links between actors — including across company boundaries — resolvable.

Admin logins and pass idhub: `admin@neocycle.local` / `admin1234`.
Admin logins and pass NeoCycleHub: `admin` / `admin1234`.
