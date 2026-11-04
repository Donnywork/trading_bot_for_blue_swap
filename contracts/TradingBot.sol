// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

// SwapRouter02 (IV3SwapRouter): the struct has no `deadline`.
interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

/// Vault + swap executor for the trading bot.
///  - owner (your enrolled wallet): the only address that can withdraw, change the operator,
///    or change the trading limits.
///  - operator (the bot's hot key): can only call swap(), and only within the owner's limits.
/// The contract never acts on its own: every swap is a transaction sent by the operator or owner.
///
/// Trading limits (set by the owner), so a leaked operator key cannot swap the vault into a
/// worthless token or an attacker-made pool:
///  - tokenCap[token]: 0 = token not allowed. A token with a nonzero cap can be swapped out to,
///    and swapped in up to `cap` per swap.
///  - feeAllowed[fee]: only these Uniswap V3 fee tiers can be used.
/// The limits do not stop a sandwich on a legitimate pool; slippage protection is the
/// operator-supplied amountOutMinimum, and the per-swap cap bounds the loss.
contract TradingBot {
    ISwapRouter02 public immutable router;
    address public owner;
    address public pendingOwner;
    address public operator;

    mapping(address => uint256) public tokenCap;
    mapping(uint24 => bool) public feeAllowed;

    event Swap(address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut);
    event Withdraw(address indexed token, uint256 amount);
    event OperatorChanged(address indexed operator);
    event TokenCapSet(address indexed token, uint256 cap);
    event FeeAllowedSet(uint24 indexed fee, bool allowed);
    event OwnershipTransferStarted(address indexed owner, address indexed pendingOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyOperator() {
        require(msg.sender == operator || msg.sender == owner, "not operator");
        _;
    }

    constructor(
        address owner_,
        address router_,
        address operator_,
        address[] memory tokens,
        uint256[] memory caps,
        uint24[] memory fees
    ) {
        require(owner_ != address(0) && owner_ != address(this), "bad owner");
        require(router_.code.length > 0, "router not a contract");
        require(tokens.length == caps.length, "length mismatch");

        owner = owner_;
        router = ISwapRouter02(router_);
        operator = operator_;
        emit OwnershipTransferred(address(0), owner_);
        emit OperatorChanged(operator_);

        for (uint256 i = 0; i < tokens.length; i++) {
            _setTokenCap(tokens[i], caps[i]);
        }
        for (uint256 i = 0; i < fees.length; i++) {
            _setFeeAllowed(fees[i], true);
        }
    }

    receive() external payable {}

    function swap(address tokenIn, address tokenOut, uint24 fee, uint256 amountIn, uint256 amountOutMinimum)
        external
        onlyOperator
        returns (uint256 amountOut)
    {
        require(tokenIn != tokenOut, "same token");
        require(feeAllowed[fee], "fee not allowed");
        require(tokenCap[tokenOut] > 0, "tokenOut not allowed");
        require(tokenCap[tokenIn] > 0, "tokenIn not allowed");
        require(amountIn > 0 && amountIn <= tokenCap[tokenIn], "amountIn out of range");
        // a zero minimum would disable slippage protection
        require(amountOutMinimum > 0, "minOut is zero");

        _call(tokenIn, abi.encodeCall(IERC20.approve, (address(router), amountIn)));

        amountOut = router.exactInputSingle(
            ISwapRouter02.ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this), // never the caller: funds only leave via withdraw()
                amountIn: amountIn,
                amountOutMinimum: amountOutMinimum,
                sqrtPriceLimitX96: 0
            })
        );

        // Clear any allowance the router did not use (e.g. a partial fill). Best effort: some
        // tokens reject approve(0), and the router is trusted, so a failure is not fatal.
        (bool reset,) = tokenIn.call(abi.encodeCall(IERC20.approve, (address(router), 0)));
        reset;

        emit Swap(tokenIn, tokenOut, amountIn, amountOut);
    }

    /// Send tokens back to the owner. Destination is fixed; there is no free-form recipient.
    function withdraw(address token, uint256 amount) external onlyOwner {
        _call(token, abi.encodeCall(IERC20.transfer, (owner, amount)));
        emit Withdraw(token, amount);
    }

    function withdrawETH(uint256 amount) external onlyOwner {
        (bool ok,) = owner.call{value: amount}("");
        require(ok, "eth transfer failed");
        emit Withdraw(address(0), amount);
    }

    /// Set a token's per-swap input cap. 0 removes the token from the allowlist.
    function setTokenCap(address token, uint256 cap) external onlyOwner {
        _setTokenCap(token, cap);
    }

    function setFeeAllowed(uint24 fee, bool allowed) external onlyOwner {
        _setFeeAllowed(fee, allowed);
    }

    /// address(0) disables the operator.
    function setOperator(address operator_) external onlyOwner {
        operator = operator_;
        emit OperatorChanged(operator_);
    }

    /// Step 1 of 2: nothing changes until the new owner calls acceptOwnership().
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0) && newOwner != address(this), "bad owner");
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    function acceptOwnership() external {
        require(msg.sender == pendingOwner, "not pending owner");
        emit OwnershipTransferred(owner, msg.sender);
        owner = msg.sender;
        pendingOwner = address(0);
    }

    function _setTokenCap(address token, uint256 cap) private {
        tokenCap[token] = cap;
        emit TokenCapSet(token, cap);
    }

    function _setFeeAllowed(uint24 fee, bool allowed) private {
        feeAllowed[fee] = allowed;
        emit FeeAllowedSet(fee, allowed);
    }

    // Low-level call so tokens that return nothing (e.g. USDT) also work.
    function _call(address token, bytes memory data) private {
        require(token.code.length > 0, "not a contract");
        (bool ok, bytes memory ret) = token.call(data);
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "token call failed");
    }
}
