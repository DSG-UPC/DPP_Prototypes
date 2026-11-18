# NeoCycle UNTP v0.7.0 Demonstration — Step-by-Step Guide

## What this demonstrates

Four companies in a rare-earth-magnet recycling chain, each issuing signed
UNTP v0.7.0 verifiable credentials that cross-link into one provenance graph:

1. **RECYCLER** — Rare Earth Recycling Ltd. (`role: supplier`)
   Collects end-of-life WEEE, extracts NdFeB magnets, ships them to the
   Upcycler.

2. **UPCYCLER** — Rare Earth Upcycling Ltd. (`role: upcycler`)
   Separates the magnets into rare-earth catalysts via two parallel
   processes (SSC and ECEX), ships one output (the FeNd/ammonia alloy) on
   to the Manufacturer.

3. **MANUFACTURER** — GreenFert Innovations SAS (`role: manufacturer`)
   Blends the alloy into a nitrogen fertilizer — the final product, three
   companies downstream from the recycler.

4. **AUDITOR** — NeoCycle Auditor (`role: auditor`)
   Issues no credentials of its own. Reads everything the other three
   actors issued, and follows `relatedDocument` links across company
   boundaries to verify the chain.

Each of the first three actors issues, per UNTP v0.7.0:

| Type | Name | Cardinality |
|---|---|---|
| DIA | Digital Identity Anchor | one per actor |
| DFR | Digital Facility Record | one per actor |
| DCC | Digital Conformity Credential | one per actor's certification |
| DTE | Digital Traceability Event | one per process/shipment event |
| DPP | Digital Product Passport | one per product batch |

A DPP's `relatedDocument` points back at the DTE that produced it; a DTE's
`relatedDocument` points at the DPP(s) of the inputs it consumed — including
when that input DPP was issued by a *different* company. That's the chain
the Auditor verifies at the end.

## Prerequisites

- Docker + Docker Compose
- Python 3 with `pip install requests` (only needed for the setup script,
  run on your host, not inside a container)

## Step 1 — Start the stack

```bash
docker compose up --build
```

(Older Docker installs may need the standalone `docker-compose` binary
instead of the `docker compose` plugin — same arguments either way.)

This starts three services:

| Service | URL | Notes |
|---|---|---|
| `idhub` | http://localhost:8000 | signs all credentials |
| `neocyclehub` | http://localhost:9000 | plain UI, no login |
| `neocyclehub-bootstrap` | http://localhost:9001 | Bootstrap-styled UI, login required |

Both `neocyclehub` variants are independent instances of the same app —
separate databases, same fixture data. Everything below uses the plain
instance on `:9000`; the same steps work on `:9001` after logging in with
`admin` / `admin1234`.

Wait for `idhub` to report healthy (`docker compose ps` or the logs) before
continuing — neocyclehub's schema loading depends on it.

## Step 2 — One-time setup (recommended: automated)

A single script does everything Steps A–C used to require by hand: uploads
the 5 UNTP v0.7.0 schemas to idhub, creates a `did:web` identity and a
Bearer token in idhub for each of the three credential-issuing actors,
wires those into NeoCycleHub's Actor records, and then issues every
credential for every actor in the correct dependency order (so all
`relatedDocument` links, including the cross-company ones, resolve
immediately).

```bash
cd neocyclehub
pip install requests
python scripts/setup.py
```

The script finds the right containers itself (via Docker's own Compose
service labels), regardless of which compose file started them or whether it
pins `container_name:` — `docker-compose-remote.yml` deliberately doesn't,
to avoid name collisions on a shared host. Only set
`NEOCYCLEHUB_CONTAINER`/`IDHUB_CONTAINER` yourself if two deployments of this
stack are running on the same Docker host at once (discovery can't tell them
apart, and will tell you so instead of guessing).

The script is idempotent — safe to re-run after adding/editing fixture data.

For the Bootstrap variant on `:9001`, run `neocyclehub-bootstrap/scripts/setup.py`
instead — it targets the `neocyclehub-bootstrap` container and will create
its own DIDs/tokens (idhub identities are shared, tokens are not).

> When it finishes, skip to [Step 5](#step-5--verify-the-chain) — everything
> is already issued.

## Step 3 — One-time setup (alternative: manual, for a live walkthrough)

Do this instead of Step 2 if you want to demonstrate the mechanics by
clicking through the UI rather than running the script.

**Step A — Create three org DIDs in idhub**

Go to http://localhost:8000/admin/dids/new/, and for each of:

- Label: `supplier-rer` (Recycler)
- Label: `upcycler-reu` (Upcycler)
- Label: `manufacturer-greenfert` (Manufacturer)

set Type: `Web` and click Save. idhub auto-generates a `did:web:...` string
— copy each one for Step C. (The Auditor gets no DID/token — it never
signs anything.)

**Step B — Create three Bearer tokens**

Go to http://localhost:8000/webhook/tokens/, click "New token" for each:

- `neocyclehub-supplier`
- `neocyclehub-upcycler`
- `neocyclehub-greenfert`

Copy each token UUID (shown once, right after creation).

**Step C — Wire DIDs + tokens into NeoCycleHub**

Go to http://localhost:9000/admin/supply_chain/actor/ and, for each of the
three actors (Rare Earth Recycling Ltd., Rare Earth Upcycling Ltd.,
GreenFert Innovations SAS), set:

- `did` → the matching DID from Step A
- `idhub_token` → the matching token from Step B
- `idhub_url` → `http://idhub:8000/api/v1`

Leave "NeoCycle Auditor" (actor 3) untouched — no DID, no token.

**Step D — Upload the UNTP v0.7.0 schemas to idhub**

Go to http://localhost:8000/admin/schemas/upload, open the "Enable from
URL" tab, and for each of these 5 schema URLs, paste it into "Schema URL
reference" and `https://vocabulary.uncefact.org/untp/0.7.0/context/` into
"Context URL reference", then Save — idhub fetches and validates the schema
itself, and derives the stored filename from the URL's last path segment
(e.g. `DigitalProductPassport.json`):

- https://untp.unece.org/artefacts/schema/v0.7.0/dpp/DigitalProductPassport.json
- https://untp.unece.org/artefacts/schema/v0.7.0/dte/DigitalTraceabilityEvent.json
- https://untp.unece.org/artefacts/schema/v0.7.0/dcc/ConformityCredential.json
- https://untp.unece.org/artefacts/schema/v0.7.0/dfr/DigitalFacilityRecord.json
- https://untp.unece.org/artefacts/schema/v0.7.0/dia/DigitalIdentityAnchor.json

(This is exactly what `scripts/setup.py` automates in Step 2; if you did
Step 2, the schemas are already there. There's also an "Enable from File"
tab if you'd rather fetch the JSON yourself and paste/upload it — that one
doesn't take a context URL.)

## Step 4 — Issue credentials manually via the dashboard

> Skip this section if you ran the automated setup in Step 2 — everything
> is already issued and you can go straight to [Step 5](#step-5--verify-the-chain).

Open http://localhost:9000, click into each actor ("Manage credentials"),
and click the "Issue ..." button next to each row. The order below respects
every dependency (an event's DTE can only usefully link to inputs whose DPP
already exists, so issue DTEs before the DPPs of anything they consume as
*input*, and after the DPPs of anything they need to *reference*). If a
page shows a stale state after issuing, refresh the browser.

### RECYCLER — Rare Earth Recycling Ltd.

1. Issue DIA (Identity Anchor)
2. Issue DFR (Facility Record)
3. Issue DCC — R2:2013 Responsible Recycling Standard
4. Issue DTE — WEEE Collection, Extraction and Processing — SSC Path
5. Issue DPP — Crushed NdFeB Granulate for Solid-State Chlorination
6. Issue DTE — WEEE Collection, Extraction and Processing — ECEX Path
7. Issue DPP — Whole NdFeB Magnets for Electrochemical Extraction
8. Issue DTE — Shipment of Crushed NdFeB Granulate to Upcycler — SSC Path
9. Issue DTE — Shipment of Whole NdFeB Magnets to Upcycler — ECEX Path

### UPCYCLER — Rare Earth Upcycling Ltd.

1. Issue DIA
2. Issue DFR
3. Issue DCC — ISO 14001:2015 Environmental Management
4. Issue DTE — SSC Chlorination, Separation and Downstream Production
   (ModifyEvent, input = Crushed NdFeB Granulate — already has the
   Recycler's DPP from step 5 above, so this DTE's `relatedDocument`
   cross-links to it automatically)
5. Issue DPP — FeNd Bimetallic Catalyst for Ammonia Synthesis (SSC Path)
6. Issue DPP — Nd Polymerization Catalyst (SSC Path)
7. Issue DPP — Nd API Synthesis Catalyst (SSC Path)
8. Issue DPP — Nd Suzuki Reaction Catalyst (SSC Path)
9. Issue DPP — FeNd Alloy for Ammonia Fertilizer Production (SSC Path)
10. Issue DTE — ECEX Electrochemical Extraction, Separation and Downstream
    Production (cross-links to the Recycler's Whole NdFeB Magnets DPP
    from step 7 above)
11. Issue DPP — FeNd Bimetallic Catalyst for Ammonia Synthesis (ECEX Path)
12. Issue DPP — Nd Polymerization Catalyst (ECEX Path)
13. Issue DPP — Nd API Synthesis Catalyst (ECEX Path)
14. Issue DPP — Nd Suzuki Reaction Catalyst (ECEX Path)
15. Issue DTE — Shipment of FeNd/Ammonia Alloy to GreenFert — SSC Path
    (input = the DPP from step 9 — must come after it)

> Steps 5–9 can be in any order among themselves, but only after step 4.
> Steps 11–14 can be in any order among themselves, but only after step 10.
> Step 15 must come after step 9.

### MANUFACTURER — GreenFert Innovations SAS

1. Issue DIA
2. Issue DFR
3. Issue DCC — Regulation (EU) 2019/1009 Fertilising Products Regulation
4. Issue DTE — Fertilizer Blending Process — Green Fertilizer Production
   (input = the FeNd/ammonia alloy — already has the Upcycler's DPP from
   step 9 above, so this DTE cross-links to it, one company hop removed
   from the original Recycler DPP)
5. Issue DPP — Green Fertilizer (Nitrogen, NH3-derived)

### AUDITOR — NeoCycle Auditor

Nothing to issue — this actor has no `idhub_token` by design. Open its
"Manage credentials" page instead of an issue form: it shows a "Credential
Chain Analysis" table listing every DPP issued by any actor.

## Step 5 — Verify the chain

From the Auditor's page (or any actor's credential list), click "View
chain" on the Green Fertilizer DPP. This walks its `relatedDocument` links
recursively:

```
Green Fertilizer DPP (GreenFert)
  -> Fertilizer Blending DTE (GreenFert)
       -> FeNd Alloy DPP (Upcycler)
            -> SSC Chlorination DTE (Upcycler)
                 -> Crushed NdFeB Granulate DPP (Recycler)
                      -> WEEE Collection DTE (Recycler)
```

Each node is verified live against idhub's `/verify/` endpoint and shown as
valid/invalid/unavailable. This is the concrete proof that credentials
issued independently by three different companies, each with their own
`did:web` identity and signing key, form one continuous, cryptographically
checkable provenance graph.

Every credential is also servable directly as JSON-LD at a stable URL:

```
http://localhost:9000/credentials/<uuid>/
```

## Troubleshooting

- **`scripts/setup.py` fails with `idhub API error [401]: Unauthorized`** —
  the tokens NeoCycleHub has for its actors don't match any token in idhub
  (common after recreating containers under a different Docker Compose
  project name, which gives neocyclehub a fresh database while idhub's
  bind-mounted SQLite file is reused, or vice versa). Re-run
  `python scripts/setup.py` — it creates fresh tokens and rewires the
  actors.
- **Schema upload reports "Already exists" for all 5 on a re-run** —
  expected, not an error.
- **"Issue" buttons fail with a connection error** — idhub isn't up yet, or
  the actor's `idhub_token`/`idhub_url` is wrong — check Actor records in
  http://localhost:9000/admin/supply_chain/actor/.
- **A DTE's `relatedDocument` is missing an expected cross-company link** —
  its input product's DPP wasn't issued yet at the time — issue that DPP
  first. The DTE re-issue isn't automatic (the manual UI issues once); re-run
  `scripts/setup.py`'s `issue_all` management command to backfill safely, it
  skips anything already issued.
- **Stale-looking page after issuing a credential** — refresh the browser.
- **To reset NeoCycleHub only** — `docker compose down -v` drops the
  `neocyclehub` and `neocyclehub-bootstrap` DB volumes (named volumes), then
  re-run from Step 1. This re-loads the fixtures but keeps idhub's existing
  DIDs/tokens/schemas, so you'll need to re-wire Actor records to them (or
  just re-run `scripts/setup.py`, which reuses existing DIDs by label and
  creates fresh tokens).
- **To reset idhub too (fresh DIDs/schemas)** — idhub's database is a bind
  mount at `idhub/db.sqlite3` on the host, not a Docker volume, so `-v`
  doesn't touch it — stop the stack and delete that file yourself, then
  restart.

**Admin logins:** idhub (`admin@neocycle.local` / `admin1234`) and
NeoCycleHub (`admin` / `admin1234`). idhub has no plain `/admin/` index —
it's a custom admin UI, not Django's built-in admin — start instead at
http://localhost:8000/admin/dashboard/, or go directly to
http://localhost:8000/admin/schemas/, http://localhost:8000/admin/wallet/identities/,
or http://localhost:8000/webhook/tokens/. NeoCycleHub's admin *is* Django's
built-in admin, at http://localhost:9000/admin/ (or `:9001`).
