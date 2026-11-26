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
