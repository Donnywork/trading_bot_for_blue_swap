// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import "../TradingBot.sol";

interface IMockArbToken {
    function mint(address to, uint256 amount) external;
}

contract MockArbFactory {
    mapping(bytes32 => address) private pools;

    function setPool(address a, address b, uint24 fee, address pool) external {
        pools[keccak256(abi.encode(a < b ? a : b, a < b ? b : a, fee))] = pool;
    }

    function getPool(address a, address b, uint24 fee) external view returns (address) {
        return pools[keccak256(abi.encode(a < b ? a : b, a < b ? b : a, fee))];
    }
}

contract MockArbPool {
    address public immutable token0;
    address public immutable token1;
    address public immutable factory;
    uint24 public immutable fee;

    constructor(address a, address b, uint24 f, address fac) {
        token0 = a;
        token1 = b;
        fee = f;
        factory = fac;
    }
}

/// Route-specific rates and finite available input model full fills versus range-limited fills.
/// It also supports deliberately dishonest return values and reentry for adversarial tests.
contract MockArbRouter {
    address public immutable factory;
    struct Route {
        uint256 numerator;
        uint256 denominator;
        uint256 availableInput;
        uint256 lie;
        bool ignoreMinimum;
    }
    mapping(bytes32 => Route) public routes;
    uint256 public calls;
    address public lastRecipient;
    uint160 public lastLimit;
    address public callback;
    bytes public callbackData;
    bool public reentrySucceeded;

    constructor(address fac) { factory = fac; }

    function setRoute(address input, uint24 fee, Route calldata r) external {
        routes[keccak256(abi.encode(input, fee))] = r;
    }

    function setCallback(address target, bytes calldata data) external {
        callback = target;
        callbackData = data;
    }

    function exactInputSingle(ISwapRouter02.ExactInputSingleParams calldata p) external payable returns (uint256) {
        calls++;
        lastRecipient = p.recipient;
        lastLimit = p.sqrtPriceLimitX96;
        if (callback != address(0)) {
            (bool ok,) = callback.call(callbackData);
            reentrySucceeded = ok;
        }
        Route memory r = routes[keccak256(abi.encode(p.tokenIn, p.fee))];
        uint256 spent = p.amountIn < r.availableInput ? p.amountIn : r.availableInput;
        uint256 out = spent * r.numerator / r.denominator;
        (bool ok, bytes memory data) = p.tokenIn.call(abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", msg.sender, address(this), spent));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "pull failed");
        require(r.ignoreMinimum || out >= p.amountOutMinimum, "Too little received");
        IMockArbToken(p.tokenOut).mint(p.recipient, out);
        return out + r.lie;
    }
}
