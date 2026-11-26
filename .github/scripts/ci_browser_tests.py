#!/usr/bin/env python3
"""CI 浏览器功能测试。

在 GitHub Actions 里连接刚构建出的浏览器容器 CDP 端口，分别用基础 HTTP/WS 客户端
以及几种 Python 自动化客户端做同一组最小功能验证：导航、DOM 读取、JS 执行、截图。
测试全部在 CI 隔离环境运行，不污染本地开发机；脚本只用于 CI，不进入运行时镜像。
"""

import argparse
import asyncio
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PASS = []
FAIL = []


def check(name, fn):
    try:
        detail = fn()
        PASS.append((name, detail))
        print(f"[PASS] {name}: {detail}")
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        FAIL.append((name, detail))
        print(f"[FAIL] {name}: {detail}")


def chrome_version_from_dockerfile() -> str:
    for filename in ("Dockerfile.browser", "Dockerfile.browser-headless"):
        path = Path(filename)
        if not path.exists():
            continue
        match = re.search(r"CHROME_VERSION[= ]+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", path.read_text())
        if match:
            return match.group(1)
    raise RuntimeError("无法从 Dockerfile 读取 CHROME_VERSION")


def ensure_chromedriver(version: str) -> str:
    target_dir = Path("/tmp/cdp-ci-chromedriver")
    driver = target_dir / "chromedriver-linux64" / "chromedriver"
    if driver.exists():
        return str(driver)
    url = (
        f"https://storage.googleapis.com/chrome-for-testing-public/"
        f"{version}/linux64/chromedriver-linux64.zip"
    )
    with urllib.request.urlopen(url, timeout=90) as r:
        payload = r.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(target_dir)
    driver.chmod(0o755)
    return str(driver)


def browser_ws_url(cdp: str) -> str:
    with urllib.request.urlopen(cdp.rstrip("/") + "/json/version", timeout=10) as r:
        info = json.loads(r.read().decode("utf-8", "replace"))
    return info["webSocketDebuggerUrl"]


def test_requests(cdp: str):
    import requests

    resp = requests.get(cdp.rstrip("/") + "/json/version", timeout=10)
    resp.raise_for_status()
    browser = resp.json().get("Browser", "")
    if not browser:
        raise RuntimeError("/json/version 未返回 Browser")
    return browser


def test_websocket_client(cdp: str):
    import websocket

    ws = websocket.create_connection(browser_ws_url(cdp), timeout=10, suppress_origin=True)
    try:
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "21*2", "returnByValue": True},
                }
            )
        )
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
        value = msg["result"]["result"].get("value")
        if value != 42:
            raise RuntimeError(f"Runtime.evaluate 返回 {value}")
        return "Runtime.evaluate=42"
    finally:
        ws.close()


async def _pydoll_async(cdp: str):
    from pydoll.browser.chromium import Chrome

    chrome = Chrome()
    tab = await chrome.connect(browser_ws_url(cdp))
    await tab.go_to("data:text/html,<h1 id='t'>PYDOLL_OK</h1>")
    value = await tab.execute_script("return document.querySelector('#t').textContent")
    return value


def test_pydoll(cdp: str):
    value = asyncio.run(_pydoll_async(cdp))
    if value != "PYDOLL_OK":
        raise RuntimeError(f"DOM 读取结果为 {value}")
    return "导航与 DOM 读取 OK"


def test_playwright(cdp: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp.rstrip("/"))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("data:text/html,<h1 id='t'>PLAYWRIGHT_OK</h1>")
        value = page.locator("#t").inner_text()
        png = page.screenshot()
        if value != "PLAYWRIGHT_OK":
            raise RuntimeError(f"DOM 读取结果为 {value}")
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("截图不是 PNG")
        # 不调用 browser.close()，避免断开上游浏览器影响后续用例。
        return f"导航/DOM/截图 OK（{len(png)} 字节 PNG）"


def test_selenium(cdp: str):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By

    version = chrome_version_from_dockerfile()
    driver_path = ensure_chromedriver(version)
    parsed = urllib.parse.urlparse(cdp.rstrip("/"))
    debugger_address = f"{parsed.hostname}:{parsed.port}"

    options = Options()
    options.add_experimental_option("debuggerAddress", debugger_address)
    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    try:
        driver.get("data:text/html,<h1 id='t'>SELENIUM_OK</h1>")
        value = driver.find_element(By.ID, "t").text
        if value != "SELENIUM_OK":
            raise RuntimeError(f"DOM 读取结果为 {value}")
        return f"导航与 DOM 读取 OK（driver {version}）"
    finally:
        driver.service.stop()
        try:
            driver.quit()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", required=True, help="浏览器 CDP HTTP 根地址，如 http://127.0.0.1:9222")
    parser.add_argument("--mode", default="", help="仅用于日志标识：full / headless")
    args = parser.parse_args()

    print(f"=== CI 浏览器功能测试 mode={args.mode or '-'} cdp={args.cdp} ===")
    check("基础 HTTP 客户端", lambda: test_requests(args.cdp))
    check("基础 WebSocket 客户端", lambda: test_websocket_client(args.cdp))
    check("Python 自动化客户端 A", lambda: test_pydoll(args.cdp))
    check("Python 自动化客户端 B", lambda: test_playwright(args.cdp))
    check("Python 自动化客户端 C", lambda: test_selenium(args.cdp))

    print(f"=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    if FAIL:
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
