// SPDX-License-Identifier: MIT
pragma solidity 0.8.27;

import {ISwapRouter02} from "./TradingBot.sol";

interface IP3Token {
    function decimals() external view returns (uint8);
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface IP3Credit {
    function f564() external view returns (address);
    function fETH() external view returns (address);
    function pricingPool() external view returns (address);
    function oraclePrice() external view returns (uint256, uint32);
    function collateralOf(address, address) external view returns (uint256);
    function debtOf(address, address) external view returns (uint256);
    function deposit(address, uint256) external;
    function withdraw(address, uint256) external;
    function borrow(address, uint256) external;
    function repay(address, uint256) external returns (uint256);
    function liquidate(address, address, address, uint256) external returns (uint256, uint256);
}

interface IP3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
}

interface IP3Router { function factory() external view returns (address); }
interface IP3Factory { function getPool(address, address, uint24) external view returns (address); }

contract Phase3Vault {
    uint256 public constant maxLtvBps = 4000;
    address public owner;
    address public pendingOwner;
    address public operator;
    address public immutable quote;
    address public immutable base;
    address public immutable pool;
    IP3Credit public immutable credit;
    ISwapRouter02 public immutable router;
    bool public borrowingEnabled;
    bool private entered;

    // Caps and reserves are in the token's smallest unit. Zero debt/liquidation caps disable exposure.
    struct Limits { uint256 debtCap; uint256 liquidReserve; uint256 maxLiquidation; }
    mapping(address => Limits) public limits;
    struct Lot {
        address borrower;
        address debtToken;
        address collateralToken;
        uint256 maxRepay;
        uint256 minSeized;
        uint256 deadline;
    }
    event LimitsUpdated(address indexed token, uint256 debtCap, uint256 liquidReserve, uint256 maxLiquidation);
    event BorrowingEnabled(bool enabled);
    event OperatorChanged(address indexed operator);
    event OwnerProposed(address indexed successor);
    event OwnerChanged(address indexed owner);
    event WithdrawnToOwner(address indexed token, uint256 amount);
    event LiquidationSettled(address indexed borrower, address indexed debtToken, address indexed collateralToken, uint256 repaid, uint256 seized, bool closed, uint256 gain);

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier authorized() { require(msg.sender == owner || msg.sender == operator, "not operator"); _; }
    modifier lock() { require(!entered, "reentrant"); entered = true; _; entered = false; }

    constructor(address owner_, address operator_, address credit_, address router_, address quote_, address base_, address pool_) {
        require(owner_ != address(0) && operator_ != address(0) && owner_ != operator_ && owner_ != address(this), "roles");
        credit = IP3Credit(credit_);
        router = ISwapRouter02(router_);
        require(quote_ != base_ && credit.f564() == quote_ && credit.fETH() == base_ && credit.pricingPool() == pool_, "credit identity");
        require(IP3Token(quote_).decimals() == 18 && IP3Token(base_).decimals() == 18, "decimals");
        IP3Pool p = IP3Pool(pool_);
        require(p.token0() == quote_ && p.token1() == base_ && p.fee() == 3000, "pool identity");
        require(IP3Factory(IP3Router(router_).factory()).getPool(quote_, base_, 3000) == pool_, "router pool");
        quote = quote_; base = base_; pool = pool_;
        owner = owner_; operator = operator_;
        _limits(quote_, Limits(0, 1000 ether, 0));
        _limits(base_, Limits(0, 1 ether, 0));
        emit OwnerChanged(owner_); emit OperatorChanged(operator_);
    }

    function setLimits(address token, Limits calldata value) external onlyOwner lock { _token(token); _limits(token, value); }
    function _limits(address token, Limits memory value) private {
        limits[token] = value;
        emit LimitsUpdated(token, value.debtCap, value.liquidReserve, value.maxLiquidation);
    }
    function setBorrowingEnabled(bool enabled) external onlyOwner lock { borrowingEnabled = enabled; emit BorrowingEnabled(enabled); }
    function setOperator(address next) external onlyOwner lock {
        require(next != owner && next != address(this), "roles"); operator = next; emit OperatorChanged(next);
    }
    function transferOwnership(address next) external onlyOwner lock {
        require(next != address(0) && next != address(this) && next != operator, "roles"); pendingOwner = next; emit OwnerProposed(next);
    }
    function acceptOwnership() external lock {
        require(msg.sender == pendingOwner && msg.sender != operator, "not successor"); owner = msg.sender; pendingOwner = address(0); emit OwnerChanged(owner);
    }
    function withdrawToOwner(address token, uint256 amount) external onlyOwner lock {
        require(IP3Token(token).transfer(owner, amount), "transfer"); emit WithdrawnToOwner(token, amount);
    }
    function _token(address token) private view { require(token == quote || token == base, "unknown token"); }
    function _balance(address token) private view returns (uint256) { return IP3Token(token).balanceOf(address(this)); }
    function _approve(address token, address spender, uint256 amount) private { require(IP3Token(token).approve(spender, amount), "approval"); }
    function _retain(address token) private view { require(_balance(token) >= limits[token].liquidReserve, "liquid reserve"); }

    function deposit(address token, uint256 amount) external authorized lock {
        _token(token); _approve(token, address(credit), amount); credit.deposit(token, amount);
        _approve(token, address(credit), 0); _retain(token);
    }
    function repay(address token, uint256 maximum) external authorized lock returns (uint256 paid) {
        _token(token); _approve(token, address(credit), maximum); paid = credit.repay(token, maximum);
        _approve(token, address(credit), 0); require(paid > 0 && paid <= maximum, "repayment");
    }
    function borrow(address token, uint256 amount) external authorized lock {
        _token(token); require(borrowingEnabled, "borrowing disabled"); credit.borrow(token, amount); _checkRisk(true);
    }
    function withdrawCollateral(address token, uint256 amount) external authorized lock {
        _token(token); credit.withdraw(token, amount); _checkRisk(false);
    }
    function _checkRisk(bool checkCaps) private view {
        uint256 dq = credit.debtOf(address(this), quote);
        uint256 db = credit.debtOf(address(this), base);
        if (checkCaps) require(dq <= limits[quote].debtCap && db <= limits[base].debtCap, "debt cap");
        if (dq == 0 && db == 0) return;
        (uint256 price, uint32 history) = credit.oraclePrice();
        require(price > 0 && history >= 1800, "oracle history");
        uint256 debt = dq * 1 ether + db * price;
        uint256 collateral = credit.collateralOf(address(this), quote) * 1 ether + credit.collateralOf(address(this), base) * price;
        require(debt * 10000 <= collateral * maxLtvBps, "vault LTV");
    }

    function _liquidate(Lot calldata l) private returns (uint256 repaid, uint256 seized) {
        _token(l.debtToken); _token(l.collateralToken);
        require(l.borrower != address(this) && block.timestamp <= l.deadline, "invalid lot");
        require(l.maxRepay > 0 && l.maxRepay <= limits[l.debtToken].maxLiquidation && l.minSeized > 0, "lot limits");
        require(_balance(l.debtToken) >= l.maxRepay, "repayment balance");
        _approve(l.debtToken, address(credit), l.maxRepay);
        (repaid, seized) = credit.liquidate(l.borrower, l.debtToken, l.collateralToken, l.maxRepay);
        _approve(l.debtToken, address(credit), 0);
        require(repaid > 0 && repaid <= l.maxRepay && seized >= l.minSeized, "lot result");
    }
    function liquidate(Lot calldata l) external authorized lock returns (uint256 repaid, uint256 seized) {
        (repaid, seized) = _liquidate(l); _retain(l.debtToken);
        emit LiquidationSettled(l.borrower, l.debtToken, l.collateralToken, repaid, seized, false, 0);
    }
    function liquidateAndClose(Lot calldata l, uint256 minGain, uint160 sqrtLimit) external authorized lock returns (uint256 gain) {
        require(minGain > 0, "zero gain");
        uint256 beforeDebt = _balance(l.debtToken);
        uint256 beforeCollateral = _balance(l.collateralToken);
        (uint256 paid, uint256 seized) = _liquidate(l);
        if (l.debtToken != l.collateralToken) {
            require(sqrtLimit > 4295128739 && sqrtLimit < 1461446703485210103287273052203988822378723970342, "price limit");
            require(_balance(l.collateralToken) == beforeCollateral + seized, "seized balance");
            _approve(l.collateralToken, address(router), seized);
            router.exactInputSingle(ISwapRouter02.ExactInputSingleParams(l.collateralToken, l.debtToken, 3000, address(this), seized, paid + minGain, sqrtLimit));
            _approve(l.collateralToken, address(router), 0);
            require(_balance(l.collateralToken) == beforeCollateral, "partial swap");
        }
        require(_balance(l.debtToken) >= beforeDebt + minGain, "insufficient gain");
        gain = _balance(l.debtToken) - beforeDebt;
        emit LiquidationSettled(l.borrower, l.debtToken, l.collateralToken, paid, seized, true, gain);
    }
}
