from decimal import Decimal

import pytest
import requests
from web3.exceptions import Web3RPCError

import config
import legacy_main as main
import pool_monitor


def test_no_cap_when_unset_or_zero(monkeypatch):
    monkeypatch.delenv("MAX_RPC_PER_SECOND", raising=False)
    assert config.rpc_rate() is None
    monkeypatch.setenv("MAX_RPC_PER_SECOND", "0")
    assert config.rpc_rate() is None


def test_rate_reads_a_number(monkeypatch):
    monkeypatch.setenv("MAX_RPC_PER_SECOND", "5")
    assert config.rpc_rate() == Decimal(5)


@pytest.mark.parametrize("value", ["-1", "abc", "", "nan"])
def test_bad_rate_stops_the_program(monkeypatch, value):
    monkeypatch.setenv("MAX_RPC_PER_SECOND", value)
    with pytest.raises(SystemExit):
        config.rpc_rate()


def fake_clock(monkeypatch):
    # a monotonic clock that only moves when the provider sleeps, so the test needs no real time
    now = [1000.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(config.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(config.time, "sleep", fake_sleep)
    return sleeps


def stub_responses(monkeypatch):
    calls = []

    def send(self, req, **kwargs):
        import json

        payload = json.loads(req.body)
        calls.append(payload["method"])
        reply = requests.Response()
        reply.status_code = 200
        reply._content = json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": "0x1"}
        ).encode()
        return reply

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


def test_provider_spaces_requests(monkeypatch):
    sleeps = fake_clock(monkeypatch)
    calls = stub_responses(monkeypatch)
    provider = config.ThrottledProvider("http://rpc.test", rate=Decimal(2))
    for _ in range(3):
        provider.make_request("eth_blockNumber", [])
    assert calls == ["eth_blockNumber"] * 3
    assert sleeps == [0.5, 0.5]  # 2 per second -> half a second apart


def test_provider_without_rate_never_sleeps(monkeypatch):
    sleeps = fake_clock(monkeypatch)
    stub_responses(monkeypatch)
    provider = config.rpc_provider(
        "http://rpc.test"
    )  # MAX_RPC_PER_SECOND unset in tests
    for _ in range(3):
        provider.make_request("eth_getLogs", [])
    assert sleeps == []


def test_provider_takes_the_cap_from_env(monkeypatch):
    monkeypatch.setenv("MAX_RPC_PER_SECOND", "4")
    assert config.rpc_provider("http://rpc.test").batch_size == 4


# ---------------------------------------------------------------- failover


class Fake:
    """A provider stub: raises `error` when set, else returns `result` and records calls."""

    def __init__(self, result=None):
        self.result = result or {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
        self.error = None
        self.calls = []

    def make_request(self, method, params):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result

    def is_connected(self):
        return self.error is None


def test_fallback_urls_are_optional_and_trimmed(monkeypatch):
    monkeypatch.delenv("RPC_FALLBACK_URLS", raising=False)
    assert config.fallback_urls() == []
    monkeypatch.setenv("RPC_FALLBACK_URLS", " http://a.test , http://b.test ,, ")
    assert config.fallback_urls() == ["http://a.test", "http://b.test"]


def test_happy_path_sticks_to_the_primary():
    primary, other = Fake(), Fake()
    provider = config.FailoverProvider([primary, other])
    assert provider.make_request("eth_blockNumber", []) == primary.result
    assert len(primary.calls) == 1
    assert other.calls == []


def test_transport_error_switches_and_the_same_request_is_retried():
    primary, other = Fake(), Fake()
    primary.error = ConnectionError("429 Too Many Requests for url: http://secret")
    provider = config.FailoverProvider([primary, other])
    assert provider.make_request("eth_getLogs", [{"fromBlock": "0x1"}]) == other.result
    assert (other.calls[0]) == ("eth_getLogs", [{"fromBlock": "0x1"}])
    # sticky: the next call goes straight to the provider that worked
    provider.make_request("eth_blockNumber", [])
    assert len(primary.calls) == 1
    assert len(other.calls) == 2


def test_a_rebroadcast_retries_the_identical_bytes():
    # a re-sent swap must be the same signed transaction, never a rebuilt one
    primary, other = Fake(), Fake()
    primary.error = ConnectionError("connection reset")
    provider = config.FailoverProvider([primary, other])
    raw = "0x02f8b2012345"
    provider.make_request("eth_sendRawTransaction", [raw])
    assert primary.calls == other.calls == [("eth_sendRawTransaction", [raw])]


def test_a_node_error_message_also_switches():
    primary, other = Fake(), Fake()
    primary.error = Web3RPCError("Under the Free tier plan ...")
    provider = config.FailoverProvider([primary, other])
    assert provider.make_request("eth_call", []) == other.result


def test_every_provider_failing_raises_the_last_error():
    primary, other = Fake(), Fake()
    primary.error = ConnectionError("down")
    other.error = ConnectionError("capped")
    provider = config.FailoverProvider([primary, other])
    with pytest.raises(ConnectionError, match="capped"):
        provider.make_request("eth_blockNumber", [])
    assert len(primary.calls) == len(other.calls) == 1


def test_three_providers_cycle_to_the_last():
    a, b, c = Fake(), Fake(), Fake()
    a.error = ConnectionError("down")
    b.error = Web3RPCError("limited")
    provider = config.FailoverProvider([a, b, c])
    assert provider.make_request("eth_blockNumber", []) == c.result


def test_a_cooled_provider_is_tried_again_once_its_cooldown_passes(monkeypatch):
    now = [1000.0]

    def advance(seconds):  # the clock only moves when someone sleeps
        now[0] += seconds

    monkeypatch.setattr(config.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(config.time, "sleep", advance)
    a, b = Fake(), Fake()
    a.error = ConnectionError("down")
    provider = config.FailoverProvider([a, b], cooldown=60.0)
    provider.make_request("eth_blockNumber", [])  # a fails, b serves
    a.error = None
    b.error = ConnectionError("down")
    now[0] += 120.0  # past a's cooldown
    assert provider.make_request("eth_blockNumber", []) == a.result


def test_is_connected_accepts_any_provider():
    down, up = Fake(), Fake()
    down.error = ConnectionError("down")
    assert config.FailoverProvider([down, up]).is_connected()
    up.error = ConnectionError("down")
    assert not config.FailoverProvider([down, up]).is_connected()


def test_rpc_provider_builds_failover_only_when_a_fallback_is_set(monkeypatch):
    monkeypatch.delenv("RPC_FALLBACK_URLS", raising=False)
    assert isinstance(config.rpc_provider("http://a.test"), config.ThrottledProvider)
    monkeypatch.setenv("RPC_FALLBACK_URLS", "http://b.test")
    provider = config.rpc_provider("http://a.test")
    assert isinstance(provider, config.FailoverProvider)
    assert len(provider._providers) == 2


def test_web3_accepts_the_failover_provider():
    w3 = config.Web3(config.FailoverProvider([Fake(), Fake()]))
    assert w3.is_connected()
    assert w3.eth.chain_id == 1  # "0x1" from the fake, through web3's formatting


def test_a_plan_limited_primary_rides_the_fallback_for_wide_getlogs():
    # the promise: Alchemy first + a fallback means pool_monitor's wide chunks fail over
    # instead of erroring (or shrinking), so the plan limit stops mattering
    class AlchemyFree:
        # refuses any eth_getLogs wider than 10 blocks, like the real free tier
        def __init__(self):
            self.calls = 0

        def make_request(self, method, params):
            self.calls += 1
            if method == "eth_getLogs":
                f = params[0]
                if int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1 > 10:
                    raise Web3RPCError(
                        "Under the Free tier plan ... up to a 10 block range"
                    )
            return {"jsonrpc": "2.0", "id": 1, "result": []}

        def is_connected(self):
            return True

    class Infura:
        def __init__(self):
            self.calls = 0

        def make_request(self, method, params):
            self.calls += 1
            return {"jsonrpc": "2.0", "id": 1, "result": []}

        def is_connected(self):
            return True

    class Pool:
        # pool.events.Swap.get_logs backed by the real web3 stack
        def __init__(self, w3):
            self._w3 = w3
            self.events = self
            self.Swap = self

        def get_logs(self, from_block, to_block):
            return self._w3.eth.get_logs({"fromBlock": from_block, "toBlock": to_block})

    alchemy, infura = AlchemyFree(), Infura()
    w3 = config.Web3(config.FailoverProvider([alchemy, infura]))
    assert pool_monitor.fetch_swaps(Pool(w3), 0, 4999, chunk=2000) == []
    assert alchemy.calls == 1  # refused the first wide chunk, then left alone (sticky)
    assert infura.calls == 3  # 0..4999 in 2000-block chunks, no shrinking to 7


def test_redact_covers_the_fallback_urls_too(monkeypatch):
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.setenv(
        "RPC_FALLBACK_URLS", "https://eth-sepolia.g.alchemy.com/v2/abcd1234efgh5678"
    )
    text = "failed for https://eth-sepolia.g.alchemy.com/v2/abcd1234efgh5678 today"
    assert main.redact(text) == "failed for *** today"
    assert "abcd1234efgh5678" not in main.redact("key abcd1234efgh5678 leaked")
