# Chain Anchor — distributed ledger tamper-evidence PoC

`blockchain/` builds and deploys one small contract, `DPPAnchor`, to a
**4-node Hyperledger Besu network running QBFT consensus**: one validator
per issuing *role* (supplier, upcycler, manufacturer — nine actors
share these three role nodes: S1/S2 on supplier;
U1/U2/U3/U4/U5 on upcycler; M1/M2 on manufacturer), plus a fourth,
`besu-upc`, UPC's own dedicated node — a per-company split off a shared
role node, which brings the network to the `N≥3f+1`
Byzantine-fault-tolerance threshold for `f=1` (see Known limitations
below). It anchors a hash of
every issued Digital Product Passport on-chain, as tamper-evidence for
[`eu-registry`](EU_REGISTRY.md)'s off-chain index: if `eu-registry`'s DB row
for a DPP is ever altered, recomputing the hash and comparing it against the
on-chain `Anchored` event reveals the mismatch, without needing to trust
`eu-registry` itself.

This is a real multi-node network, not a single-process development
chain: real p2p
gossip (verified via `admin_peers`), real multi-party block agreement
(verified by reading identical contract state back from nodes that never
received the deploy call), and no single process that alone determines
chain state.

It is a separate concern from `eu-registry`. `eu-registry` mocks the EU
regulator's centralized index; the chain instead represents the trust layer
*among the supply-chain participants themselves* — so, unlike
`register_dpp()`, the write comes from `neocyclehub`/`neocyclehub-bootstrap`
directly, not from a shared registry service. This also better reflects a
"correct" deployment where each of the ten actors (S1, S2, U1,
U2, U3, UPC, U4, U5, M1, M2) runs its own `neocyclehub` instance: today's single shared instance is itself a demo
simplification, so giving each `Actor` its own on-chain signing identity —
the same way each already has its own `did:web` — keeps that property even
though the Django processes are still shared. With 4 Besu validators —
three shared per role, plus UPC's own — each actor also submits its
anchor transaction through its *own* or its role's node, not a single
shared entrypoint — see [Per-actor identity](#per-actor-identity-and-per-actor-node)
below.

## Where it sits

```
S1 / S2 (supplier)               ──anchor_dpp()──▶ besu-supplier      ─┐
U1 / U2 / U3 / U4 / U5 (upcycler) ──anchor_dpp()──▶ besu-upcycler       ├─▶ QBFT consensus (all 4 validators)
M1 / M2 (manufacturer)               ──anchor_dpp()──▶ besu-manufacturer  ─┤
UPC (its own dedicated node)              ──anchor_dpp()──▶ besu-upc          ─┘        │
                                                                                          ▼
                                          Anchored(dppHash, actor, dppUrl, timestamp)
                                          — identical on every node, not just the one that received the tx
```

`anchor_dpp()` runs immediately after `register_dpp()`, from the same call
sites — see `supply_chain/chain_client.py` in both `neocyclehub` and
`neocyclehub-bootstrap` (identical in both), called from
`supply_chain/views.py` (manual "Issue" button flow) and
`supply_chain/management/commands/issue_all.py::_issue_dpp` (bulk/setup-script
flow). Like `eu-registry`, it only runs for **DPP** credentials.

## Per-actor identity, per-role node

Each `Actor` gets its own Ethereum keypair, generated lazily the first time
that actor anchors a DPP (`chain_client._ensure_actor_account()`):

1. `eth_account.Account.create()` generates a fresh keypair — no
   mnemonic/HD-derivation dependency needed, and no shared key to reuse.
2. **No funding step.** The Besu network runs with `--min-gas-price=0`
   (permissioned demo chain, no gas economics needed), so a brand-new,
   zero-balance account can submit a transaction immediately, without a
   funding transfer from a pre-funded account.
3. `Actor.chain_address` / `Actor.chain_private_key` are saved — every
   subsequent anchor for that actor reuses the same identity.

This mirrors `Actor.did`/`Actor.idhub_token` exactly (per-actor identity
inside one shared DB), just for a second, independent trust mechanism.
**Unlike** the DID/VC signing key (which only `idhub` ever touches),
`neocyclehub` holds this key directly — for a permissioned-chain story, each
participating organization should sign with its own identity rather than
delegating to a third-party custodian.

On top of that, `chain_client._node_rpc_url()` routes each actor's
transaction through **its role's validator node** — `Actor.role`
(`supplier`/`upcycler`/`manufacturer`) maps to
`besu-supplier`/`besu-upcycler`/`besu-manufacturer` (`NODE_BY_ROLE`) —
*except* UPC, which routes to its own dedicated
`besu-upc` node instead of the shared upcycler one (matched by actor name,
since `Actor` has no dedicated per-company routing field). S1 and
S2 share `besu-supplier`; U1, U2, U3, U4, and U5 share
`besu-upcycler`; M1 and M2 share `besu-manufacturer`; UPC alone has
`besu-upc` — the topology is otherwise one node per role rather than
one per company. Identity is
per-actor (distinct keys for every one of the ten); only the node they
submit through is shared or not. An S1 DPP is signed by S1's
own key *and* submitted via the supplier node, gossiped to the other
three, and only becomes canonical once agreed by QBFT consensus; a UPC DPP
is gossiped from its own node the same way.

## Failure mode: best-effort, non-blocking

Same convention as `register_dpp()`: `anchor_dpp()` catches any exception
and only logs a warning — it never raises. A Besu network outage does not
block DPP issuance.

## Contract

`blockchain/contracts/DPPAnchor.sol` — event-only, no storage. Being plain
EVM code, the same contract, ABI, and web3.py signing/sending code work
against any EVM-compatible JSON-RPC endpoint:

```solidity
event Anchored(bytes32 indexed dppHash, address indexed actor, string dppUrl, uint256 timestamp);
function anchor(bytes32 dppHash, string calldata dppUrl) external {
    emit Anchored(dppHash, msg.sender, dppUrl, block.timestamp);
}
```

`msg.sender` — the actor's own chain account — *is* the on-chain identity,
so no actor address needs to be passed as an argument. `dppHash` is
`keccak256(json.dumps(issued_credential.credential, sort_keys=True))`: a
hash of the full signed credential (not just its metadata), computed
locally by `chain_client.py`.

## Running / wiring

Part of the standard `docker compose up --build` (see `README.md` /
`HOWTO.md`) — no separate setup step. Compose specifics:

- Services `besu-supplier`, `besu-upcycler`, `besu-manufacturer`, `besu-upc`
  — official `hyperledger/besu:26.7.1` image, each running QBFT with
  `--min-gas-price=0`. Each mounts its own config from
  `blockchain/besu-config/<role>/`: a shared `genesis.json`, its own `key`
  (validator private key), and a `static-nodes.json` listing the other
  three peers.
- **Fixed IPs are required.** Besu's static-nodes parser rejects DNS
  hostnames outright (`Illegal static enode supplied` — verified
  empirically while building this, not assumed), so the four nodes peer
  via literal addresses on a dedicated subnet (`172.28.0.11/12/13/14`,
  declared in the top-level `networks:` block) rather than their
  container/service names. JSON-RPC calls from `neocyclehub`/`chain-deploy`
  to these nodes *do* use normal service-name DNS
  (`http://besu-supplier:8545`) — the hostname restriction is specific to
  Besu's p2p static-peer config, not general Docker networking.
- `genesis.json` and the 4 validator keypairs are **pre-generated, committed
  files**, not created at container start. They came from Besu's own
  `operator generate-blockchain-config` tool (run once against the official
  image, `count: 4`; the RLP-encoded `extraData` field must not be
  hand-crafted — see
  `blockchain/besu-config/qbftConfigFile.json` for the generation input).
- `chain-deploy` (one-shot) `depends_on` all four Besu nodes being
  healthy — QBFT needs a live network to include a deployment transaction
  in a block. It deploys `DPPAnchor` with the Hardhat tooling in
  `blockchain/`: `hardhat.config.js`'s `besu` network entry points at
  `besu-supplier`'s RPC with a fixed, funding-free deployer key (works
  because of `--min-gas-price=0`, same as actor accounts). If the
  deployment file in the `chain-deployment` volume already points at a
  contract with code on the chain, `deploy.js` reuses it instead of
  deploying a new one, so anchors survive `docker compose up`/`start`.
- `neocyclehub`/`neocyclehub-bootstrap` mount `chain-deployment` read-only
  and get `CHAIN_RPC_URL` as a fallback default — actual routing
  per anchor call goes through `chain_client._node_rpc_url()` instead.
- Chain state lives in each Besu node's own data volume
  (`besu-supplier-data` etc.), so it survives a single container restart (that node resyncs from its
  peers). A full reset needs all four data volumes removed, not just one.
- `besu-supplier` additionally publishes its RPC port to the host
  (`8546:8545`) — the only one of the four that does. Purely a convenience
  for poking at the network directly (curl/Postman/MetaMask/Blockscout —
  see [Exploring the chain directly](#exploring-the-chain-directly) below)
  without going through `docker exec`; `besu-upcycler`/`besu-manufacturer`/
  `besu-upc` are equally part of the same shared ledger, just not
  host-exposed.

### Migration

The `Actor.chain_address`/`chain_private_key` fields come from
`supply_chain/migrations/0006_actor_chain_address_actor_chain_private_key_and_more.py`
(`neocyclehub`) / `0007_...` (`neocyclehub-bootstrap`).

## Verifying it's actually working

```bash
docker exec neocyclehub python manage.py shell -c "
from supply_chain.models import IssuedCredential
c = IssuedCredential.objects.filter(cred_type='DPP').exclude(chain_tx_hash='').first()
print(c.uuid, c.chain_tx_hash, c.chain_block_number)
"
```

To confirm this is a *distributed* ledger and not just "a tx happened":
recompute the hash locally and read the `Anchored` event back from a node
that **never received the anchor call** (proving the state actually
propagated via consensus, not just written to whichever node the actor's
transaction was sent to) — e.g. an upcycler's DPP was submitted to
`besu-upcycler`, so read it back from `besu-supplier` or `besu-manufacturer`.

Or open the dashboard's credential chain view (`/credentials/<uuid>/chain/`)
— every DPP node runs exactly this check live (`chain_client.verify_anchor()`,
see below) and shows the result as a badge next to its signature-verification
badge: "⛓ anchored ✓" (hash matches), "⛓ MISMATCH" (tampering or a changed
credential), "⛓ not anchored" (no tx recorded), or "⛓ unverified" (Besu
unreachable right now). Hover any badge for the tx hash / block number /
detail.

## Live anchor re-verification (`verify_anchor()`)

`chain_tx_hash`/`chain_block_number` are written once, at issuance time,
by `anchor_dpp()`. `chain_client.verify_anchor(issued_credential)` re-checks
them live against Besu, so the "⛓ anchored" badge means "this credential's
anchor checks out right now," not only "a transaction hash was recorded
once":

1. If `chain_tx_hash` is empty, return `not_anchored` immediately — no
   further calls (this DPP was never anchored, or `anchor_dpp()` failed
   best-effort at issuance time).
2. Otherwise, connect to the anchoring actor's own node
   (`_node_rpc_url()`, same routing `anchor_dpp()` used) and fetch the
   transaction receipt.
3. No receipt → `unavailable` (Besu unreachable, or the tx hasn't
   propagated to this node yet — absence of evidence, not evidence of
   absence).
4. Receipt present but `status != 1` → `invalid` (the anchor transaction
   itself reverted on-chain).
5. Decode the `Anchored` event from the receipt via the deployed contract's
   ABI, pull out its `dppHash`, and recompute
   `keccak256(json.dumps(credential, sort_keys=True))` from the credential's
   **current** stored content.
6. Hashes match → `valid`. Don't match → `invalid` — this is the case that
   actually matters: it's what would catch a credential that was tampered
   with (or somehow re-issued with different content) after being anchored.

Only meaningful for DPPs — `_build_chain()` (`views.py`) only calls it when
`cred_type == 'DPP'`; DTE/DCC/DFR/DIA are never anchored in the first place.

This isn't free: it's one more live Besu RPC call per DPP node in a chain
walk, on top of the existing per-node idhub signature check. The metrics
dashboard records what that costs in practice; *where in the chain you
start verifying from* matters far more than this one addition does.

## Strict anchor check (`verify_anchor_strict()`)

`verify_anchor()` follows the transaction hash the issuer stored itself and
accepts an `Anchored` event from whatever contract emitted it. An issuer
that silently replaces a credential can therefore anchor the new hash and
repoint its stored transaction hash, and `verify_anchor()` would accept
the replacement. `chain_client.verify_anchor_strict(issued_credential)`
trusts no issuer-stored pointer: it queries the pinned `DPPAnchor` contract
for all `Anchored` events of the credential's URL, takes the **earliest**
one, and checks both its hash and that its sender is the issuing actor's
account. Logs are queried in 5000-block windows, Besu's default
`--rpc-max-logs-range`. The threat tests in `experiments/` compare both
checks.

**To see the fault-tolerance property directly** (stop one validator,
confirm the network keeps producing blocks — this is the concrete evidence
behind the crash-fault-tolerance claim in Known limitations below):

```bash
docker stop besu-manufacturer
# poll block height on besu-supplier — it keeps advancing
docker start besu-manufacturer
# poll again — besu-manufacturer resyncs and catches back up to the same height
```

Verified against `besu-upc` specifically: stopping it drops `besu-supplier`'s `admin_peers` count from 3 to 2, but the
network keeps producing blocks (block height kept advancing); a UPC-issued
DPP's anchor call fails cleanly during that window (`besu-upc` connection
refused, caught by `anchor_dpp()`'s best-effort try/except — logged, not
raised) while every other actor's anchoring is unaffected; restarting
`besu-upc` resyncs it and its peer count returns to 3 within seconds.

## Exploring the chain directly

`besu-supplier`'s RPC is published at `http://localhost:8546`, so standard
Ethereum JSON-RPC tools work straight from the host — no `docker exec`
needed:

```bash
curl -s -X POST -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["<tx hash>"],"id":1}' \
  http://localhost:8546
```

Same endpoint works with MetaMask (add a custom network: RPC URL
`http://localhost:8546`, chain ID `4004`) or Postman for interactive
poking.

For actually *browsing* the chain — every anchored DPP's transaction, the
`Anchored` event log, per-account history — rather than hand-decoding raw
JSON-RPC responses, [Blockscout](https://www.blockscout.com/) is the
natural fit: it's open-source, self-hostable, and Besu documents it
directly as a supported explorer for private/permissioned networks. It's
not wired into this repo (a manual step, typically a Postgres + Blockscout
container pair pointed at `http://localhost:8546` / chain ID `4004`) — a
reasonable next addition if this PoC needs a nicer way to show anchoring
results to someone who isn't going to run `curl` commands.

## Known limitations

- **Byzantine-fault-tolerant for 1 validator.** The standard BFT formula
  (`N ≥ 3f+1`) needs `N=4` to tolerate `f=1`; three validators would be
  crash-fault-tolerant for one node but tolerate zero Byzantine faults (a
  single validator equivocating or signing conflicting blocks). The fourth
  node, `besu-upc`, reaches that threshold. **What this does and doesn't prove:**
  it's a real protocol guarantee of the QBFT consensus algorithm, not
  something this project independently re-derived — genuine Byzantine
  behavior (a validator deliberately signing conflicting blocks) has not
  been tested here, since that needs deliberately running modified/malicious
  validator software, a separate and more specialized undertaking. What
  *has* been verified directly: stopping `besu-upc` drops the network to 3 live validators, block
  production continues uninterrupted, only UPC's own anchoring fails while
  it's down, and it resyncs cleanly on restart (see the fault-tolerance
  example above). Every validator added past this one buys nothing further
  on this specific property until a seventh (`N ≥ 3f+1` for `f=2` needs
  `N=7`).
- **`Actor.chain_private_key` is stored in plaintext in the DB.** Fine for
  a local devnet with zero real-world value (same spirit as this repo's
  existing `SECRET_KEY=idhub-dev-secret-change-me` / `admin1234`), but never
  reuse this storage pattern past a local demo.
- **Validator keys are fixed and committed to the repo.** Appropriate for a
  local permissioned demo (there's no secrecy value to protect — anyone
  running this stack controls all 4 nodes anyway), but obviously wrong for
  any real permissioned deployment, where each organization would generate
  and hold its own validator key privately.
- **Still one node per role for three of the four validators, not one per
  company.** UPC's node is the only per-company validator; the nine other
  companies share three role nodes.
- **Non-blocking is a simplification**, same as `eu-registry`'s — a chain
  outage doesn't block DPP issuance, so this cannot be relied on as a
  guarantee that every DPP is anchored.
- **No retroactive backfill.** DPPs issued while the Besu
  network/`chain-deploy` was down never get anchored
  unless manually re-issued — same caveat as `eu-registry`.
