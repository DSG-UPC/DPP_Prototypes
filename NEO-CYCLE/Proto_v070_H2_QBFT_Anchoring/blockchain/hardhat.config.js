require("@nomicfoundation/hardhat-toolbox");

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: "0.8.24",
  networks: {
    // Points at one of the 3 Besu QBFT validator nodes' JSON-RPC port —
    // used by scripts/deploy.js, run from a separate one-shot container.
    // Besu has no Hardhat-style unlocked dev accounts, so the deployer key
    // is supplied explicitly. It needs no funding: the network runs with
    // --min-gas-price=0 (see docker-compose.yml), so any keypair can send
    // transactions immediately — this is a fixed, throwaway demo-only key
    // with zero real-world value, same spirit as this repo's other fixed
    // dev credentials (e.g. idhub's SECRET_KEY).
    besu: {
      url: process.env.BESU_RPC_URL || "http://besu-recycler:8545",
      accounts: [
        process.env.BESU_DEPLOYER_KEY ||
          "0xe87a7082e9c956be2eed9fcb8502e88c5512597094a7208454f8241a93b807ed",
      ],
      gasPrice: 0,
    },
  },
};
