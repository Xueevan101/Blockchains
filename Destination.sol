// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";
import "./BridgeToken.sol";

contract Destination is AccessControl {
    bytes32 public constant WARDEN_ROLE = keccak256("BRIDGE_WARDEN_ROLE");
    bytes32 public constant CREATOR_ROLE = keccak256("CREATOR_ROLE");

    mapping(address => address) public underlying_tokens;
    mapping(address => address) public reverse_wrapped_tokens;
    mapping(address => address) public wrapped_tokens;

    address[] public tokens;

    event Creation(address indexed underlying_token, address indexed wrapped_token);
    event Wrap(address indexed underlying_token, address indexed wrapped_token, address indexed to, uint256 amount);
    event Unwrap(address indexed underlying_token, address indexed wrapped_token, address frm, address indexed to, uint256 amount);

    constructor(address admin) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(CREATOR_ROLE, admin);
        _grantRole(WARDEN_ROLE, admin);
    }
    function createToken(address underlying, string memory name, string memory symbol)
	    external
	    onlyRole(CREATOR_ROLE)
	    returns (address)
	{
	    require(wrapped_tokens[underlying] == address(0), "Token already registered");
	
	    BridgeToken bridgeToken = new BridgeToken(underlying, name, symbol, address(this));
	    address wtoken = address(bridgeToken);
	
	    wrapped_tokens[underlying] = wtoken;
	    underlying_tokens[wtoken] = underlying;
	    reverse_wrapped_tokens[wtoken] = underlying;
	
	    tokens.push(wtoken);
	
	    emit Creation(underlying, wtoken);
	
	    return wtoken;
	}

    function wrap(address underlying, address recipient, uint256 amount)
	    external
	    onlyRole(WARDEN_ROLE)
	{
	    address bridgeTokenAddr = wrapped_tokens[underlying];
	    require(bridgeTokenAddr != address(0), "Token not registered");
	
	    BridgeToken(bridgeTokenAddr).mint(recipient, amount);
	
	    emit Wrap(underlying, bridgeTokenAddr, recipient, amount);
	}

    function unwrap(address bridgeTokenAddr, address recipient, uint256 amount) external {
	    address underlying = underlying_tokens[bridgeTokenAddr];
	    require(underlying != address(0), "Not registered");
	
	    BridgeToken token = BridgeToken(bridgeTokenAddr);
	    require(token.balanceOf(msg.sender) >= amount, "Insufficient balance");
	
	    token.burnFrom(msg.sender, amount);
	
	    emit Unwrap(underlying, bridgeTokenAddr, msg.sender, recipient, amount);
	}
}

