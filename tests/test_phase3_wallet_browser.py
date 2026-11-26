"""Exercise wallet prompts in isolated Chromium. The provider is entirely synthetic."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.skipif(
    os.environ.get("PHASE2_BROWSER_TESTS") != "1", reason="Run make test-browser"
)
def test_status_while_wallet_prompts_are_open():
    page_path = Path(__file__).resolve().parents[1] / "deploy/phase3/index.html"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route(
            "http://127.0.0.1:8765/", lambda route: route.fulfill(path=page_path)
        )
        page.add_init_script("""
          let accounts = [], chain = '0x1';
          window.sends = 0;
          window.ethereum = {request: async ({method, params}) => {
            switch(method) {
              case 'eth_accounts': return accounts;
              case 'eth_requestAccounts': return new Promise(resolve => {
                window.connectWallet = () => resolve(accounts = ['0xB6bf0Ba3F43D4386216Df037dcA1A1203038e135']);
              });
              case 'eth_chainId': return chain;
              case 'wallet_switchEthereumChain': return new Promise(resolve => {
                window.switchNetwork = () => resolve(chain = params[0].chainId);
              });
              case 'eth_blockNumber': return '0x12';
              case 'eth_call': return '0x00';
              case 'eth_sendTransaction': window.sends++; return new Promise(resolve => {
                window.confirmWallet = () => resolve('0x'+'1'.repeat(64));
              });
              case 'eth_getTransactionReceipt': return null;
              default: throw Error(method);
            }
          }};
        """)
        page.goto("http://127.0.0.1:8765/")
        status = page.locator("#status")
        refresh = page.get_by_role("button", name="Check status")
        page.get_by_role("button", name="Connect wallet").click()
        refresh.click()
        expect(status).to_contain_text("approve the connection")
        page.evaluate("window.connectWallet()")
        expect(status).to_contain_text("Approve the switch")
        refresh.click()
        expect(status).to_contain_text("Select Ethereum Sepolia")
        page.evaluate("window.switchNetwork()")
        page.get_by_role("button", name="Set 1,000 f564 buy limit").click()
        expect(status).to_contain_text("confirm or reject")
        refresh.click()
        expect(status).to_contain_text("confirm or reject")
        expect(page.locator("#review")).to_be_hidden()
        page.evaluate("window.confirmWallet()")
        expect(status).to_contain_text("Waiting for confirmation")
        refresh.click()
        expect(page.locator("#review")).to_be_hidden()
        assert page.evaluate("window.sends") == 1
        browser.close()


@pytest.mark.skipif(
    os.environ.get("PHASE2_BROWSER_TESTS") != "1", reason="Run make test-browser"
)
def test_half_lp_owner_calls_and_reload():
    import time

    from eth_abi import decode, encode
    from web3 import Web3

    from phase3_lp_paper import BAND, FULL, MANAGER, NFT, OWNER
    from phase_markets import PHASE3

    root = Path(__file__).resolve().parents[1] / "deploy/phase3"
    manager = MANAGER.lower()
    tokens = [PHASE3.quote.lower(), PHASE3.base.lower()]
    unit = 10**18
    original = 63245875460548078554
    new_id = 235199
    caps = [790 * unit, 999 * unit // 1000]
    sends = []
    now = int(time.time())
    approve_before_mint = []

    def sel(sig):
        return Web3.keccak(text=sig)[:4]

    def raw(types, vals):
        return "0x" + encode(types, vals).hex()

    def position(id):
        narrow = id == new_id
        liquidity = 10**18 if narrow else original // 2 if len(sends) == 3 else original
        return raw(
            [
                "uint96",
                "address",
                "address",
                "address",
                "uint24",
                "int24",
                "int24",
                "uint128",
                "uint256",
                "uint256",
                "uint128",
                "uint128",
            ],
            [
                0,
                "0x" + "0" * 40,
                *tokens,
                3000,
                *(BAND if narrow else FULL),
                liquidity,
                0,
                0,
                0,
                0,
            ],
        )

    def calldata(tx):
        return bytes.fromhex(tx["data"][2:])

    def inspect_rebalance(data):
        assert data[:4] == sel("multicall(bytes[])")
        (calls,) = decode(["bytes[]"], data[4:])
        assert len(calls) == 3
        for payload, method, fields in zip(
            calls,
            ["decreaseLiquidity", "collect", "mint"],
            [
                (
                    "uint256,uint128,uint256,uint256,uint256",
                    [NFT, original // 2, 998 * unit, caps[1], now + 300],
                ),
                (
                    "uint256,address,uint128,uint128",
                    [NFT, OWNER, 2**128 - 1, 2**128 - 1],
                ),
                (
                    "address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256",
                    [
                        *tokens,
                        3000,
                        *BAND,
                        *caps,
                        783 * unit,
                        994 * unit // 1000,
                        OWNER,
                        now + 300,
                    ],
                ),
            ],
            strict=True,
        ):
            schema, expected = fields
            assert payload[:4] == sel(f"{method}(({schema}))")
            assert decode([f"({schema})"], payload[4:]) == (tuple(expected),)

    def rpc(req):
        method = req["method"]
        params = req.get("params", [])
        if method == "eth_accounts":
            return [OWNER]
        if method == "eth_chainId":
            return "0xaa36a7"
        if method == "eth_getBlockByNumber":
            return {"number": "0x12", "timestamp": hex(now)}
        if method == "eth_getTransactionCount":
            return hex(134 + len(sends))
        if method == "eth_getTransactionReceipt":
            i = sends.index(params[0])
            return {
                "status": "0x1",
                "transactionHash": sends[i],
                "to": [*tokens, manager][i],
                "from": OWNER,
                "logs": []
                if i < 2
                else [
                    {
                        "address": manager,
                        "topics": [
                            Web3.keccak(
                                text="Transfer(address,address,uint256)"
                            ).to_0x_hex(),
                            raw(["uint256"], [0]),
                            raw(["address"], [OWNER]),
                            raw(["uint256"], [new_id]),
                        ],
                    }
                ],
            }
        tx = params[0]
        data = calldata(tx)
        sig = data[:4]
        if method == "eth_sendTransaction":
            i = len(sends)
            assert (
                tx["nonce"] == hex(134 + i)
                and tx["from"] == OWNER
                and tx["chainId"] == "0xaa36a7"
                and tx["value"] == "0x0"
            )
            assert tx["to"] == [*tokens, manager][i]
            if i < 2:
                assert sig == sel("approve(address,uint256)") and decode(
                    ["address", "uint256"], data[4:]
                ) == (manager, caps[i])
            else:
                inspect_rebalance(data)
            sends.append("0x" + str(i + 1) * 64)
            return sends[-1]
        assert method == "eth_call"
        if sig == sel("ownerOf(uint256)"):
            return raw(["address"], [OWNER])
        if sig == sel("positions(uint256)"):
            return position(int.from_bytes(data[4:]))
        if sig == sel("allowance(address,address)"):
            return raw(["uint256"], [caps[tokens.index(tx["to"])]])
        if sig == sel("approve(address,uint256)"):
            return raw(["bool"], [True])
        inspect_rebalance(data)
        approve_before_mint.append(len(sends))
        return raw(
            ["bytes[]"],
            [
                [
                    encode(["uint256"] * 2, [1000 * unit, unit]),
                    encode(["uint256"] * 2, [1000 * unit, unit]),
                    encode(["uint256"] * 4, [new_id, 10**18, 786 * unit, caps[1]]),
                ]
            ],
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.expose_function("mockRpc", rpc)
        url = "http://127.0.0.1:8765/lp-half.html"
        page.route(
            "http://127.0.0.1:8765/lp-half.*",
            lambda route: route.fulfill(path=root / route.request.url.split("/")[-1]),
        )
        page.add_init_script("window.ethereum={request:window.mockRpc}")
        page.goto(url)
        for index, label in enumerate(
            [
                "Approve 790 f564",
                "Approve 0.999 fETH",
                "Move half and mint new LP",
            ]
        ):
            page.get_by_role("button", name=label).click()
            expect(page.locator("#status")).to_contain_text(
                f"Step {index + 2} of 3" if index < 2 else "Confirmed."
            )
            page.reload()
        expect(page.locator("#status")).to_contain_text(
            f"Confirmed. New LP NFT #{new_id}"
        )
        assert approve_before_mint == [2] and len(sends) == 3
        browser.close()


@pytest.mark.skipif(
    os.environ.get("PHASE2_BROWSER_TESTS") != "1", reason="Run make test-browser"
)
def test_status_429_uses_public_reads_and_never_retries_wallet_submission():
    page_path = Path(__file__).resolve().parents[1] / "deploy/phase3/index.html"
    public_methods = []

    def public_reply(route):
        req = route.request.post_data_json
        method = req["method"]
        public_methods.append(method)
        result = {
            "eth_chainId": "0xaa36a7",
            "eth_blockNumber": "0x123",
            "eth_call": "0x" + "0" * 64,
        }[method]
        route.fulfill(
            json={"jsonrpc": "2.0", "id": req["id"], "result": result},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route(
            "http://127.0.0.1:8765/", lambda route: route.fulfill(path=page_path)
        )
        page.route("https://ethereum-sepolia.publicnode.com/", public_reply)
        page.add_init_script("""
          window.sends=0;
          window.ethereum={request:async ({method})=>{
            if(method==='eth_accounts')return ['0xB6bf0Ba3F43D4386216Df037dcA1A1203038e135'];
            if(method==='eth_chainId')return '0xaa36a7';
            if(method==='eth_sendTransaction')window.sends++;
            throw Error('RPC request failed: 429');
          }};
        """)
        page.goto("http://127.0.0.1:8765/")
        button = page.get_by_role("button", name="Set 1,000 f564 buy limit")
        expect(button).to_be_visible()
        assert public_methods == ["eth_chainId", "eth_blockNumber", "eth_call"]
        button.click()
        expect(page.locator("#status")).to_contain_text("Submission outcome unknown")
        page.get_by_role("button", name="Check status").click()
        expect(page.locator("#status")).to_contain_text("no transaction hash yet")
        assert page.evaluate("window.sends") == 1
        browser.close()
