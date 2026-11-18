# UNTP Digital Product Passports with permissioned QBFT anchoring

A demonstrator that issues signed [UNTP](https://untp.unece.org/) (UN Transparency
Protocol) v0.7.0 verifiable credentials across a ten-actor rare-earth-magnet
recycling and upcycling supply chain, modeled on the NEO-CYCLE consortium, and
anchors every issued Digital Product Passport on a four-validator Hyperledger Besu
network running QBFT. It is the artifact of the paper "Evaluating Permissioned QBFT
Anchoring for UNTP Digital Product Passports in a Rare-Earth Upcycling Consortium";
the experiment harness and all raw results are in [`experiments/`](experiments/README.md).

## Actors

Partners other than UPC are pseudonymized by role (S = supplier, U = upcycler,
M = manufacturer):

| Actor | Role | Function |
|---|---|---|
| Supplier S1, Supplier S2 | supplier | source and demagnetise spent NdFeB magnets |
| Upcycler U1 | upcycler | solid-state chlorination (SSC) |
| Upcycler U2 | upcycler | electrochemical leaching (SENE) |
| Upcycler U3 | upcycler | Nd/Fe/B separation |
| UPC | upcycler | boron concentration to H3BO3 |
| Upcycler U4 | upcycler | Nd catalyst and API chemistry |
| Upcycler U5 | upcycler | Fe nanoparticle production |
| Manufacturer M1 | manufacturer | Fe-Nd ammonia catalyst |
| Manufacturer M2 | manufacturer | API validation |
| NeoCycle Auditor | auditor | issues nothing, verifies the chain |

Each issuing actor signs its own credentials under its own `did:web` identity, and
the credentials cross-link into one provenance graph via `relatedDocument`. The
fixture data (products, events, batch masses) is illustrative.

## Components

| Folder / service | What it is |
|---|---|
| `idhub/` (port 8000) | DID/VC signing service, based on [idhub](https://gitlab.com/dsg-upc/idhub) (AGPL-3.0) and extended with UNTP v0.7.0 support. Signs credentials as JOSE-enveloped `vc+jwt` (EdDSA), serves `did:web` documents and W3C Bitstring Status Lists. `idhub/vendor/pyvckit` is a vendored copy of pyvckit (AGPL-3.0). |
| `neocyclehub/` (port 9000) | The operator application (Django): actors, product batches, processing events, conformity claims; builds the UNTP payloads, has idhub sign them, registers and anchors DPPs, and walks/verifies the credential graph. No login. |
| `neocyclehub-bootstrap/` (port 9001) | The same application with a Bootstrap UI and login. Independent instance with its own database. Used for the demonstration and the experiments. |
| `eu-registry/` (port 9100) | Mock EU DPP registry: indexes each issued DPP's metadata, plus a minimal GS1 Digital Link resolver. See [`EU_REGISTRY.md`](EU_REGISTRY.md). |
| `metrics-dashboard/` (port 9200) | Collects issuance and verification timing samples from both hub instances. |
| `blockchain/` | Besu configuration for the four validators (`besu-supplier`, `besu-upcycler`, `besu-manufacturer`, `besu-upc`) and the `DPPAnchor` contract with its deployment script. See [`CHAIN_ANCHOR.md`](CHAIN_ANCHOR.md). |
| `experiments/` | Harness and results of the evaluation. See [`experiments/README.md`](experiments/README.md). |

All five UNTP v0.7.0 credential types are issued: Digital Product Passport (DPP),
Digital Traceability Event (DTE), Digital Conformity Credential (DCC), Digital
Facility Record (DFR) and Digital Identity Anchor (DIA).

## Running it

Requirements: Docker with Docker Compose, Python 3 with `requests` on the host,
ports 8000, 8546, 9000, 9001, 9100 and 9200 free, and internet access (idhub
downloads the UNTP schemas during setup).

```
docker compose up --build -d
```

Wait until `idhub` is healthy (`docker compose ps`) and `chain-deploy` shows
`Exited (0)`. Then run the one-time setup, which uploads the UNTP schemas to idhub
and creates a `did:web` identity and API token for each issuing actor:

```
cd neocyclehub-bootstrap
pip install requests
python scripts/setup.py
```

Open http://localhost:9001 (login `admin` / `admin1234`) and issue credentials
from the dashboard. Signed credentials are served at `/credentials/<uuid>/`. The
idhub admin is `admin@neocycle.local` / `admin1234`. All passwords, secret keys and
Besu validator keys in this repository are demo values for a local test network;
do not reuse them in any real deployment.

The `neocyclehub` instance on port 9000 needs its own setup run
(`cd neocyclehub && python scripts/setup.py`) if you want to use it as well.

## Documentation

- [`HOWTO.md`](HOWTO.md): step-by-step walkthrough, verification recipes, troubleshooting.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): components, data model, signing, use cases.
- [`CHAIN_ANCHOR.md`](CHAIN_ANCHOR.md): the Besu anchoring layer and the anchor checks.
- [`EU_REGISTRY.md`](EU_REGISTRY.md): the mock registry and resolver.
- [`experiments/README.md`](experiments/README.md): the evaluation.
