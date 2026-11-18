const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

// Writes the deployed contract's address + ABI to a shared volume so the
// Django services (neocyclehub/neocyclehub-bootstrap) can pick it up without
// hardcoding an address that depends on deployment order/nonce.
const OUTPUT_PATH = process.env.DEPLOYMENT_OUTPUT_PATH || "/deployed/DPPAnchor.json";

async function main() {
  // Reuse an existing deployment. Otherwise every `docker compose up` or
  // `start` would deploy a fresh contract and overwrite the address file, so
  // all anchors written before the restart would become invisible to
  // address-filtered log queries.
  if (fs.existsSync(OUTPUT_PATH)) {
    const existing = JSON.parse(fs.readFileSync(OUTPUT_PATH, "utf8"));
    const code = await hre.ethers.provider.getCode(existing.address);
    if (code && code !== "0x") {
      console.log(`DPPAnchor already deployed at ${existing.address}, reusing it`);
      return;
    }
  }

  const DPPAnchor = await hre.ethers.getContractFactory("DPPAnchor");
  const contract = await DPPAnchor.deploy();
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  const artifact = await hre.artifacts.readArtifact("DPPAnchor");

  fs.mkdirSync(path.dirname(OUTPUT_PATH), { recursive: true });
  fs.writeFileSync(
    OUTPUT_PATH,
    JSON.stringify({ address, abi: artifact.abi }, null, 2),
  );

  console.log(`DPPAnchor deployed at ${address}, written to ${OUTPUT_PATH}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
