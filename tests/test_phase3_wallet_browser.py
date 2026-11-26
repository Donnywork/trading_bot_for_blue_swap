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
