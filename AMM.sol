// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.17;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract AMM is AccessControl {
    bytes32 public constant LP_ROLE = keccak256("LP_ROLE");
    uint256 public invariant;
    address public tokenA;
    address public tokenB;
    uint256 feebps = 3; // Fee in basis points

    event Swap(address indexed _inToken, address indexed _outToken, uint256 inAmt, uint256 outAmt);
    event LiquidityProvision(address indexed _from, uint256 AQty, uint256 BQty);
    event Withdrawal(address indexed _from, address indexed recipient, uint256 AQty, uint256 BQty);

    constructor(address _tokenA, address _tokenB) {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(LP_ROLE, msg.sender);

        require(_tokenA != address(0), 'Token address cannot be 0');
        require(_tokenB != address(0), 'Token address cannot be 0');
        require(_tokenA != _tokenB, 'Tokens cannot be the same');
        tokenA = _tokenA;
        tokenB = _tokenB;
    }

    function getTokenAddress(uint256 index) public view returns (address) {
        require(index < 2, 'Only two tokens');
        return (index == 0) ? tokenA : tokenB;
    }

    function tradeTokens(address sellToken, uint256 sellAmount) public {
        require(invariant > 0, 'Invariant must be nonzero');
        require(sellToken == tokenA || sellToken == tokenB, 'Invalid token');
        require(sellAmount > 0, 'Cannot trade 0');

        address buyToken = (sellToken == tokenA) ? tokenB : tokenA;

        // Pull sellAmount of sellToken from sender
        require(ERC20(sellToken).transferFrom(msg.sender, address(this), sellAmount), "Transfer failed");

        uint256 reserveIn = ERC20(sellToken).balanceOf(address(this)) - sellAmount; // exclude the new input
        uint256 reserveOut = ERC20(buyToken).balanceOf(address(this));

        // Apply fee and compute output using constant product formula
        uint256 amountInWithFee = (sellAmount * (10000 - feebps)) / 10000;
        uint256 numerator = amountInWithFee * reserveOut;
        uint256 denominator = reserveIn + amountInWithFee;
        uint256 amountOut = numerator / denominator;

        require(amountOut > 0, "Insufficient output amount");

        // Transfer output tokens to user
        require(ERC20(buyToken).transfer(msg.sender, amountOut), "Transfer failed");

        emit Swap(sellToken, buyToken, sellAmount, amountOut);

        invariant = ERC20(tokenA).balanceOf(address(this)) * ERC20(tokenB).balanceOf(address(this));
    }

    function provideLiquidity(uint256 amtA, uint256 amtB) public {
        require(amtA > 0 || amtB > 0, 'Cannot provide 0 liquidity');

        require(ERC20(tokenA).transferFrom(msg.sender, address(this), amtA), "Token A transfer failed");
        require(ERC20(tokenB).transferFrom(msg.sender, address(this), amtB), "Token B transfer failed");

        invariant = ERC20(tokenA).balanceOf(address(this)) * ERC20(tokenB).balanceOf(address(this));

        emit LiquidityProvision(msg.sender, amtA, amtB);
    }

    function withdrawLiquidity(address recipient, uint256 amtA, uint256 amtB) public onlyRole(LP_ROLE) {
        require(amtA > 0 || amtB > 0, 'Cannot withdraw 0');
        require(recipient != address(0), 'Cannot withdraw to 0 address');

        if (amtA > 0) {
            require(ERC20(tokenA).transfer(recipient, amtA), "Token A transfer failed");
        }
        if (amtB > 0) {
            require(ERC20(tokenB).transfer(recipient, amtB), "Token B transfer failed");
        }

        invariant = ERC20(tokenA).balanceOf(address(this)) * ERC20(tokenB).balanceOf(address(this));

        emit Withdrawal(msg.sender, recipient, amtA, amtB);
    }
}
