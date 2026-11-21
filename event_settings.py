"""Event-service configuration. Loading this module never loads dotenv or keys."""

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

import event_market as market
from runtime import ConfigurationError


def token_amount(value):
    try:
        with localcontext() as context:
            context.prec = max(100, len(str(value)) + 20)
            number = Decimal(str(value)) * market.UNIT
        if (
            not number.is_finite()
            or number <= 0
            or number != number.to_integral_value()
            or number >= 2**256
        ):
            raise ValueError("invalid token amount")
        return int(number)
    except InvalidOperation as exc:
        raise ValueError("invalid token amount") from exc


def bounded(env, name, default, lo, hi):
    try:
        number = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be an integer in {lo}..{hi}") from None
    if not lo <= number <= hi:
        raise ConfigurationError(f"{name} must be {lo}..{hi}")
    return number


def enabled(env, name, default=False):
    # Boolean flags parse a closed set: a typo must refuse startup, not trade blind.
    # An unset/empty variable falls back to the caller's default, so a feature
    # can be armed in code while an explicit 0/false stays a working opt-out.
    value = str(env.get(name, "")).strip().lower()
    if value == "":
        return default
    if value in ("0", "false"):
        return False
    if value in ("1", "true"):
        return True
    raise ConfigurationError(f"{name} must be 1/true or 0/false")


@dataclass(frozen=True)
class EventSettings:
    mode: str = "paper"
    live_enabled: bool = False
    quote_cap: int = 1000 * market.UNIT
    base_cap: int = 10 * market.UNIT
    min_profit: int = market.UNIT
    slippage_bps: int = 20
    move_bps: int = 100
    pending_backrun: bool = True
    pending_sweep: bool = True
    sweep_priority_wei: int = 16_000_000_000
    exit_seconds: int = 60
    cooldown: int = 10
    observer_seconds: int = 15
    max_gas: int = 1_500_000
    priority_wei: int = 2_000_000_000
    max_fee_wei: int = 20_000_000_000
    pending_priority_wei: int = 2
    fee_policy: str = "fixed"
    fee_baseline_wei: int = 20_000_000_000
    fee_network_bps: int = 12_500
    fee_competitor_bps: int = 15_000
    fee_tip_cap_wei: int = 100_000_000_000
    fee_max_cap_wei: int = 200_000_000_000
    bot_address: str = ""
    owner: str = ""
    market_bots: tuple = (market.MARKET_BOT,)
    competitors: tuple = (
        "0xd067640b2e032b1a878bd87b4d85cb5a87ef64ef",
        "0x21b1a1e9f20377f4cbf6d874cfcf3df37699c7b7",
    )

    @property
    def fee_ceiling(self):
        return self.max_fee_wei if self.fee_policy == "fixed" else self.fee_max_cap_wei

    @property
    def limits(self):
        return {
            "max_quote_raw": self.quote_cap,
            "max_base_raw": self.base_cap,
            "min_profit_raw": self.min_profit,
        }

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        requested = env.get("EVENT_EXECUTION_MODE", "paper")
        if requested not in ("paper", "simulate", "live"):
            raise ConfigurationError(
                "EVENT_EXECUTION_MODE must be paper, simulate or live"
            )

        def actors(name, default):
            try:
                return tuple(
                    dict.fromkeys(
                        market.hex_value(a.strip(), 20)
                        for a in env.get(name, ",".join(default)).split(",")
                        if a.strip()
                    )
                )
            except ValueError:
                raise ConfigurationError(
                    f"{name} must contain Ethereum addresses"
                ) from None

        def amount(name, default):
            try:
                return token_amount(env.get(name, default))
            except ValueError:
                raise ConfigurationError(
                    f"{name} must be a positive token amount with at most 18 decimals"
                ) from None

        bots, rivals = (
            actors("EVENT_MARKET_BOTS", cls.market_bots),
            actors("EVENT_COMPETITORS", cls.competitors),
        )
        if not bots or set(bots) & set(rivals):
            raise ConfigurationError(
                "market-bot signers must be nonempty and distinct from competitors"
            )
        if any(
            env.get(key, "").strip()
            for key in ("ARBITRAGE_VAULT_ADDRESS", "EVENT_FRONT_VAULT_ADDRESS")
        ):
            raise ConfigurationError(
                "split-vault settings are obsolete; use one Phase2TradingBot BOT_CONTRACT_ADDRESS"
            )
        addresses = [
            env.get(n, "").strip().lower()
            for n in (
                "BOT_CONTRACT_ADDRESS",
                "OWNER_ADDRESS",
            )
        ]
        for name, address in zip(
            ("BOT_CONTRACT_ADDRESS", "OWNER_ADDRESS"), addresses, strict=True
        ):
            if address:
                try:
                    market.hex_value(address, 20)
                except ValueError:
                    raise ConfigurationError(
                        f"{name} must be an Ethereum address"
                    ) from None
                if int(address, 16) == 0:
                    raise ConfigurationError(f"{name} must be nonzero")
        if addresses[0] and not addresses[1]:
            raise ConfigurationError(
                "simulation/live requires OWNER_ADDRESS and a Phase2TradingBot BOT_CONTRACT_ADDRESS"
            )
        priority = bounded(
            env, "EVENT_PRIORITY_FEE_WEI", 2_000_000_000, 1, 100_000_000_000
        )
        max_fee = bounded(env, "EVENT_MAX_FEE_WEI", 20_000_000_000, 1, 200_000_000_000)
        if priority > max_fee:
            raise ConfigurationError("priority fee exceeds maximum fee")
        # Default 2 wei: the market bot's targets carry tip=2 wei / maxFee~32
        # wei (measured 2026-11-21), and matching that fee group is what keeps
        # the cycle in the target's block. Tip 0 proved too low - zero-tip
        # transactions are the first pushed out of a filling block, so both
        # tip-0 cycles landed a block late in the zero tail (blocks 12180474
        # idx 138 and 12181496 idx 86) behind competitor-1's 15 gwei sweep.
        pending_tip = bounded(
            env, "EVENT_PENDING_PRIORITY_FEE_WEI", 2, 0, 100_000_000_000
        )
        # The sweep replicates competitor-1's next-block play: a tip high
        # enough to top the block AFTER the target's when the same-block
        # cycle misses it (they use 15 gwei and land index 0; 16 gwei with
        # our faster broadcast wins the same-tip arrival tie).
        sweep_tip = bounded(
            env, "EVENT_SWEEP_PRIORITY_FEE_WEI", 16_000_000_000, 1, 100_000_000_000
        )
        policy = env.get("EVENT_FEE_POLICY", "fixed")
        if policy not in ("fixed", "competitive"):
            raise ConfigurationError("EVENT_FEE_POLICY must be fixed or competitive")
        baseline = bounded(
            env, "EVENT_FEE_BASELINE_WEI", 20_000_000_000, 1, 100_000_000_000
        )
        tip_cap = bounded(
            env, "EVENT_FEE_TIP_CAP_WEI", 100_000_000_000, 1, 100_000_000_000
        )
        fee_cap = bounded(
            env, "EVENT_FEE_MAX_CAP_WEI", 200_000_000_000, 1, 200_000_000_000
        )
        if not baseline <= tip_cap <= fee_cap:
            raise ConfigurationError(
                "competitive fees require baseline <= tip cap <= max cap"
            )
        # Tips are validated against the EFFECTIVE ceiling: max_fee under the
        # fixed policy, the competitive max cap otherwise.
        ceiling = max_fee if policy == "fixed" else fee_cap
        if pending_tip > ceiling:
            raise ConfigurationError("pending priority fee exceeds fee ceiling")
        if sweep_tip > ceiling:
            raise ConfigurationError("sweep priority fee exceeds fee ceiling")
        return cls(
            mode="simulate" if addresses[0] else "paper",
            live_enabled=env.get("DRY_RUN", "true").lower() == "false",
            quote_cap=amount("EVENT_QUOTE_CAP", "1000"),
            base_cap=amount("EVENT_BASE_CAP", "10"),
            min_profit=amount("EVENT_MIN_PROFIT", "1"),
            slippage_bps=bounded(env, "EVENT_SLIPPAGE_BPS", 20, 0, 500),
            move_bps=bounded(env, "EVENT_MOVE_BPS", 100, 1, 500),
            pending_backrun=enabled(env, "EVENT_PENDING_BACKRUN", default=True),
            pending_sweep=enabled(env, "EVENT_PENDING_SWEEP", default=True),
            sweep_priority_wei=sweep_tip,
            exit_seconds=bounded(env, "EVENT_EXIT_SECONDS", 60, 5, 300),
            cooldown=bounded(env, "EVENT_COOLDOWN_SECONDS", 10, 1, 300),
            observer_seconds=bounded(env, "EVENT_OBSERVER_SECONDS", 15, 5, 600),
            max_gas=bounded(env, "EVENT_MAX_GAS", 1_500_000, 100_000, 5_000_000),
            priority_wei=priority,
            max_fee_wei=max_fee,
            pending_priority_wei=pending_tip,
            fee_policy=policy,
            fee_baseline_wei=baseline,
            fee_network_bps=bounded(
                env, "EVENT_FEE_NETWORK_BPS", 12_500, 10_000, 50_000
            ),
            fee_competitor_bps=bounded(
                env, "EVENT_FEE_COMPETITOR_BPS", 15_000, 10_000, 50_000
            ),
            fee_tip_cap_wei=tip_cap,
            fee_max_cap_wei=fee_cap,
            bot_address=addresses[0],
            owner=addresses[1],
            market_bots=bots,
            competitors=rivals,
        )
