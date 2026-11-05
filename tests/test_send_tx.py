import pytest
from web3.exceptions import TimeExhausted, Web3RPCError

import bot_contract


class TxHash:
    def to_0x_hex(self):
        return "0x" + "ab" * 32


class Signed:
    raw_transaction = b"raw"


class Account:
    address = "0x" + "11" * 20

    def sign_transaction(self, tx):
        return Signed()


class Eth:
    # a node whose steps can be made to fail, to see how send_tx classifies each failure
    chain_id = 11155111
    max_priority_fee = 1

    def __init__(self):
        self.fail = {}
        self.receipt = {"status": 1, "logs": []}

    def _step(self, name):
        if name in self.fail:
            raise self.fail[name]

    def get_transaction_count(self, *a):
        self._step("nonce")
        return 7

    def estimate_gas(self, tx):
        self._step("gas")
        return 100_000

    def get_block(self, *a):
        self._step("block")
        return {"baseFeePerGas": 10}

    def send_raw_transaction(self, raw):
        self._step("send")
        return TxHash()

    def wait_for_transaction_receipt(self, tx_hash):
        self._step("receipt")
        return self.receipt


class W3:
    def __init__(self):
        self.eth = Eth()


def send(w3):
    return bot_contract.send_tx(w3, Account(), {"to": "0x" + "22" * 20, "data": "0x"})


def test_a_successful_send_returns_the_receipt():
    w3 = W3()
    assert send(w3) is w3.eth.receipt


@pytest.mark.parametrize("step", ["nonce", "gas", "block"])
def test_a_connection_error_before_the_broadcast_means_not_sent(step):
    w3 = W3()
    w3.eth.fail[step] = ConnectionError("rpc down")
    with pytest.raises(bot_contract.NotSent):
        send(w3)


def test_a_connection_error_while_broadcasting_may_have_sent_it():
    w3 = W3()
    w3.eth.fail["send"] = ConnectionError("reset by peer")
    with pytest.raises(bot_contract.SentUnknown):
        send(w3)


@pytest.mark.parametrize(
    "error", [ConnectionError("down"), Web3RPCError("rate limited")]
)
def test_any_failure_reading_the_receipt_of_a_broadcast_tx_is_unknown(error):
    w3 = W3()
    w3.eth.fail["receipt"] = error
    with pytest.raises(bot_contract.SentUnknown, match="0x" + "ab" * 32):
        send(w3)


def test_a_receipt_timeout_stays_a_timeout():
    w3 = W3()
    w3.eth.fail["receipt"] = TimeExhausted("no receipt")
    with pytest.raises(TimeExhausted):
        send(w3)


def test_a_node_that_rejects_the_transaction_is_an_ordinary_failure():
    # the node answered with an error: it was refused, not sent
    w3 = W3()
    w3.eth.fail["send"] = Web3RPCError("nonce too low")
    with pytest.raises(Web3RPCError):
        send(w3)


class RecordingAccount(Account):
    def sign_transaction(self, tx):
        self.tx = tx
        return Signed()


def gas_limit_of(**kw):
    account = RecordingAccount()
    bot_contract.send_tx(
        W3(), account, {"to": "0x" + "22" * 20, "data": "0x"}, **kw
    )  # the fake node estimates 100,000
    return account.tx["gas"]


def test_the_gas_limit_is_the_estimate_times_the_buffer():
    assert gas_limit_of() == int(100_000 * bot_contract.GAS_BUFFER)
    assert gas_limit_of(gas_buffer=2.0) == 200_000


def test_a_swap_gets_far_more_gas_headroom_than_20_percent():
    # Four swaps of 2026-11-05 ran out of gas inside the pool at estimate x 1.2: another swap moved
    # the price in the same block, so they crossed more ticks and needed 21 to 38% more. The
    # headroom has to cover the worst of those with room to spare.
    worst_seen = 387_117 / 279_578
    assert bot_contract.GAS_BUFFER < worst_seen
    assert bot_contract.SWAP_GAS_BUFFER >= worst_seen * 1.4


def test_a_reverted_transaction_is_an_ordinary_failure():
    w3 = W3()
    w3.eth.receipt = {"status": 0, "logs": []}
    with pytest.raises(RuntimeError, match="reverted"):
        send(w3)
