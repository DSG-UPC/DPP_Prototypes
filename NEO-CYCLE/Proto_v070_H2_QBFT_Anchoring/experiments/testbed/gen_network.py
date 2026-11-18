#!/usr/bin/env python3
"""
Generate a standalone N-validator Hyperledger Besu QBFT network for the
anchoring experiments. Same Besu image, consensus protocol,
static peering and zero gas price as the prototype's own 4-node network in
../../docker-compose.yml, but parameterised by validator count and block
period, and isolated from the prototype's data.

Usage: python3 gen_network.py --validators 4 --block-period 2 --out net_n4_b2

Writes <out>/docker-compose.yml plus one config directory per node. Node i
gets IP 172.30.0.(10+i) and host RPC port 8600+i.
"""
import argparse
import json
import os
import shutil
import subprocess

BESU_IMAGE = "hyperledger/besu:26.7.1"
SUBNET = "172.30.0.0/24"


def ip(i):
    return f"172.30.0.{10 + i}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validators", type=int, required=True)
    ap.add_argument("--block-period", type=int, default=2)
    ap.add_argument("--request-timeout", type=int, default=10)
    ap.add_argument("--heap-mb", type=int, default=768)
    ap.add_argument("--gas-limit", default="0x47b760", help="block gas limit; default = the prototype's")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    out = os.path.abspath(a.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)

    qbft = {
        "genesis": {
            "config": {
                "chainId": 4004,
                "berlinBlock": 0,
                "qbft": {
                    "blockperiodseconds": a.block_period,
                    "epochlength": 30000,
                    "requesttimeoutseconds": a.request_timeout,
                },
            },
            "nonce": "0x0",
            "timestamp": "0x58ee40ba",
            "gasLimit": a.gas_limit,
            "difficulty": "0x1",
            "mixHash": "0x63746963616c2062797a616e74696e65206661756c7420746f6c6572616e6365",
            "coinbase": "0x0000000000000000000000000000000000000000",
            "alloc": {},
        },
        "blockchain": {"nodes": {"generate": True, "count": a.validators}},
    }
    with open(os.path.join(out, "qbftConfigFile.json"), "w") as f:
        json.dump(qbft, f, indent=2)

    subprocess.run(
        ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
         "-v", f"{out}:/work", "--entrypoint", "besu", BESU_IMAGE,
         "operator", "generate-blockchain-config",
         "--config-file=/work/qbftConfigFile.json",
         "--to=/work/networkFiles", "--private-key-file-name=key"],
        check=True, capture_output=True,
    )

    key_dirs = sorted(os.listdir(os.path.join(out, "networkFiles", "keys")))
    assert len(key_dirs) == a.validators, key_dirs
    enodes = []
    for i, kd in enumerate(key_dirs):
        node_dir = os.path.join(out, f"node{i}")
        os.makedirs(node_dir)
        src = os.path.join(out, "networkFiles", "keys", kd)
        shutil.copy(os.path.join(src, "key"), os.path.join(node_dir, "key"))
        shutil.copy(os.path.join(out, "networkFiles", "genesis.json"), os.path.join(node_dir, "genesis.json"))
        with open(os.path.join(src, "key.pub")) as f:
            pub = f.read().strip()[2:]
        enodes.append(f"enode://{pub}@{ip(i)}:30303")
    for i in range(a.validators):
        with open(os.path.join(out, f"node{i}", "static-nodes.json"), "w") as f:
            json.dump([e for j, e in enumerate(enodes) if j != i], f, indent=1)

    services = []
    for i in range(a.validators):
        services.append(f"""  node{i}:
    image: {BESU_IMAGE}
    container_name: tb-node{i}
    environment:
      - BESU_OPTS=-Xmx{a.heap_mb}m
    networks:
      tb:
        ipv4_address: {ip(i)}
    ports:
      - "{8600 + i}:8545"
    volumes:
      - ./node{i}:/config
    command: >
      --data-path=/tmp/data
      --genesis-file=/config/genesis.json
      --node-private-key-file=/config/key
      --static-nodes-file=/config/static-nodes.json
      --network-id=4004
      --discovery-enabled=false
      --p2p-enabled=true --p2p-host={ip(i)} --p2p-port=30303
      --rpc-http-enabled --rpc-http-host=0.0.0.0 --rpc-http-port=8545
      --rpc-http-api=ETH,NET,QBFT,ADMIN,WEB3,TXPOOL
      --host-allowlist=* --min-gas-price=0
""")
    compose = "services:\n" + "\n".join(services) + f"""
networks:
  tb:
    ipam:
      config:
        - subnet: {SUBNET}
"""
    with open(os.path.join(out, "docker-compose.yml"), "w") as f:
        f.write(compose)
    meta = {"validators": a.validators, "block_period": a.block_period,
            "request_timeout": a.request_timeout, "heap_mb": a.heap_mb,
            "gas_limit": a.gas_limit,
            "image": BESU_IMAGE}
    with open(os.path.join(out, "network.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(f"generated {a.validators}-validator network in {out}")


if __name__ == "__main__":
    main()
