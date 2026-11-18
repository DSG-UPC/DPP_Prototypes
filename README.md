# DPP Prototypes

Research prototypes of Digital Product Passport (DPP) systems from the Distributed
Systems Group (DSG) at Universitat Politècnica de Catalunya (UPC). The prototypes
issue [UNTP](https://untp.unece.org/) (UN Transparency Protocol) verifiable
credentials, signed under `did:web` identities, along a supply chain and
cross-link them into a verifiable provenance graph.

## Prototypes

### `NEO-CYCLE/`

Prototypes for a rare-earth magnet recycling and upcycling value chain, built in
the context of the NEO-CYCLE project. Both implement UNTP v0.7.0.

| Folder | What it is |
|---|---|
| [`Proto_v070_C_eu-reg_JWT_Env_Decoded`](NEO-CYCLE/Proto_v070_C_eu-reg_JWT_Env_Decoded) | A four-actor demonstrator (supplier, upcycler, manufacturer, auditor). Issues all five UNTP credential types as JOSE-enveloped `vc+jwt` credentials, registers each DPP in a mock EU DPP registry, and shows the decoded JWT header and payload of every signed credential. No blockchain. |
| [`Proto_v070_H2_QBFT_Anchoring`](NEO-CYCLE/Proto_v070_H2_QBFT_Anchoring) | A ten-actor consortium scenario (partners other than UPC pseudonymized by role) in which every DPP is also anchored on a four-validator Hyperledger Besu network running QBFT. Includes the experiment harness and raw results of an evaluation of the anchoring: overhead, throughput, fault behaviour and threat tests. |

Each folder is a self-contained Docker Compose stack with its own `README.md` and
`HOWTO.md`. They share host ports (8000, 9000, 9001, 9100), so run only one at a time.

## Requirements

Docker with Docker Compose, Python 3 with `requests` for the one-time setup script,
and internet access during setup (the UNTP schemas are downloaded from
`untp.unece.org`). See the README of each prototype for the exact steps.

All passwords, secret keys and blockchain keys in this repository are demo values
for local test deployments. Do not reuse them in a real deployment.

## Licence

Each prototype is licensed under the GNU Affero General Public License v3.0; see
the `LICENSE` file in its folder. The prototypes include modified copies of
[idhub](https://gitlab.com/dsg-upc/idhub) and pyvckit, both AGPL-3.0.
