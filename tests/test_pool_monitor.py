import pytest
from web3.exceptions import Web3RPCError

import pool_monitor
from pool_monitor import Swap

ME = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def swap(ts, who=OTHER, side="buy", amount=1.0, price=100.0):
    return Swap(ts=ts, recipient=who, side=side, base_amount=amount, price=price)


def swap_log(block, recipient, amount0, sqrt_price=2 * 2**96):
    return {
        "blockNumber": block,
        "args": {
            "recipient": recipient,
            "amount0": amount0,
            "sqrtPriceX96": sqrt_price,
        },
    }


# ---------------------------------------------------------------- decoding


def test_to_swaps_reads_side_amount_and_price():
    logs = [swap_log(5, ME, -3 * 10**18), swap_log(6, OTHER, 2 * 10**18)]
    buy, sell = pool_monitor.to_swaps(logs, {5: 1000, 6: 1012}, 18, 18)
    assert (buy.side, buy.base_amount, buy.ts) == ("buy", 3.0, 1000)
    assert (sell.side, sell.base_amount, sell.recipient) == ("sell", 2.0, OTHER)
    assert buy.price == pytest.approx(4.0)  # sqrtPriceX96 = 2 * 2**96 -> price 4


def test_to_swaps_adjusts_the_price_for_decimals():
    (s,) = pool_monitor.to_swaps([swap_log(1, ME, -1)], {1: 0}, 18, 6)
    assert s.price == pytest.approx(4.0 * 10**12)


# ---------------------------------------------------------------- fetching


class FakePool:
    # refuses any range wider than `limit` blocks, like a node capping eth_getLogs
    def __init__(self, limit):
        self.limit = limit
        self.ranges = []
        self.events = self
        self.Swap = self

    def get_logs(self, from_block, to_block):
        if to_block - from_block + 1 > self.limit:
            raise Web3RPCError("query returned more than 10000 results")
        self.ranges.append((from_block, to_block))
        return [{"blockNumber": b} for b in range(from_block, to_block + 1, 500)]


def test_fetch_swaps_halves_the_chunk_and_covers_every_block_once():
    pool = FakePool(limit=300)
    logs = pool_monitor.fetch_swaps(pool, 1000, 4999, chunk=2000)
    covered = [b for lo, hi in pool.ranges for b in range(lo, hi + 1)]
    assert covered == list(range(1000, 5000))
    assert len(logs) == len({e["blockNumber"] for e in logs})


def test_fetch_swaps_gives_up_at_the_smallest_chunk():
    with pytest.raises(Web3RPCError):
        pool_monitor.fetch_swaps(FakePool(limit=1), 0, 1000, chunk=200)


def test_fetch_swaps_with_nothing_to_read():
    assert pool_monitor.fetch_swaps(FakePool(limit=10), 10, 9) == []


# ---------------------------------------------------------------- actors


def test_actor_stats_counts_net_and_orders_by_activity():
    swaps = [
        swap(100, ME, "buy", 2.0),
        swap(200, ME, "sell", 0.5),
        swap(300, OTHER, "buy", 1.0),
        swap(400, ME, "buy", 1.0),
    ]
    me, other = pool_monitor.actor_stats(swaps)
    assert (me.address, me.swaps, me.buys, me.sells) == (ME, 3, 2, 1)
    assert me.net_base == pytest.approx(2.5)
    assert me.median_gap == 150  # gaps 100 and 200 -> median 150
    assert me.last_ts == 400
    assert (other.swaps, other.median_gap) == (1, None)


def test_steady_gaps_look_scripted_and_uneven_ones_do_not():
    steady = [swap(t, ME) for t in range(0, 600, 120)]  # 5 swaps, 4 equal gaps
    uneven = [swap(t, OTHER) for t in (0, 10, 400, 420, 1300)]
    by_address = {a.address: a for a in pool_monitor.actor_stats(steady + uneven)}
    assert by_address[ME].scripted
    assert not by_address[OTHER].scripted


def test_too_few_swaps_are_never_called_scripted():
    assert not pool_monitor.actor_stats([swap(0, ME), swap(60, ME)])[0].scripted


# ---------------------------------------------------------------- seen file


def test_seen_file_round_trip_and_first_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_monitor, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(pool_monitor, "LOG_DIR", tmp_path)
    assert pool_monitor.load_seen() is None
    pool_monitor.save_seen({OTHER, ME})
    assert pool_monitor.load_seen() == {ME, OTHER}


def test_a_corrupt_seen_file_counts_as_a_first_run(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    monkeypatch.setattr(pool_monitor, "SEEN_FILE", path)
    assert pool_monitor.load_seen() is None


# ---------------------------------------------------------------- report


def test_report_tags_you_scripted_and_new_actors(capsys):
    swaps = [swap(t, ME, price=100 + t / 100) for t in range(0, 600, 120)]
    swaps.append(swap(590, OTHER, price=101.0))
    pool_monitor.report(1234, 600, swaps, 24, ("fETH", "f564"), {OTHER}, ME.lower())
    out = capsys.readouterr().out
    assert "block 1234" in out
    assert "actors: 2 in the window, 2 in the last hour" in out
    me_line = next(line for line in out.splitlines() if line.strip().startswith(ME))
    other_line = next(
        line for line in out.splitlines() if line.strip().startswith(OTHER)
    )
    assert "YOU" in me_line and "scripted?" in me_line and "NEW" not in me_line
    assert "NEW" in other_line and "YOU" not in other_line
    assert "new since the last run: 1" in out


def test_report_with_no_swaps(capsys):
    pool_monitor.report(1, 600, [], 24, ("fETH", "f564"), set(), "")
    assert "no swaps in this window" in capsys.readouterr().out


@pytest.mark.parametrize(
    "seconds,text", [(45, "45s"), (300, "5m"), (7200, "2.0h"), (36000, "10.0h")]
)
def test_span(seconds, text):
    assert pool_monitor.span(seconds) == text


# ---------------------------------------------------------------- run loop


class FakeEth:
    def __init__(self):
        self.block_number = 10_000

    def get_block(self, number):
        return {"timestamp": 1_000_000 + number * 12}


class FakeW3:
    def __init__(self):
        self.eth = FakeEth()


class LivePool:
    # `logs` is what get_logs returns for every request
    def __init__(self, logs):
        self.logs = logs
        self.events = self
        self.Swap = self

    def get_logs(self, from_block, to_block):
        return [e for e in self.logs if from_block <= e["blockNumber"] <= to_block]


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_monitor, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(pool_monitor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(pool_monitor.config, "read_token", lambda w3, a: ("T", 18))
    monkeypatch.delenv("BOT_CONTRACT_ADDRESS", raising=False)


class Tokens:
    # stands in for pool.functions.token0() / token1()
    def token0(self):
        return self

    token1 = token0

    def call(self):
        return "0x" + "00" * 20


def make_pool(logs):
    pool = LivePool(logs)
    pool.functions = Tokens()
    return pool


def test_first_run_flags_nobody_then_a_newcomer_is_flagged(monitor_env, capsys):
    w3 = FakeW3()
    pool_monitor.run(w3, make_pool([swap_log(9990, ME, -(10**18))]), 1, 0)
    first = capsys.readouterr().out
    assert ME in first and "NEW" not in first

    logs = [swap_log(9990, ME, -(10**18)), swap_log(9995, OTHER, 10**18)]
    pool_monitor.run(w3, make_pool(logs), 1, 0)
    second = capsys.readouterr().out
    other_line = next(line for line in second.splitlines() if OTHER in line)
    me_line = next(line for line in second.splitlines() if ME in line)
    assert "NEW" in other_line and "NEW" not in me_line


def test_old_swaps_fall_out_of_the_window(monitor_env, capsys):
    # 1h window = 300 blocks: block 9000 is far older than that
    logs = [swap_log(9000, ME, -(10**18)), swap_log(9990, OTHER, 10**18)]
    pool_monitor.run(FakeW3(), make_pool(logs), 1, 0)
    out = capsys.readouterr().out
    assert OTHER in out and ME not in out


# ---------------------------------------------------------------- cli


@pytest.mark.parametrize(
    "argv",
    [
        ["--hours"],
        ["--hours", "x"],
        ["--hours", "0"],
        ["--bogus", "1"],
        ["--watch", "-5"],
    ],
)
def test_cli_rejects_bad_arguments(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pool_monitor.py", *argv])
    with pytest.raises(SystemExit, match="usage"):
        pool_monitor.cli()


def test_cli_passes_the_options_on(monkeypatch):
    calls = []
    monkeypatch.setattr(pool_monitor, "main", lambda *a: calls.append(a))
    monkeypatch.setattr(
        "sys.argv", ["pool_monitor.py", "--hours", "6", "--watch", "60"]
    )
    pool_monitor.cli()
    assert calls == [(6, 60)]
