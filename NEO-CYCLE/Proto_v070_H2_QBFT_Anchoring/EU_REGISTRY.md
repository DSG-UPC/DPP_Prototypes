# EU Registry — mock DPP registry service

`eu-registry/` is a small Django service that mocks the EU's centralized
Digital Product Passport Registry (see
[single-market-economy.ec.europa.eu — DPP Registry](https://single-market-economy.ec.europa.eu/single-market/digital-product-passport/dpp-registry_en)).
It exists to demonstrate ESPR's mandatory registration step: *"Before a
product can be placed on the EU market, the relevant economic operator must
register its DPP in the Registry."*

It is not part of the credential trust chain — it never signs, verifies, or
stores DPP content. It only stores the index/metadata needed to find a DPP,
mirroring the real registry's decentralized-content / centralized-index
split: the actual credential stays hosted by the economic operator
(`neocyclehub`), the registry just points at it.

## Where it sits

```
Supplier / Upcycler / Manufacturer (neocyclehub)
        │  1. issue_credential() → idhub signs the DPP
        ▼
   idhub (signs + serves the DPP as JSON-LD at /credentials/<uuid>/)
        │  2. register_dpp() → POST index metadata only
        ▼
   eu-registry (stores gtin, issuer, validity, dpp_url — never the DPP itself)
```

`register_dpp()` runs immediately after a DPP is signed — see
`supply_chain/registry_client.py` in both `neocyclehub` and
`neocyclehub-bootstrap` (identical in both), called from
`supply_chain/views.py` (manual "Issue" button flow) and
`supply_chain/management/commands/issue_all.py::_issue_dpp` (bulk/setup-script
flow). It only runs for **DPP** credentials — DIA/DFR/DCC/DTE never touch the
registry, since only products (not events, facilities, or identities) get
placed on the market under ESPR.

## Failure mode: best-effort, non-blocking

`register_dpp()` catches `requests.RequestException` and only logs a
warning — it never raises. A registry outage does not block DPP issuance.
This is a deliberate simplification for the demo (the real ESPR obligation
is that a product *can't be placed on the market* without registration,
i.e. registration should arguably be blocking) — see
[Known limitations](#known-limitations).

## Data model

`eu-registry/registry/models.py::RegistryEntry` — one row per registered
DPP:

| Field | Source | Notes |
|---|---|---|
| `gtin` | `product.gtin` | GS1 GTIN or equivalent product identifier URL |
| `product_name` | `product.name` | |
| `issuer_did` | `actor.did` | the economic operator's `did:web` |
| `issuer_name` | `actor.name` | |
| `valid_from` / `valid_until` | decoded from the signed credential's JWT (`vc.validFrom` / `vc.validUntil`) | see [decoding note](#decoding-the-jose-envelope) below |
| `dpp_url` | `{public_url}/credentials/{uuid}/` | pointer back to neocyclehub — the registry never stores the DPP content itself |
| `registered_at` | `auto_now_add` | when the registry received it, not when the DPP was issued |

No foreign key back to `neocyclehub` — the two services don't share a
database, `dpp_url` is the only link between them, exactly as in the real
registry's design.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/register/` | `POST` | Called by `neocyclehub`/`neocyclehub-bootstrap` right after a DPP is signed. Body: JSON with the fields above (`gtin`, `product_name`, `issuer_did`, `issuer_name`, `dpp_url` required; `valid_from`/`valid_until` optional). Returns `201` + `{"registered": true, "id": <pk>}`, or `400` listing missing fields. |
| `/` | `GET` | Human search UI — `?q=` matches against `gtin`, `product_name`, or `issuer_name` (case-insensitive substring). Shows validity window, registration timestamp, and a link to the full DPP at its `dpp_url`. |
| `/01/<gtin>` | `GET` | Minimal **GS1 Digital Link resolver** — machine/QR-code-facing discovery, distinct from the human search UI above. Default response: `302` redirect to the matching entry's `dpp_url`. With `Accept: application/linkset+json` or `?linkType=all`: an IETF Linkset (RFC 9264) JSON response listing the full DPP and the public-summary view as `pip` link relations. `404` if no `RegistryEntry` matches. No authentication — a resolver is discovery, meant to be public. |

`/api/register/` is `@csrf_exempt` (it's a service-to-service call, not a
browser form post) and has no authentication — anyone reaching port 9100
can write fake entries. Fine for a local demo; see
[Known limitations](#known-limitations) if reusing this pattern elsewhere.

## Decoding the JOSE envelope

`registry_client.py::_decode_envelope_vc()` locally base64url-decodes the
signed credential's JWT payload to pull out `validFrom`/`validUntil` —
**no signature check**. That's intentional: the registry only needs those
two dates for its index, not proof of authenticity. Signature verification
already happens elsewhere (idhub's `/verify/` endpoint, and the chain-walk
UI) — duplicating it here would just be wasted work for a value the
registry doesn't otherwise use trust-wise.

## Running / wiring

Part of the standard `docker compose up --build` (see `README.md` /
`HOWTO.md`) — no separate setup step. Compose specifics:

- Service `eu-registry`, port **9100**, health-checked via a plain `GET /`.
- `neocyclehub` and `neocyclehub-bootstrap` both `depends_on: eu-registry`
  (`condition: service_healthy`) and get `EU_REGISTRY_URL=http://eu-registry:9100`
  injected as an env var — that's the only config `registry_client.py`
  needs (`settings.EU_REGISTRY_URL`).
- DB is a **named volume** (`eu-registry-db`), separate from both
  neocyclehub instances' volumes — survives `docker compose down` but not
  `docker compose down -v`.

### Migration

`eu-registry/registry/migrations/0001_initial.py` creates the
`RegistryEntry` table. The compose command only runs `migrate`, not
`makemigrations`, so this file has to exist and be committed — it isn't
generated on the fly at container start. If the `RegistryEntry` model is
ever changed, regenerate it explicitly:

```bash
docker run --rm -v "$(pwd)/eu-registry:/app" eu-registry-image \
    python manage.py makemigrations registry
```

(then fix ownership on the generated file — the container runs as root, so
`makemigrations` leaves it `root`-owned on the host.)

## Verifying it's actually working

Registration is silent on success (only logged on failure), so the
straightforward way to confirm it's wired up:

```bash
docker exec eu-registry python manage.py shell -c "
from registry.models import RegistryEntry
print(RegistryEntry.objects.count())
"
```

or open `http://localhost:9100` and search for a product/company name
you've issued a DPP for. Note: **already-issued** DPPs from a prior
session's DB volume won't retroactively appear — `issue_all` skips
credentials it's already issued (by design, see its docstring), so
`register_dpp()` only fires for DPPs issued *after* `eu-registry` existed.
To backfill, delete the relevant `IssuedCredential` row and re-run
`manage.py issue_all` (or re-issue via the dashboard) — this issues a
fresh credential and registers it.

## Known limitations

- **Non-blocking is a simplification, not the real ESPR rule.** The actual
  regulation makes registration a precondition for placing a product on
  the market; this demo treats it as fire-and-forget so a registry outage
  can't break the credential-issuance demo. If demonstrating ESPR
  compliance specifically (not just the credential chain), consider making
  this blocking, or at minimum surfacing failures in the UI instead of
  only logging them.
- **No auth on `/api/register/`.** Anyone who can reach port 9100 can
  create arbitrary entries. Not exposed outside the Docker network by
  default, but don't reuse this pattern past a local demo.
- **No retroactive backfill.** As noted above, DPPs issued before
  `eu-registry` was wired in (or issued while it was down) never get
  registered unless manually re-issued.
- **`valid_from`/`valid_until` are opportunistic, not guaranteed.** If the
  JWT payload can't be decoded (unexpected shape, `IndexError`/`ValueError`/
  `JSONDecodeError`), `_decode_envelope_vc()` silently returns `{}` and
  both fields are stored empty — registration still succeeds, just without
  those two fields.
- **`/01/<gtin>` is a minimal resolver, not a full GS1 Digital Link
  implementation.** No browsable multi-link HTML page (default response is
  a plain redirect), no `linkType` filtering beyond `all`, no other GS1
  link relations (`pdp`, `pgi`, etc.) — just enough to demonstrate
  discovery. No auth here either, but that's by design for a resolver, not
  a gap.
