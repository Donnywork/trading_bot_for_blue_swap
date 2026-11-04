// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

// Test-only contracts. Not deployed.
contract MockToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function decimals() external pure returns (uint8) { return 18; }

    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

// Swaps 1:1 and mimics SwapRouter02: pulls tokenIn from the caller via allowance, pays tokenOut.
contract MockRouter {
    // last call, so tests can assert what the bot actually sent
    address public lastRecipient;
    uint24 public lastFee;
    uint256 public lastAmountIn;
    uint160 public lastSqrtPriceLimit;
    uint256 public lastAllowance; // allowance the router saw at swap time

    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata p) external payable returns (uint256) {
        lastRecipient = p.recipient;
        lastFee = p.fee;
        lastAmountIn = p.amountIn;
        lastSqrtPriceLimit = p.sqrtPriceLimitX96;
        lastAllowance = MockToken(p.tokenIn).allowance(msg.sender, address(this));
        MockToken(p.tokenIn).transferFrom(msg.sender, address(this), p.amountIn);
        require(p.amountIn >= p.amountOutMinimum, "Too little received");
        MockToken(p.tokenOut).mint(p.recipient, p.amountIn);
        return p.amountIn;
    }
}

// USDT-style: no return values; approve(nonzero -> nonzero) reverts.
contract NoReturnToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 a) external { balanceOf[to] += a; }

    function approve(address s, uint256 a) external {
        require(a == 0 || allowance[msg.sender][s] == 0, "usdt approve");
        allowance[msg.sender][s] = a;
    }

    function transfer(address to, uint256 a) external {
        balanceOf[msg.sender] -= a;
        balanceOf[to] += a;
    }

    function transferFrom(address f, address to, uint256 a) external {
        allowance[f][msg.sender] -= a;
        balanceOf[f] -= a;
        balanceOf[to] += a;
    }
}

// Returns false from everything (no revert, no effect).
contract FalseToken {
    function approve(address, uint256) external pure returns (bool) { return false; }
    function transfer(address, uint256) external pure returns (bool) { return false; }
}

// Tries to call back into the bot during approve/transfer.
contract ReentrantToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public bot;
    bytes public attack;
    bool public attempted;
    bool public reentrySucceeded;

    function mint(address to, uint256 a) external { balanceOf[to] += a; }

    function setup(address b, bytes calldata a) external { bot = b; attack = a; }

    function _go() internal {
        attempted = true;
        (bool ok,) = bot.call(attack);
        if (ok) reentrySucceeded = true;
    }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        _go();
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        balanceOf[msg.sender] -= a;
        balanceOf[to] += a;
        _go();
        return true;
    }

    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        allowance[f][msg.sender] -= a;
        balanceOf[f] -= a;
        balanceOf[to] += a;
        return true;
    }
}

// Pulls only half of amountIn (like a V3 partial fill), leaving a residual allowance.
contract PartialRouter {
    struct P {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(P calldata p) external payable returns (uint256) {
        NoReturnToken(p.tokenIn).transferFrom(msg.sender, address(this), p.amountIn / 2);
        NoReturnToken(p.tokenOut).mint(p.recipient, p.amountIn / 2);
        return p.amountIn / 2;
    }
}
