// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";
import "./BridgeToken.sol";

contract Destination is AccessControl {
    bytes32 public constant CREATOR_ROLE = keccak256("CREATOR_ROLE");
    bytes32 public constant WARDEN_ROLE = keccak256("WARDEN_ROLE");

    event Creation(address indexed underlying, address indexed bridgeToken);
    event Wrap(address indexed underlying, address indexed recipient, uint256 amount);
    event Unwrap(address indexed bridgeToken, address indexed recipient, uint256 amount);

    // Mapping from underlying token address to deployed BridgeToken
    mapping(address => address) public bridgeTokens;

    constructor() {
        _setupRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _setupRole(CREATOR_ROLE, msg.sender);
    }

    function createToken(address underlying, string memory name, string memory symbol)
        external
        onlyRole(CREATOR_ROLE)
        returns (address)
    {
        require(bridgeTokens[underlying] == address(0), "Token already registered");

        BridgeToken bridgeToken = new BridgeToken(name, symbol, underlying);
        bridgeTokens[underlying] = address(bridgeToken);

        emit Creation(underlying, address(bridgeToken));
        return address(bridgeToken);
    }

    function wrap(address underlying, address recipient, uint256 amount)
        external
        onlyRole(WARDEN_ROLE)
    {
        address bridgeTokenAddr = bridgeTokens[underlying];
        require(bridgeTokenAddr != address(0), "Token not registered");

        BridgeToken(bridgeTokenAddr).mint(recipient, amount);
        emit Wrap(underlying, recipient, amount);
    }

    function unwrap(address bridgeTokenAddr, address recipient, uint256 amount) external {
        ERC20Burnable token = ERC20Burnable(bridgeTokenAddr);
        require(token.balanceOf(msg.sender) >= amount, "Insufficient balance");

        token.burnFrom(msg.sender, amount);
        emit Unwrap(bridgeTokenAddr, recipient, amount);
    }
}
