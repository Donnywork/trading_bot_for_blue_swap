"""Read-only identity verification at the saved block, using the public RPC proxy."""

import json
from pathlib import Path

from web3 import Web3

OUT = Path(__file__).parent


def main():
    saved = json.loads((OUT / "snapshot.json").read_text())
    block = saved["block"]
    w3 = Web3(Web3.HTTPProvider(saved["source"], request_kwargs={"timeout": 45}))
    assert w3.eth.chain_id == 11155111
    assert w3.eth.get_block(block)["hash"].to_0x_hex() == saved["block_hash"]

    def call(address, signature, in_types=(), args=(), out_types=("address",)):
        data = Web3.keccak(text=signature)[:4] + w3.codec.encode(in_types, args)
        raw = w3.eth.call({"to": Web3.to_checksum_address(address), "data": data}, block_identifier=block)
        return w3.codec.decode(out_types, raw)[0]

    router = "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E"
    quoter = "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"
    factory = call(router, "factory()")
    assert call(quoter, "factory()") == factory
    for profile in saved["profiles"]:
        assert profile["factory"].lower() == factory
        actual = call(factory, "getPool(address,address,uint24)",
                      ("address", "address", "uint24"),
                      (profile["quote_address"], profile["base_address"], profile["fee"]))
        assert actual == profile["pool_address"]
        for token in (profile["quote_address"], profile["base_address"]):
            assert call(token, "decimals()", out_types=("uint8",)) == 18
    verification = {"block": block, "block_hash": saved["block_hash"], "router": router,
                    "quoter": quoter, "factory": factory, "pool_registration_verified": True,
                    "both_token_decimals": 18}
    (OUT / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    print(json.dumps(verification, indent=2))


if __name__ == "__main__":
    main()
