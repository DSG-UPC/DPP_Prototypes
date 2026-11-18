// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Anchors a hash of an issued DPP on-chain, as tamper-evidence for
/// eu-registry's off-chain index (see /CHAIN_ANCHOR.md). Event-only, no
/// storage: `msg.sender` (the issuing actor's own Hardhat dev account) is
/// the on-chain identity, so no actor address needs to be passed explicitly.
contract DPPAnchor {
    event Anchored(bytes32 indexed dppHash, address indexed actor, string dppUrl, uint256 timestamp);

    function anchor(bytes32 dppHash, string calldata dppUrl) external {
        emit Anchored(dppHash, msg.sender, dppUrl, block.timestamp);
    }
}
